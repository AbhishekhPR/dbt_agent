"""First-run onboarding: reading setup state, and creating the workspace.

Phase 1 of the onboarding backend. Two routes, both authenticated by a
server-verified Clerk session token:

    GET /api/onboarding/state   where this customer is in setup
    PUT /api/tenants            create or update their workspace

THE CHAIN THESE ROUTES IMPLEMENT
--------------------------------
    Clerk session token (browser)
      -> verified here, against Clerk's JWKS
        -> Clerk user id + active Clerk organization id
          -> Relium tenant, looked up by the organization id from the TOKEN
            -> durable onboarding state for that tenant

Every arrow is server-side. The browser contributes the token and nothing else.

WHY PUT ON A COLLECTION
-----------------------
``PUT /api/tenants`` rather than ``POST``: the resource's identity is the Clerk
organization in the verified token, not anything in the body. There is nothing
for a caller to choose, so the operation is naturally idempotent — the same
request twice yields the same tenant, which is exactly what going Back in the
setup UI and submitting again must do.

WHAT IS DELIBERATELY ABSENT
---------------------------
No GitHub installation state, repository list, dbt configuration, CI token or
completion route. Those are later phases. Where a response needs a field this
phase cannot honestly fill, it is null and documented as such — never a
plausible-looking default. ``"installed": false`` would be a claim about
GitHub that nothing here has checked.
"""
from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.api.auth import bearer_token
from agent.api.authorization import (
    ONBOARDING_READ, ONBOARDING_WRITE, CapabilityError, authorize,
)
from agent.api.clerk_identity import (
    ClerkKeysUnavailable, ClerkPrincipal, ClerkVerificationError,
)
from agent.api.validation import ValidationError, isoformat, optional_str, require_str

logger = logging.getLogger(__name__)

#: Bound on the setup form's free-text fields. Generous for a real name,
#: far short of anything worth storing as a payload.
MAX_ORGANIZATION_NAME = 200
MAX_ROLE = 100
MAX_TEAM_SIZE = 40

#: Pre-tenant steps. Neither is stored: both are derived per request from the
#: verified session, so they are absent from the CHECK constraint in migration
#: 0014, which governs only the steps a tenant can be at.
#:
#: They are two different problems and are deliberately not merged:
#:
#:   organization  The Clerk session has no active organization. Nothing can
#:                 be created until one exists, and the fix lives in CLERK —
#:                 the user picks or creates an organization there.
#:   workspace     There IS an active Clerk organization, but no Relium tenant
#:                 for it yet. The fix lives in RELIUM — PUT /api/tenants.
#:
#: Collapsing them into one step would send a user with no organization to a
#: form that cannot succeed, and there would be nothing on the response for a
#: frontend to branch on.
STEP_ORGANIZATION = "organization"
STEP_WORKSPACE = "workspace"

#: Stable, machine-readable reason returned when the session carries no active
#: Clerk organization. The frontend matches on this rather than on prose, and
#: sends the user to Clerk's organization selection/creation flow.
CODE_CLERK_ORGANIZATION_REQUIRED = "clerk_organization_required"

#: The Clerk organization is active but no Relium tenant exists yet, and the
#: requested operation needs one. Distinct from the organization code because
#: the fix is a different step, in a different system.
CODE_WORKSPACE_REQUIRED = "workspace_required"


class OnboardingAuthenticationError(Exception):
    """The Clerk credential is absent or not verifiable. Maps to 401."""


class OnboardingUnavailable(Exception):
    """Clerk cannot be reached, so no verdict is possible. Maps to 503."""


