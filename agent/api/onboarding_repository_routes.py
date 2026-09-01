"""Repository, dbt configuration, CI credential and completion routes.

Phase 3. Five Clerk-authenticated routes:

    GET  /api/onboarding/repositories                 what this tenant may use
    PUT  /api/onboarding/repositories/{repository_id} select one
    PUT  /api/onboarding/dbt                          project_dir + manifest_path
    POST /api/onboarding/ci-token                     issue the CI credential
    POST /api/onboarding/complete                     finish, idempotently

###################################################################
# THE PATH PARAMETER IS A CLAIM, NOT AN AUTHORIZATION.            #
###################################################################

``{repository_id}`` arrives from a browser and is treated exactly as such:
every handler resolves it through
``RepositoryOnboardingService.authorized_repository``, which checks it against
the repositories GitHub returns for THIS tenant's own installation. There is no
route here that touches a repository without going through that function.

An id that is unknown, belongs to another tenant, or sits outside the
installation all produce the same 404 — matching the non-disclosure policy in
agent/api/routes.py, where "an out-of-scope resource is indistinguishable from
one that does not exist".

No route reads a tenant id from a path, query or body. The tenant comes from
the verified Clerk token, via the shared ClerkAuthenticator.
"""
from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from agent.api.dashboard_bridge import DashboardBridgeError
from agent.api.repository_onboarding import (
    CODE_REPOSITORY_NOT_FOUND, RepositoryOnboardingError, configuration_payload,
    repositories_payload,
)
from agent.api.validation import isoformat

logger = logging.getLogger(__name__)

#: Bound on an onboarding request body. These carry a handful of short strings.
MAX_BODY_BYTES = 16 * 1024

#: Codes that mean "you may not have this", rendered as 404 rather than 403 so
#: the response cannot be used to probe for private repositories.
NOT_FOUND_CODES = frozenset({CODE_REPOSITORY_NOT_FOUND})


