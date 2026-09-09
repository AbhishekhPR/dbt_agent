"""Clerk-authenticated Settings lifecycle endpoints.

Tenant and user authority is always resolved from the verified token. Request
bodies carry confirmation text or subordinate resource ids only.
"""
from __future__ import annotations

import json
import logging
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.account_lifecycle import AccountLifecycleBlocked, AccountLifecycleEngine
from agent.api.clerk_identity import ClerkPrincipal
from agent.api.onboarding_routes import (
    ClerkAuthenticator, OnboardingAuthenticationError, OnboardingUnavailable,
)
from agent.api.workspace_membership import (
    WorkspaceMembershipAuthorizer, WorkspaceMembershipUnavailable,
    WorkspaceRoleDenied,
)
from agent.github_access_lifecycle import GitHubAccessBlocked, GitHubAccessLifecycle, disconnect_personal_github
from agent.workspace_access_controls import AccessControlBlocked, leave_workspace
from agent.workspace_collector_revocation import revoke_collector_access
from agent.workspace_deletion_lifecycle import LifecycleBlocked, WorkspaceDeletionEngine


logger = logging.getLogger(__name__)
MAX_BODY = 4096


def create_lifecycle_routes(*, store_pool, clerk_verifier, clerk_client,
                            polar_client, github_client, github_app_jwt,
                            repository_storage):
    authenticator = ClerkAuthenticator(clerk_verifier)

    def principal_for(request, store, *, require_workspace=True):
        if require_workspace:
            return authenticator.principal(request, store, write=True,
                                           require_tenant=True)
        identity = authenticator.identity(request)
        tenant = (store.tenant_by_clerk_organization(identity.organization_id)
                  if identity.organization_id else None)
        return ClerkPrincipal(
            clerk_user_id=identity.user_id,
            clerk_organization_id=identity.organization_id,
            tenant_id=tenant["tenant_id"] if tenant else None,
            clerk_session_id=identity.session_id,
            clerk_organization_role=identity.organization_role,
            factor_verification_age=identity.factor_verification_age,
            clerk_token_issued_at=identity.issued_at,
            is_impersonated=identity.is_impersonated)

    def authorizer_for(store):
        if clerk_client is None:
            raise OnboardingUnavailable("Clerk management is not configured")
        return WorkspaceMembershipAuthorizer(store=store, source=clerk_client)

    def workspace_engine(store, authorizer):
        if polar_client is None or github_client is None or github_app_jwt is None:
            raise OnboardingUnavailable("lifecycle providers are not configured")
        return WorkspaceDeletionEngine(
            authorizer=authorizer, store=store, polar_client=polar_client,
            github_client=github_client, github_app_jwt=github_app_jwt,
            clerk_client=clerk_client,
            repository_storage=(repository_storage.root
                                if repository_storage is not None else None))

    def capability(request, body, store):
        principal = principal_for(request, store)
        authorizer = authorizer_for(store)
        context = authorizer.authorization_context(principal)
        tenant = store.tenant_by_id(context.tenant_id)
        access = store.tenant_lifecycle_access_inventory(context.tenant_id)
        return 200, {
            "workspace": {"name": tenant["organization_name"],
                          "role": context.role,
                          "active_owner_count": context.active_owner_count},
            "capabilities": {
                "delete_workspace": context.role == "owner",
                "leave_workspace": (context.role != "owner"
                                    or context.active_owner_count > 1),
                "manage_workspace_access": context.role in {"owner", "admin"},
                "uninstall_github": context.role == "owner",
            },
            "notices": {
                "warehouse": "Rotate or drop the warehouse role and remove the collector configuration.",
                "github_actions": "Remove RELIUM_CI_TOKEN from GitHub Actions after Relium-side revocation.",
                "external_history": "Historical GitHub comments/checks and Slack publications remain in those providers.",
            },
            "github": {"repositories": access["repositories"],
                       "installations": access["installations"]},
            "collector_tokens": access["collector_tokens"],
        }

    def request_workspace_delete(request, body, store):
        principal = principal_for(request, store)
        authorizer = authorizer_for(store)
        engine = workspace_engine(store, authorizer)
        operation = engine.request(principal, confirmation=_required(body, "confirmation"))
        return 202, _operation(operation)

    def advance_workspace_delete(request, body, store):
        principal = principal_for(request, store)
        authorizer = authorizer_for(store)
        engine = workspace_engine(store, authorizer)
        return 200, _operation(engine.advance(
            principal, request.path_params["operation_id"]))

    def request_account_delete(request, body, store):
        principal = principal_for(request, store, require_workspace=False)
        authorizer_for(store)
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk_client)
        return 202, _operation(engine.request(
            principal, confirmation=_required(body, "confirmation")))

    def advance_account_delete(request, body, store):
        principal = principal_for(request, store, require_workspace=False)
        authorizer_for(store)
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk_client)
        return 200, _operation(engine.advance(
            principal, request.path_params["operation_id"]))

    def leave(request, body, store):
        principal = principal_for(request, store)
        authorizer = authorizer_for(store)
        return 200, leave_workspace(
            principal=principal, authorizer=authorizer, clerk_client=clerk_client,
            store=store, clerk_organization_id=principal.clerk_organization_id)

    def personal_disconnect(request, body, store):
        principal = principal_for(request, store, require_workspace=False)
        return 200, disconnect_personal_github(principal=principal, store=store)

    def github_action(request, body, store, kind):
        principal = principal_for(request, store)
        authorizer = authorizer_for(store)
        if github_client is None or github_app_jwt is None:
            raise OnboardingUnavailable("GitHub lifecycle is not configured")
        lifecycle = GitHubAccessLifecycle(
            authorizer=authorizer, store=store, github_client=github_client,
            github_app_jwt=github_app_jwt)
        value = int(request.path_params[
            "repository_id" if kind == "repository" else "installation_id"])
        if value <= 0:
            raise ValueError("invalid_resource_id")
        if kind == "repository":
            result = lifecycle.disconnect_repository(
                principal=principal, repository_id=value)
        elif kind == "installation":
            result = lifecycle.disconnect_installation(
                principal=principal, installation_id=value)
        else:
            result = lifecycle.uninstall(principal=principal,
                                         installation_id=value)
        return 200, _operation(result)

    def collector_action(request, body, store, token_id):
        principal = principal_for(request, store)
        authorizer = authorizer_for(store)
        result = revoke_collector_access(
            principal=principal, authorizer=authorizer, store=store,
            token_id=token_id)
        return 200, result

    def handler(fn, *, body=False):
        async def wrapped(request):
            request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
            try:
                document = await _read_body(request) if body else None
                def work():
                    with store_pool.acquire() as store:
                        return fn(request, document, store)
                status, payload = await run_in_threadpool(work)
                return _json(payload, status, request_id)
            except (OnboardingAuthenticationError,):
                return _json({"status": "unauthorized"}, 401, request_id)
            except OnboardingUnavailable:
                return _json({"status": "unavailable"}, 503, request_id)
            except WorkspaceRoleDenied:
                return _json({"status": "forbidden"}, 403, request_id)
            except WorkspaceMembershipUnavailable:
                return _json({"status": "blocked", "code": "membership_authority_unavailable"}, 409, request_id)
            except (LifecycleBlocked, AccountLifecycleBlocked,
                    AccessControlBlocked, GitHubAccessBlocked) as exc:
                status = 403 if exc.category in {"recent_verification_required", "impersonated_session"} \
                    else 409
                return _json({"status": exc.disposition, "code": exc.category}, status, request_id)
            except (ValueError, KeyError, TypeError):
                return _json({"status": "invalid_request"}, 422, request_id)
            except Exception:
                logger.error("lifecycle_request_failed",
                             extra={"error_category": "internal",
                                    "route_template": request.url.path})
                return _json({"status": "unavailable"}, 500, request_id)
        return wrapped

    return [
        Route("/api/settings/lifecycle", handler(capability), methods=["GET"]),
        Route("/api/lifecycle/workspace-deletion", handler(request_workspace_delete, body=True), methods=["POST"]),
        Route("/api/lifecycle/workspace-deletion/{operation_id}/advance", handler(advance_workspace_delete, body=True), methods=["POST"]),
        Route("/api/lifecycle/account-deletion", handler(request_account_delete, body=True), methods=["POST"]),
        Route("/api/lifecycle/account-deletion/{operation_id}/advance", handler(advance_account_delete, body=True), methods=["POST"]),
        Route("/api/lifecycle/workspace/leave", handler(leave, body=True), methods=["POST"]),
        Route("/api/lifecycle/github/personal/disconnect", handler(personal_disconnect, body=True), methods=["POST"]),
        Route("/api/lifecycle/github/repositories/{repository_id}/disconnect", handler(lambda r,b,s: github_action(r,b,s,"repository"), body=True), methods=["POST"]),
        Route("/api/lifecycle/github/installations/{installation_id}/disconnect", handler(lambda r,b,s: github_action(r,b,s,"installation"), body=True), methods=["POST"]),
        Route("/api/lifecycle/github/installations/{installation_id}/uninstall", handler(lambda r,b,s: github_action(r,b,s,"uninstall"), body=True), methods=["POST"]),
        Route("/api/lifecycle/collector-tokens/{token_id}/revoke", handler(lambda r,b,s: collector_action(r,b,s,r.path_params["token_id"]), body=True), methods=["POST"]),
        Route("/api/lifecycle/collector-access/revoke-all", handler(lambda r,b,s: collector_action(r,b,s,None), body=True), methods=["POST"]),
    ]


async def _read_body(request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("body_too_large")
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("body_must_be_object")
    forbidden = {"tenant_id", "user_id", "clerk_user_id", "role"} & value.keys()
    if forbidden:
        raise ValueError("untrusted_scope")
    return value


def _required(body, name):
    value = body.get(name) if isinstance(body, dict) else None
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("invalid_confirmation")
    return value


def _operation(row):
    allowed = ("operation_id", "phase", "disposition", "failure_category",
               "state", "receipt_id", "artifact_files_deleted",
               "operational_records_deleted")
    return {key: row[key] for key in allowed if key in row}


def _json(payload, status, request_id):
    response = JSONResponse(payload, status_code=status)
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return response