class ClerkAuthenticator:
    """Verifies a Clerk token and resolves it to a tenant-scoped principal.

    Extracted so there is exactly ONE place a Clerk session token is verified
    and exactly one place a tenant is resolved from it. The GitHub onboarding
    routes need the same thing, and a second copy of this logic is how the two
    drift until one of them forgets a check.
    """

    def __init__(self, verifier):
        self._verifier = verifier

    @property
    def configured(self):
        return self._verifier is not None

    def identity(self, request):
        """The verified Clerk identity behind this request, or raise."""
        if self._verifier is None:
            raise OnboardingUnavailable("Clerk authentication is not configured")

        presented = bearer_token(request.headers.get("Authorization"))
        try:
            return self._verifier.verify(presented)
        except ClerkVerificationError as exc:
            # Recorded for an operator; the caller learns only that it failed.
            # Naming the failed check would be an oracle for forging tokens.
            # No token or claim is logged.
            logger.info("onboarding_authentication_refused",
                        extra={"operation": "clerk_verify", "reason": str(exc)})
            raise OnboardingAuthenticationError(str(exc)) from None
        except ClerkKeysUnavailable as exc:
            # An outage is not a bad token. 401 here would sign every customer
            # out for the duration.
            logger.error("clerk_keys_unavailable",
                         extra={"error_category": "identity_provider"})
            raise OnboardingUnavailable(str(exc)) from None

    def principal(self, request, store, *, write, require_tenant=False):
        """Resolve to a ClerkPrincipal carrying the tenant, or raise.

        The tenant comes from the organization id inside the VERIFIED token and
        from nowhere else — never a path, query or body field.
        """
        identity = self.identity(request)
        tenant = None
        if identity.organization_id:
            tenant = store.tenant_by_clerk_organization(identity.organization_id)

        principal = ClerkPrincipal(
            clerk_user_id=identity.user_id,
            clerk_organization_id=identity.organization_id,
            tenant_id=tenant["tenant_id"] if tenant else None,
            clerk_session_id=identity.session_id,
        )
        capability = ONBOARDING_WRITE if write else ONBOARDING_READ
        try:
            authorize(principal, capability)
        except CapabilityError as exc:
            raise _Forbidden(str(exc)) from None

        if not principal.clerk_organization_id:
            raise _OrganizationRequired()
        if require_tenant and principal.tenant_id is None:
            raise _WorkspaceRequired()
        return principal

    def map_error(self, exc, request_id):
        """Render the shared failure modes, or None if this is not one of them."""
        if isinstance(exc, OnboardingAuthenticationError):
            return _json({"status": "unauthorized"}, 401, request_id)
        if isinstance(exc, OnboardingUnavailable):
            return _json({"status": "unavailable"}, 503, request_id)
        if isinstance(exc, _Forbidden):
            return _json({"status": "forbidden", "detail": str(exc)}, 403,
                         request_id)
        if isinstance(exc, _OrganizationRequired):
            return _json({"status": "conflict",
                          "code": CODE_CLERK_ORGANIZATION_REQUIRED,
                          "detail": "no active Clerk organization on this session"},
                         409, request_id)
        if isinstance(exc, _WorkspaceRequired):
            return _json({"status": "conflict", "code": CODE_WORKSPACE_REQUIRED,
                          "detail": "create the Relium workspace first"},
                         409, request_id)
        if isinstance(exc, ValidationError):
            return _json(exc.as_dict(), 422, request_id)
        if isinstance(exc, _BadRequest):
            return _json({"status": "invalid_request", "detail": str(exc)}, 400,
                         request_id)
        return None


