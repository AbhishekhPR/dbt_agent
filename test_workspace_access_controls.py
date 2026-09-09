from __future__ import annotations

import unittest

from agent.api.workspace_membership import (
    ClerkMembership,
    ClerkOrganizationSnapshot,
    WorkspaceAuthorizationContext,
)
from agent.workspace_access_controls import AccessControlBlocked, leave_workspace
from agent.api.clerk_identity import ClerkPrincipal


def _principal():
    return ClerkPrincipal(clerk_user_id="user_a", clerk_organization_id="org_a",
                          tenant_id="ten_a", factor_verification_age=(0, -1))


class _Authorizer:
    def __init__(self, role, owners): self.role, self.owners = role, owners
    def authorization_context(self, principal):
        return WorkspaceAuthorizationContext(
            tenant_id="ten_a", clerk_user_id="user_a", role=self.role,
            ownership_status="authoritative", active_owner_count=self.owners,
            sync_generation="g")


class _Clerk:
    def __init__(self): self.deleted = []
    def delete_organization_membership(self, organization_id, user_id):
        self.deleted.append((organization_id, user_id))
    def user_organization_memberships(self, user_id):
        if self.deleted:
            return ()
        return (_Membership(),)
    def organization_snapshot(self, organization_id):
        return ClerkOrganizationSnapshot(
            organization_id=organization_id, created_by_user_id=None,
            memberships=(ClerkMembership("user_a", "mem_a", "org:member"),
                         ClerkMembership("user_b", "mem_b", "org:owner")))


class _Membership:
    organization_id = "org_a"


class _Store:
    def __init__(self): self.completed, self.revoked = [], []
    def begin_workspace_departure(self, **values):
        return {"operation_id": "leave_1", **values}
    def revoke_workspace_departure_access(self, **values):
        self.revoked.append(values)
    def complete_workspace_departure(self, **values): self.completed.append(values)


class WorkspaceAccessControlTests(unittest.TestCase):
    def test_leave_requires_recent_non_impersonated_session(self):
        stale = ClerkPrincipal(clerk_user_id="user_a", clerk_organization_id="org_a",
                               tenant_id="ten_a", factor_verification_age=None)
        with self.assertRaises(AccessControlBlocked) as raised:
            leave_workspace(principal=stale, authorizer=_Authorizer("member", 1),
                            clerk_client=_Clerk(), store=_Store(),
                            clerk_organization_id="org_a")
        self.assertEqual(raised.exception.category, "recent_verification_required")
    def test_sole_owner_cannot_leave(self):
        clerk = _Clerk()
        with self.assertRaises(AccessControlBlocked) as raised:
            leave_workspace(principal=_principal(), authorizer=_Authorizer("owner", 1),
                            clerk_client=clerk, store=_Store(),
                            clerk_organization_id="org_a")
        self.assertEqual(raised.exception.category, "sole_owner")
        self.assertEqual(clerk.deleted, [])

    def test_member_and_nonsole_owner_can_leave_without_touching_workspace(self):
        for role, owners in (("member", 1), ("admin", 1), ("owner", 2)):
            clerk = _Clerk()
            store = _Store()
            result = leave_workspace(
                principal=_principal(), authorizer=_Authorizer(role, owners),
                clerk_client=clerk, store=store, clerk_organization_id="org_a")
            self.assertEqual(result["state"], "left")
            self.assertEqual(clerk.deleted, [("org_a", "user_a")])
            self.assertEqual(store.revoked, [{
                "operation_id": "leave_1",
                "clerk_organization_id": "org_a",
                "clerk_user_id": "user_a",
            }])


if __name__ == "__main__": unittest.main()
