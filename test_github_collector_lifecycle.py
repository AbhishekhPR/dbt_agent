from __future__ import annotations

import unittest

from agent.api.workspace_membership import WorkspaceAuthorizationContext
from agent.github_access_lifecycle import (
    GitHubAccessLifecycle, GitHubAccessBlocked, disconnect_personal_github,
)
from agent.workspace_collector_revocation import revoke_collector_access
from agent.github_app.client import GitHubNotFoundError
from agent.api.clerk_identity import ClerkPrincipal


def _principal():
    return ClerkPrincipal(clerk_user_id="user_a", clerk_organization_id="org_a",
                          tenant_id="ten_a", factor_verification_age=(0, -1))


class _Auth:
    def require_admin_or_owner(self, principal):
        return WorkspaceAuthorizationContext("ten_a", "user_a", "admin",
                                             "authoritative", 1, "g")
    def require_owner(self, principal):
        return WorkspaceAuthorizationContext("ten_a", "user_a", "owner",
                                             "authoritative", 1, "g")


class _Store:
    def __init__(self): self.calls = []; self.op = None
    def revoke_current_user_github_identity(self, user):
        self.calls.append(("personal", user)); return {"state": "revoked"}
    def revoke_tenant_collector_access(self, **values):
        self.calls.append(("collector", values)); return {"state": "revoked", "tokens_revoked": 1}
    def begin_github_access_operation(self, **values):
        self.calls.append(("begin", values)); self.op = {
            "operation_id": "gha_1", "tenant_id": values["tenant_id"],
            "phase": "local_revoked", "operation_kind": values["operation_kind"],
            "github_installation_id": values.get("github_installation_id")}
        return dict(self.op)
    def complete_github_access_operation(self, **values):
        self.calls.append(("complete", values)); return {"state": "completed"}
    def fail_github_access_operation(self, **values): self.calls.append(("fail", values))


class _GitHub:
    def __init__(self): self.deleted = []
    def delete_installation(self, installation_id, jwt): self.deleted.append(installation_id)
    def get_installation(self, installation_id, jwt): raise GitHubNotFoundError("absent", status_code=404)


class GitHubCollectorLifecycleTests(unittest.TestCase):
    def test_every_access_revocation_rejects_stale_or_impersonated_session(self):
        store = _Store()
        stale = ClerkPrincipal(clerk_user_id="user_a", clerk_organization_id="org_a",
                               tenant_id="ten_a", factor_verification_age=None)
        impersonated = ClerkPrincipal(
            clerk_user_id="user_a", clerk_organization_id="org_a",
            tenant_id="ten_a", factor_verification_age=(0, -1),
            is_impersonated=True)
        with self.assertRaises(GitHubAccessBlocked):
            disconnect_personal_github(principal=stale, store=store)
        lifecycle = GitHubAccessLifecycle(authorizer=_Auth(), store=store,
                                          github_client=_GitHub(),
                                          github_app_jwt=lambda: "jwt")
        with self.assertRaises(GitHubAccessBlocked):
            lifecycle.disconnect_repository(principal=impersonated,
                                            repository_id=1)
        from agent.workspace_collector_revocation import CollectorAccessBlocked
        with self.assertRaises(CollectorAccessBlocked):
            revoke_collector_access(principal=stale, authorizer=_Auth(),
                                    store=store)
    def test_personal_disconnect_is_user_scoped(self):
        store = _Store()
        result = disconnect_personal_github(principal=_principal(), store=store)
        self.assertEqual(result["state"], "revoked")
        self.assertEqual(store.calls, [("personal", "user_a")])

    def test_collector_revoke_uses_authorized_tenant_not_browser_scope(self):
        store = _Store()
        result = revoke_collector_access(principal=_principal(), authorizer=_Auth(),
                                         store=store, token_id="tok_1")
        self.assertEqual(result["state"], "revoked")
        self.assertEqual(store.calls[0][1]["tenant_id"], "ten_a")
        self.assertIn("rotate_or_drop_warehouse_role", result["customer_remediation"])

    def test_uninstall_revokes_locally_then_requires_verified_absence(self):
        store, github = _Store(), _GitHub()
        lifecycle = GitHubAccessLifecycle(authorizer=_Auth(), store=store,
                                          github_client=github,
                                          github_app_jwt=lambda: "jwt")
        result = lifecycle.uninstall(principal=_principal(), installation_id=9)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(github.deleted, [9])
        self.assertEqual([call[0] for call in store.calls], ["begin", "complete"])


if __name__ == "__main__": unittest.main()
