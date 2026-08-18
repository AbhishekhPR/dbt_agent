"""The GitHub half of onboarding: linking a human, and binding an installation.

Four routes, three different kinds of caller — kept apart deliberately, because
each proves a different thing:

    POST /api/onboarding/github/identity   Clerk session. Starts GitHub OAuth
                                           so Relium can later ask GitHub, as
                                           this person, what they may reach.
    POST /api/onboarding/github/install    Clerk session. Mints a single-use
                                           state and returns the install URL.
    GET  /auth/github/link/callback        GitHub OAuth redirect. Authenticated
                                           by the single-use state, not a
                                           session.
    GET  /github/setup                     GitHub App Setup redirect. Also
                                           authenticated by the state. Every
                                           query value is attacker-controlled.

WHY THE SETUP REDIRECT IS NOT AUTHENTICATED BY A SESSION
--------------------------------------------------------
It arrives as a top-level browser navigation from github.com, so it cannot
carry an Authorization header, and a cookie would be sent cross-site. The
single-use state IS the credential: it was minted against a verified Clerk
session, it is bound server-side to a tenant and a user, and it is spent once.

That still does not make the redirect trustworthy. It says which flow this is;
it does not say what was installed. See agent/api/github_installation.py.
"""
from __future__ import annotations

import logging
import urllib.parse

from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse
from starlette.routing import Route

from agent.api.github_installation import (
    CODE_APP_NOT_CONFIGURED, CODE_GITHUB_IDENTITY_REQUIRED, CODE_STATE_INVALID,
    INSTALLATION_STATE_LIFETIME, InstallationBindingError, hash_state, new_state,
)
from agent.api.validation import isoformat

logger = logging.getLogger(__name__)

#: Where a completed or failed GitHub round trip sends the browser back to.
DEFAULT_RETURN_PATH = "/onboarding"


def safe_return_path(value, default=DEFAULT_RETURN_PATH):
    """Only same-site paths are accepted as a post-redirect destination.

    Identical rule and identical reasoning to auth_routes._safe_redirect: an
    open redirect on a callback is a phishing primitive, letting an attacker
    send a victim through a genuine GitHub flow and land them somewhere of the
    attacker's choosing. Anything with a scheme, a host, or a protocol-relative
    prefix is discarded rather than sanitised.
    """
    if not isinstance(value, str) or not value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return default
    return value


def _redirect_back(app_url, path, params):
    target = f"{app_url}{safe_return_path(path)}"
    if params:
        separator = "&" if "?" in target else "?"
        target = f"{target}{separator}{urllib.parse.urlencode(params)}"
    return RedirectResponse(target, status_code=302)