def create_onboarding_routes(*, store_pool, clerk_verifier=None):
    """Build the onboarding route table.

    ``clerk_verifier`` is None when this deployment has no Clerk configuration.
    The routes are still registered — an endpoint that vanishes when
    misconfigured is indistinguishable from one that was never deployed, and
    the API contract test requires the served table to be stable — but they
    answer 503 rather than authenticating anybody.
    """

    def _authenticate(request):
        """Resolve the caller into a ClerkPrincipal, or refuse.

        ONLY a Clerk bearer token is accepted here. A dashboard session cookie
        and a ``rlm_`` service token are different principals with different
        authority and are not consulted: a machine credential must not be able
        to mint a tenant, and the existing GitHub session is scoped to a
        repository that does not exist yet during first-run setup.
        """
        if clerk_verifier is None:
            raise OnboardingUnavailable("Clerk authentication is not configured")

        presented = bearer_token(request.headers.get("Authorization"))
        try:
            identity = clerk_verifier.verify(presented)
        except ClerkVerificationError as exc:
            # The reason is recorded for an operator; the caller is told only
            # that it failed. Naming the failed check would turn this into an
            # oracle for forging tokens. No token or claim is logged.
            logger.info("onboarding_authentication_refused",
                        extra={"operation": "clerk_verify", "reason": str(exc)})
            raise OnboardingAuthenticationError(str(exc)) from None
        except ClerkKeysUnavailable as exc:
            # An outage is not a bad token. Answering 401 here would sign every
            # customer out for the duration.
            logger.error("clerk_keys_unavailable",
                         extra={"error_category": "identity_provider"})
            raise OnboardingUnavailable(str(exc)) from None

        return identity

    def _principal_for(store, identity, *, capability):
        """Attach the Relium tenant to a verified Clerk identity.

        The tenant is resolved from ``identity.organization_id`` — which came
        out of the verified token — and never from the path, query or body.
        """
        tenant = None
        if identity.organization_id:
            tenant = store.tenant_by_clerk_organization(identity.organization_id)

        principal = ClerkPrincipal(
            clerk_user_id=identity.user_id,
            clerk_organization_id=identity.organization_id,
            tenant_id=tenant["tenant_id"] if tenant else None,
            clerk_session_id=identity.session_id,
        )
        try:
            authorize(principal, capability)
        except CapabilityError as exc:
            raise _Forbidden(str(exc)) from None
        return principal, tenant

    # -- handlers ---------------------------------------------------------

    def get_state(request, body, store):
        identity = request.state.clerk_identity
        _, tenant = _principal_for(store, identity, capability=ONBOARDING_READ)

        if identity.organization_id is None:
            # Signed in to Clerk, but no organization is active on the session.
            #
            # Clerk owns this. The application uses Organizations, so a new
            # user may still need to create, join or select one as part of
            # Clerk's own flow — and Relium must not paper over that by
            # inventing an organization on their behalf. The frontend sends
            # them to Clerk, and returns here with a refreshed token.
            return 200, {
                "complete": False,
                "current_step": STEP_ORGANIZATION,
                "code": CODE_CLERK_ORGANIZATION_REQUIRED,
                "workspace": None,
                "github": None,
                "configuration": None,
            }

        if tenant is None:
            # There IS an active Clerk organization; Relium has no tenant for
            # it yet. This is the genuine first-run workspace state, and the
            # only legitimate way to hold a verified identity with an
            # organization and still have no tenant.
            return 200, {
                "complete": False,
                "current_step": STEP_WORKSPACE,
                "workspace": None,
                "github": None,
                "configuration": None,
            }

        # The GitHub section is now factual: it reports bindings that survived
        # the three verifications in agent/api/github_installation.py, and
        # nothing else. An installation that only a browser has asserted does
        # not appear here, because it was never written.
        from agent.api.github_installation import (
            github_identity_payload, installations_payload,
        )

        github = installations_payload(store, tenant["tenant_id"])
        # Whether the human has proved a GitHub identity yet. Required before
        # an installation can be bound, so the UI can ask for it first rather
        # than after the customer has already installed the App.
        github["identity"] = github_identity_payload(
            store, identity.user_id)

        return 200, {
            "complete": tenant.get("completed_at") is not None,
            "current_step": tenant.get("current_step") or "github",
            "workspace": _workspace_payload(tenant),
            "github": github,
            # Phase 3. Null rather than a default, because a shape here would
            # assert a dbt configuration that nothing has checked.
            "configuration": None,
        }

    def put_tenant(request, body, store):
        identity = request.state.clerk_identity
        principal, _ = _principal_for(store, identity, capability=ONBOARDING_WRITE)

        if not principal.clerk_organization_id:
            # NOT a validation failure — the body may be perfectly well formed.
            # The SESSION is not in a state where a workspace can exist yet, so
            # this is a conflict with current state, and it carries a stable
            # code the frontend can branch on.
            #
            # Relium does not create the Clerk organization. Doing so would
            # need a Clerk Secret Key in this backend, and it would produce a
            # second organization for a user Clerk may already have prompted —
            # exactly the duplicate this phase exists to avoid.
            raise _OrganizationRequired()

        # require_str already strips and refuses an empty result, so a name of
        # whitespace is rejected rather than stored.
        organization_name = require_str(
            body, "organization_name", max_length=MAX_ORGANIZATION_NAME)
        role = optional_str(body, "role", max_length=MAX_ROLE)
        team_size = optional_str(body, "team_size", max_length=MAX_TEAM_SIZE)

        # clerk_organization_id is NOT read from the body. Accepting it there
        # would let any caller create or rename another organization's tenant.
        tenant = store.upsert_tenant_for_clerk_organization(
            principal.clerk_organization_id,
            organization_name=organization_name,
            role=role,
            team_size=team_size,
        )
        return 200, _workspace_payload(tenant)

    # -- plumbing ---------------------------------------------------------

    # The capability is applied by each handler through `_principal_for`, not
    # here: it has to be checked AFTER the tenant is resolved, and passing it
    # to this wrapper as well would give the signature the appearance of
    # enforcing something it does not.
    def handler(fn, *, write):
        async def wrapped(request):
            request_id = _request_id(request)
            try:
                identity = _authenticate(request)
                request.state.clerk_identity = identity

                body = None
                if write:
                    body = await _read_json(request)

                def work():
                    with store_pool.acquire() as store:
                        return fn(request, body, store)

                status, payload = await run_in_threadpool(work)
                return _json(payload, status, request_id)
            except OnboardingAuthenticationError:
                return _json({"status": "unauthorized"}, 401, request_id)
            except OnboardingUnavailable:
                return _json({"status": "unavailable"}, 503, request_id)
            except _Forbidden as exc:
                return _json({"status": "forbidden", "detail": str(exc)}, 403, request_id)
            except _OrganizationRequired:
                # 409: the request is fine, the session state is not. The code
                # is what routes the user into Clerk's organization flow.
                return _json({
                    "status": "conflict",
                    "code": CODE_CLERK_ORGANIZATION_REQUIRED,
                    "detail": "no active Clerk organization on this session",
                }, 409, request_id)
            except ValidationError as exc:
                return _json(exc.as_dict(), 422, request_id)
            except _BadRequest as exc:
                return _json({"status": "invalid_request", "detail": str(exc)},
                             400, request_id)
            except Exception as exc:
                if type(exc).__name__ in ("UniqueViolation", "ForeignKeyViolation",
                                          "IntegrityError", "CheckViolation"):
                    # A constraint refused the write. Reported as a conflict
                    # rather than a 500 because it is an expected outcome of
                    # the tenancy rules, not an internal fault. The detail is
                    # ours, not the driver's: a database message can name
                    # columns and values.
                    logger.info("onboarding_integrity_conflict",
                                extra={"error_category": "scoped_conflict",
                                       "route_template": request.url.path})
                    return _json({"status": "conflict",
                                  "detail": "this workspace could not be created"},
                                 409, request_id)
                logger.error("onboarding_request_failed",
                             extra={"error_category": "internal",
                                    "route_template": request.url.path})
                return _json({"status": "unavailable"}, 500, request_id)

        return wrapped

    return [
        Route("/api/onboarding/state", handler(get_state, write=False),
              methods=["GET"]),
        Route("/api/tenants", handler(put_tenant, write=True),
              methods=["PUT"]),
    ]


