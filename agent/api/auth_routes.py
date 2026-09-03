"""Sign-in, session and sign-out for the dashboard.

Four routes, mounted under /auth to keep them clearly apart from /api — /api
is the tenant-scoped resource surface, this is how a browser acquires the
identity that lets it call /api at all.

No route here returns a GitHub token, a refresh token or the client secret,
and none of those values is written to a log. What the browser gets is an
opaque session id in an HttpOnly cookie and a CSRF token it must echo back on
mutations. The CSRF token is returned by /auth/session as well as set as a
cookie, because in production the dashboard and the API are different hosts
and a host-only cookie is not readable across them.
"""
from __future__ import annotations

import logging
import urllib.parse

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from agent.api.sessions import (
    CSRF_COOKIE, OAUTH_NONCE_COOKIE, OAUTH_STATE_LIFETIME, SESSION_COOKIE,
    SESSION_LIFETIME, SessionError,
)

logger = logging.getLogger(__name__)


def _safe_redirect(value, default="/"):
    """Only same-site paths are accepted as a post-login destination.

    An open redirect on a login callback is a phishing primitive: it lets an
    attacker send a victim through a genuine sign-in and land them somewhere
    of the attacker's choosing. Anything with a scheme, a host, or a
    protocol-relative prefix is discarded rather than sanitised.
    """
    if not isinstance(value, str) or not value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return default
    return value


def create_auth_routes(*, store_pool, session_manager, dashboard_url,
                       callback_url, organization_id, repository_id,
                       environment, secure_cookies=True):
    """Build the /auth route table.

    ``organization_id`` / ``repository_id`` are the tenant this deployment
    serves. For a single-design-partner pilot that is one repository, and
    binding it in configuration means a session can never be minted against a
    repository the operator did not intend.
    """

    def _cookie(response, name, value, *, max_age, http_only=True):
        response.set_cookie(
            name, value,
            max_age=max_age,
            httponly=http_only,
            secure=secure_cookies,
            samesite="lax",
            path="/",
        )

    def _clear(response, name):
        response.delete_cookie(name, path="/")

    async def start(request):
        """Begin authorization and send the browser to GitHub."""
        redirect_to = _safe_redirect(request.query_params.get("redirect_to"))

        def work():
            with store_pool.acquire() as store:
                return session_manager.begin_authorization(
                    store, redirect_to=redirect_to, redirect_uri=callback_url)

        url, nonce = await run_in_threadpool(work)
        response = RedirectResponse(url, status_code=302)
        # Binds this authorization attempt to this browser. Without it a state
        # value observed in transit could be completed elsewhere.
        _cookie(response, OAUTH_NONCE_COOKIE, nonce,
                max_age=int(OAUTH_STATE_LIFETIME.total_seconds()))
        return response

    async def callback(request):
        """Complete authorization and mint the session."""
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        nonce = request.cookies.get(OAUTH_NONCE_COOKIE)

        def work():
            with store_pool.acquire() as store:
                return session_manager.complete_authorization(
                    store, code=code, state=state, nonce=nonce,
                    redirect_uri=callback_url,
                    organization_id=organization_id,
                    repository_id=repository_id,
                    environment=environment)

        try:
            result = await run_in_threadpool(work)
        except SessionError as exc:
            # The reason is shown to the operator but never the values that
            # produced it.
            logger.info("dashboard_sign_in_refused",
                        extra={"operation": "auth_callback"})
            failed = RedirectResponse(
                f"{dashboard_url}/signed-out?error="
                f"{urllib.parse.quote(str(exc)[:200])}", status_code=302)
            _clear(failed, OAUTH_NONCE_COOKIE)
            return failed

        destination = _safe_redirect(result.get("redirect_to"))
        response = RedirectResponse(f"{dashboard_url}{destination}", status_code=302)
        _cookie(response, SESSION_COOKIE, result["session_id"],
                max_age=int(SESSION_LIFETIME.total_seconds()))
        # Readable by the dashboard so it can echo it back on mutations. It is
        # not a credential on its own: a request needs the session cookie too.
        _cookie(response, CSRF_COOKIE, result["csrf_token"],
                max_age=int(SESSION_LIFETIME.total_seconds()), http_only=False)
        _clear(response, OAUTH_NONCE_COOKIE)
        return response

    async def session(request):
        """Who the browser currently is, if anyone.

        The dashboard calls this on load. It returns identity, authority and
        the session's CSRF token — never a credential.

        ``csrf_token`` is here because the dashboard and the API are different
        hosts in production, so the script-readable CSRF cookie the API sets on
        ``api.relium.dev`` is invisible to ``document.cookie`` on
        ``app.relium.dev``. See ``SessionManager.csrf_token``. It is safe to
        return: this response is reachable only with the session cookie
        attached, and CORS lists the dashboard origin exactly, so no other page
        can read the body. It is also useless alone — a mutation still needs
        the HttpOnly session cookie, which script cannot touch.
        """
        session_id = request.cookies.get(SESSION_COOKIE)

        def work():
            with store_pool.acquire() as store:
                principal = session_manager.authenticate(store, session_id)
                # Read inside the same acquisition: a second one could observe
                # a session revoked in between and report no token for an
                # identity it just confirmed.
                return principal, session_manager.csrf_token(store, session_id)

        try:
            principal, csrf_token = await run_in_threadpool(work)
        except SessionError:
            return JSONResponse({"authenticated": False}, status_code=401)

        return JSONResponse({
            "authenticated": True,
            "login": principal.github_login,
            "actor": principal.actor,
            "repository": f"{principal.organization_id}/{principal.repository_id}",
            "environment": principal.environment,
            "github_permission": principal.github_permission,
            "may_govern": principal.may_govern,
            "csrf_token": csrf_token,
        })

    async def logout(request):
        """Revoke the session and destroy the stored GitHub credentials."""
        session_id = request.cookies.get(SESSION_COOKIE)

        def work():
            with store_pool.acquire() as store:
                return session_manager.revoke(store, session_id, "logout")

        await run_in_threadpool(work)
        response = JSONResponse({"authenticated": False})
        _clear(response, SESSION_COOKIE)
        _clear(response, CSRF_COOKIE)
        return response

    return [
        Route("/auth/github/start", start, methods=["GET"]),
        Route("/auth/github/callback", callback, methods=["GET"]),
        Route("/auth/session", session, methods=["GET"]),
        Route("/auth/logout", logout, methods=["POST"]),
    ]