def create_onboarding_repository_routes(*, store_pool, clerk_authenticator,
                                        service=None, api_url="",
                                        dashboard_bridge=None,
                                        secure_cookies=True,
                                        billing_settings=None):
    """Build the routes. ``service`` is None when GitHub is not configured.

    ``billing_settings`` is the Polar configuration, or None on a deployment
    that has none. It is passed to the entitlement resolver rather than read
    from the environment here, so what a workspace may do is a function of the
    configuration this application was built with — see
    ``agent.billing.access.get_workspace_entitlements``.
    """
    from agent.billing.access import get_workspace_entitlements

    def _entitlements(store, principal):
        return get_workspace_entitlements(
            store, principal.tenant_id, billing_settings)

    def _repository_id(request):
        """Read the path parameter as an integer, or refuse.

        Refusing here with the same not-found code the authorization check uses
        means a malformed id and an unauthorized id are indistinguishable.
        """
        raw = request.path_params.get("repository_id")
        if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 20:
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)
        value = int(raw)
        if value <= 0:
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)
        return value

    # -- handlers ----------------------------------------------------------

    def list_repositories(request, body, store, principal):
        repositories = service.list_repositories(store, principal.tenant_id)
        return 200, {"repositories": repositories_payload(
            store, principal.tenant_id, repositories)}

    def select_repository(request, body, store, principal):
        record = service.select_repository(
            store, principal.tenant_id, _repository_id(request),
            repository_limit=_entitlements(store, principal).repository_limit)
        return 200, {
            "repository_id": record["github_repository_id"],
            "full_name": f"{record['owner_login']}/{record['name']}",
            "default_branch": record.get("default_branch"),
            "dbt_detected": record.get("dbt_detected"),
            "dbt_project_dir": record.get("dbt_project_dir"),
            "selected_at": isoformat(record.get("selected_at")),
        }

    def configure_dbt(request, body, store, principal):
        repository_id = body.get("repository_id")
        if isinstance(repository_id, bool) or not isinstance(repository_id, int):
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)

        service.configure_repository(
            store, principal.tenant_id, repository_id,
            project_dir=body.get("project_dir"),
            manifest_path=body.get("manifest_path"),
            enforcement_mode=body.get("enforcement_mode"),
            merge_blocking=_entitlements(store, principal).merge_blocking)
        # Re-read through the payload builder so the response is exactly what
        # a later GET /api/onboarding/state will report, rather than a
        # separately-assembled view that could drift from it.
        return 200, configuration_payload(store, principal.tenant_id,
                                          api_url=api_url)

    def issue_ci_token(request, body, store, principal):
        repository_id = body.get("repository_id")
        if isinstance(repository_id, bool) or not isinstance(repository_id, int):
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)

        credential = service.issue_ci_credential(
            store, principal.tenant_id, repository_id,
            force=bool(body.get("force")))

        payload = {
            "token_id": credential.token_id,
            "delivery": credential.delivery,
            "secret_name": credential.secret_name,
            "ci_token_issued": True,
        }
        if credential.secret is not None:
            # The ONLY response in the entire API that carries the value, and
            # only on the fallback path where Relium could not write the
            # repository secret itself. It is not stored, not logged, and not
            # returned again — a second call without `force` returns the id
            # alone.
            payload["token"] = credential.secret
            payload["instructions"] = (
                f"Create a repository secret named {credential.secret_name} "
                "with this value. It is shown once and cannot be recovered.")
        return 200, payload

    def establish_dashboard_session(request, body, store, principal):
        """Exchange a verified Clerk session for the ordinary dashboard session.

        Takes NO input. Every value is resolved from the verified token and the
        tenant's own records, so there is nothing in the request for a caller
        to substitute.

        The response is minimal on purpose: whether it worked, and the
        authority GitHub reported. The session itself leaves as an HttpOnly
        cookie the browser cannot read, and no GitHub credential is returned.
        """
        from agent.api.dashboard_bridge import DashboardBridgeError

        state = store.onboarding_state_for_tenant(principal.tenant_id)
        if state is None or state.get("completed_at") is None:
            # Entering the dashboard is what finishing setup earns. Allowing it
            # earlier would make the completion checks bypassable by skipping
            # to this route.
            raise DashboardBridgeError("onboarding_incomplete")

        established = dashboard_bridge.establish(
            store, clerk_user_id=principal.clerk_user_id,
            tenant_id=principal.tenant_id)
        return 200, {
            "established": True,
            # Display and capability, exactly what /auth/session already
            # returns. No token, no repository id, no tenant id.
            "login": established["login"],
            "may_govern": established["may_govern"],
            "github_permission": established["github_permission"],
            "_session": established,   # stripped before the response is sent
        }

    def complete_onboarding(request, body, store, principal):
        result = service.complete_onboarding(
            store, principal.tenant_id, principal.clerk_user_id)
        return 200, {
            "complete": True,
            "completed_at": isoformat(result["completed_at"]),
            "repository_id": result.get("repository_id"),
        }

    # -- plumbing ----------------------------------------------------------

    def handler(fn, *, write, sets_session=False):
        async def wrapped(request):
            request_id = _request_id(request)
            required = dashboard_bridge if sets_session else service
            if required is None:
                return _json({"status": "unavailable",
                              "code": "github_app_not_configured"}, 503,
                             request_id)
            try:
                body = await _read_json(request) if write else None

                def work():
                    with store_pool.acquire() as store:
                        # require_tenant: every route here needs a workspace,
                        # and saying so once is better than five copies of the
                        # same check that one of them could omit.
                        principal = clerk_authenticator.principal(
                            request, store, write=write, require_tenant=True)
                        return fn(request, body, store, principal)

                status, payload = await run_in_threadpool(work)
                session = payload.pop("_session", None) if sets_session else None
                response = _json(payload, status, request_id)
                if session is not None:
                    _set_session_cookies(response, session,
                                         secure=secure_cookies)
                return response
            except DashboardBridgeError as exc:
                # A refusal here is a conflict with current state, not a bad
                # request: the caller is who they say they are and asked for
                # something reasonable. The code says what is missing.
                return _json({"status": "conflict", "code": exc.code}, 409,
                             request_id)
            except RepositoryOnboardingError as exc:
                if exc.code in NOT_FOUND_CODES:
                    # Non-disclosing. No detail: the caller learns nothing
                    # about whether the repository exists.
                    return _json({"status": "not_found",
                                  "code": exc.code}, 404, request_id)
                return _json({"status": "conflict", "code": exc.code}, 409,
                             request_id)
            except Exception as exc:
                mapped = clerk_authenticator.map_error(exc, request_id)
                if mapped is not None:
                    return mapped
                if type(exc).__name__ in ("UniqueViolation", "ForeignKeyViolation",
                                          "IntegrityError", "CheckViolation"):
                    logger.info("onboarding_repository_conflict",
                                extra={"error_category": "scoped_conflict",
                                       "route_template": request.url.path})
                    return _json({"status": "not_found",
                                  "code": CODE_REPOSITORY_NOT_FOUND}, 404,
                                 request_id)
                logger.error("onboarding_repository_request_failed",
                             extra={"error_category": "internal",
                                    "route_template": request.url.path})
                return _json({"status": "unavailable"}, 500, request_id)

        return wrapped

    return [
        Route("/api/onboarding/repositories",
              handler(list_repositories, write=False), methods=["GET"]),
        Route("/api/onboarding/repositories/{repository_id}",
              handler(select_repository, write=True), methods=["PUT"]),
        Route("/api/onboarding/dbt", handler(configure_dbt, write=True),
              methods=["PUT"]),
        Route("/api/onboarding/ci-token", handler(issue_ci_token, write=True),
              methods=["POST"]),
        Route("/api/onboarding/complete",
              handler(complete_onboarding, write=True), methods=["POST"]),
        Route("/api/onboarding/dashboard-session",
              handler(establish_dashboard_session, write=True,
                      sets_session=True),
              methods=["POST"]),
    ]


def _set_session_cookies(response, established, *, secure):
    """Write the SAME cookies /auth/github/callback writes.

    Identical names, flags and lifetimes, because this is the same session.
    The session id is HttpOnly so script cannot read it; the CSRF token is
    readable because the dashboard has to echo it on mutations, and it is
    useless without the session cookie.

    SameSite=lax matches the existing model. Neither cookie, nor anything that
    could reconstruct one, appears in the response body or a redirect URL.
    """
    from agent.api.sessions import CSRF_COOKIE, SESSION_COOKIE, SESSION_LIFETIME

    max_age = int(SESSION_LIFETIME.total_seconds())
    response.set_cookie(SESSION_COOKIE, established["session_id"],
                        max_age=max_age, httponly=True, secure=secure,
                        samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, established["csrf_token"],
                        max_age=max_age, httponly=False, secure=secure,
                        samesite="lax", path="/")


async def _read_json(request):
    from agent.api.onboarding_routes import _BadRequest

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise _BadRequest("request body is too large")
        body.extend(chunk)
    if not body:
        return {}
    import json as json_module

    try:
        document = json_module.loads(bytes(body))
    except (ValueError, UnicodeDecodeError):
        raise _BadRequest("request body must be valid JSON") from None
    if not isinstance(document, dict):
        raise _BadRequest("request body must be a JSON object")
    return document


# Imported late: onboarding_routes owns the JSON plumbing and the Clerk
# authenticator, and imports nothing from here.
from agent.api.onboarding_routes import _json, _request_id  # noqa: E402