class _Forbidden(Exception):
    """Authenticated, but lacking the capability."""


class _WorkspaceRequired(Exception):
    """The tenant does not exist yet and this operation needs it."""


class _OrganizationRequired(Exception):
    """Verified Clerk session with no active organization.

    A distinct outcome from both "unauthenticated" and "invalid request": the
    caller is who they say they are and asked for something reasonable, but no
    Relium tenant can exist until Clerk has an active organization for them.
    """


class _BadRequest(Exception):
    """The request itself is unusable, before any field is validated."""


def _workspace_payload(tenant):
    """The workspace as the API exposes it.

    ``clerk_organization_id`` is echoed deliberately: the caller already knows
    it — it came from their own token — and returning it lets a client confirm
    which workspace it is looking at. Nothing else about Clerk is exposed.
    """
    return {
        "id": tenant["tenant_id"],
        "clerk_organization_id": tenant["clerk_organization_id"],
        "organization_name": tenant["organization_name"],
        "role": tenant.get("role"),
        "team_size": tenant.get("team_size"),
        "created_at": isoformat(tenant.get("created_at")),
        "updated_at": isoformat(tenant.get("updated_at")),
    }


#: Bound on an onboarding request body. These carry three short strings; a
#: megabyte of JSON is not a setup form.
MAX_BODY_BYTES = 16 * 1024


async def _read_json(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise _BadRequest("request body is too large")
        body.extend(chunk)
    if not body:
        raise _BadRequest("a JSON object body is required")
    import json as _json_module

    try:
        document = _json_module.loads(bytes(body))
    except (ValueError, UnicodeDecodeError):
        raise _BadRequest("request body must be valid JSON") from None
    if not isinstance(document, dict):
        raise _BadRequest("request body must be a JSON object")
    return document


def _request_id(request):
    import uuid

    presented = request.headers.get("X-Request-Id")
    if isinstance(presented, str) and 0 < len(presented) <= 200 and presented.isprintable():
        return presented
    return uuid.uuid4().hex


def _json(payload, status, request_id):
    document = dict(payload)
    document.setdefault("request_id", request_id)
    return JSONResponse(document, status_code=status,
                        headers={"X-Request-Id": request_id})