def create_onboarding_github_routes(*, store_pool, clerk_authenticator,
                                    binder=None, identity_linker=None,
                                    app_url=""):
    """Build the GitHub onboarding routes.

    ``binder`` and ``identity_linker`` are None when this deployment has no
    GitHub App or no GitHub user-authorization credentials configured. The
    routes are still registered — the served route table must be stable, and
    the API contract test depends on it — and answer 503.

    ``clerk_authenticator`` is the shared authentication callable from
    onboarding_routes, so there is exactly one place a Clerk token is verified.
    """

    def _require(component, request_id, json_writer):
        if component is None:
            return json_writer({"status": "unavailable",
                                "code": CODE_APP_NOT_CONFIGURED}, 503, request_id)
        return None

    # -- POST /api/onboarding/github/identity ------------------------------

    def start_identity_link(request, body, store, principal):
        """Begin GitHub OAuth so this Clerk user can prove a GitHub identity.

        Returns a URL rather than redirecting: the caller holds a Clerk bearer
        token, which a browser navigation could not carry, so the frontend
        fetches this and then navigates.
        """
        if principal.tenant_id is None:
            raise InstallationBindingError(
                "workspace_required",
                "create the Relium workspace before connecting GitHub")

        state = new_state()
        expires_at = identity_linker.expires_at()
        store.create_github_installation_state(
            state_hash=hash_state(state),
            tenant_id=principal.tenant_id,
            clerk_user_id=principal.clerk_user_id,
            # Same clock as the expiry it is paired with. See
            # create_github_installation_state.
            created_at=expires_at - INSTALLATION_STATE_LIFETIME,
            expires_at=expires_at,
            purpose="github_identity_link",
        )
        return 200, {
            "authorize_url": identity_linker.authorize_url(state),
            "expires_at": isoformat(identity_linker.expires_at()),
        }

    # -- POST /api/onboarding/github/install -------------------------------

    def start_installation(request, body, store, principal):
        """Mint a single-use state and return where to install the App.

        The state is opaque and carries no tenant id. The tenant lives only in
        the database row, so the value cannot be decoded, edited, or re-pointed
        at another workspace.
        """
        if principal.tenant_id is None:
            raise InstallationBindingError(
                "workspace_required",
                "create the Relium workspace before connecting GitHub")

        # A GitHub identity is required to COMPLETE the flow, so say so now
        # rather than letting someone install the App and only then discover
        # the binding cannot be verified.
        if store.clerk_github_identity(principal.clerk_user_id) is None:
            raise InstallationBindingError(
                CODE_GITHUB_IDENTITY_REQUIRED,
                "link a GitHub identity before installing the App")

        started = binder.start(store, tenant_id=principal.tenant_id,
                               clerk_user_id=principal.clerk_user_id)
        return 200, {
            "install_url": started["install_url"],
            "expires_at": isoformat(started["expires_at"]),
        }

    # -- GET /auth/github/link/callback ------------------------------------

    async def complete_identity_link(request):
        request_id = _request_id(request)
        unavailable = _require(identity_linker, request_id, _json)
        if unavailable is not None:
            return unavailable

        code = request.query_params.get("code")
        presented_state = request.query_params.get("state")

        def work():
            with store_pool.acquire() as store:
                return identity_linker.complete(
                    store, code=code, presented_state=presented_state)

        try:
            await run_in_threadpool(work)
        except InstallationBindingError as exc:
            logger.info("github_identity_link_refused", extra={
                "operation": "github_link_callback", "reason": exc.code})
            return _redirect_back(app_url, DEFAULT_RETURN_PATH,
                                  {"github_error": exc.code})
        except Exception:
            logger.error("github_identity_link_failed",
                         extra={"error_category": "internal"})
            return _redirect_back(app_url, DEFAULT_RETURN_PATH,
                                  {"github_error": "github_unavailable"})

        return _redirect_back(app_url, DEFAULT_RETURN_PATH,
                              {"github_linked": "1"})

    # -- GET /github/setup -------------------------------------------------

    async def setup_redirect(request):
        """GitHub's post-installation browser redirect.

        ###############################################################
        # EVERY QUERY VALUE HERE IS ATTACKER-CONTROLLED.              #
        ###############################################################

        `installation_id`, `state` and `setup_action` all arrive in a URL that
        anyone can construct and send to a victim. The state is verified and
        spent; the installation id is treated as a claim and checked against
        GitHub twice — once as the App, once as the human. Nothing is bound on
        the strength of the URL.
        """
        request_id = _request_id(request)
        unavailable = _require(binder, request_id, _json)
        if unavailable is not None:
            return unavailable

        presented_state = request.query_params.get("state")
        setup_action = request.query_params.get("setup_action")
        raw_installation = request.query_params.get("installation_id")

        # `request` means an organization owner must still approve the
        # installation. A real, common outcome that is not a failure — and
        # there is nothing to bind yet.
        if setup_action == "request":
            return _redirect_back(app_url, DEFAULT_RETURN_PATH,
                                  {"github_pending": "approval"})

        installation_id = None
        if isinstance(raw_installation, str) and raw_installation.isdigit():
            # Bounded before int(): an unbounded numeric string is a cheap way
            # to make the process do arbitrary work.
            if len(raw_installation) <= 20:
                installation_id = int(raw_installation)

        def work():
            with store_pool.acquire() as store:
                return binder.complete(store, presented_state=presented_state,
                                       installation_id=installation_id)

        try:
            binding = await run_in_threadpool(work)
        except InstallationBindingError as exc:
            # The code is stable and safe; the presented state and the
            # installation id are never echoed into the redirect.
            logger.info("github_installation_refused", extra={
                "operation": "install_setup", "reason": exc.code})
            return _redirect_back(app_url, DEFAULT_RETURN_PATH,
                                  {"github_error": exc.code})
        except Exception:
            logger.error("github_installation_setup_failed",
                         extra={"error_category": "internal"})
            return _redirect_back(app_url, DEFAULT_RETURN_PATH,
                                  {"github_error": "github_unavailable"})

        return _redirect_back(app_url, DEFAULT_RETURN_PATH, {
            "github_installed": "1",
            "installation_id": str(binding.installation_id),
        })

    # -- plumbing ----------------------------------------------------------

    def clerk_handler(fn, *, write):
        """A Clerk-authenticated JSON handler, using the shared authenticator."""
        async def wrapped(request):
            request_id = _request_id(request)
            unavailable = _require(binder if fn is start_installation
                                   else identity_linker, request_id, _json)
            if unavailable is not None:
                return unavailable

            try:
                principal_holder = {}

                def work():
                    with store_pool.acquire() as store:
                        principal = clerk_authenticator.principal(
                            request, store, write=write)
                        principal_holder["p"] = principal
                        return fn(request, None, store, principal)

                status, payload = await run_in_threadpool(work)
                return _json(payload, status, request_id)
            except InstallationBindingError as exc:
                return _json({"status": "conflict", "code": exc.code}, 409,
                             request_id)
            except Exception as exc:
                mapped = clerk_authenticator.map_error(exc, request_id)
                if mapped is not None:
                    return mapped
                logger.error("onboarding_github_request_failed",
                             extra={"error_category": "internal",
                                    "route_template": request.url.path})
                return _json({"status": "unavailable"}, 500, request_id)

        return wrapped

    return [
        Route("/api/onboarding/github/identity",
              clerk_handler(start_identity_link, write=True), methods=["POST"]),
        Route("/api/onboarding/github/install",
              clerk_handler(start_installation, write=True), methods=["POST"]),
        Route("/auth/github/link/callback", complete_identity_link,
              methods=["GET"]),
        Route("/github/setup", setup_redirect, methods=["GET"]),
    ]


# Imported late to avoid a cycle: onboarding_routes owns the JSON plumbing and
# the Clerk authenticator, and imports nothing from here.
from agent.api.onboarding_routes import _json, _request_id  # noqa: E402
