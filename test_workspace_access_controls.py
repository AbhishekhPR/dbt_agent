from __future__ import annotations

import unittest

from agent.api.workspace_membership import WorkspaceAuthorizationContext
from agent.workspace_access_controls import AccessControlBlocked, leave_workspace


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
    def user_organization_memberships(self, user_id): return ()


class _Store:
    def __init__(self): self.completed = []
    def begin_workspace_departure(self, **values):
        return {"operation_id": "leave_1", **values}
    def complete_workspace_departure(self, **values): self.completed.append(values)


class WorkspaceAccessControlTests(unittest.TestCase):
    def test_sole_owner_cannot_leave(self):
        clerk = _Clerk()
        with self.assertRaises(AccessControlBlocked) as raised:
            leave_workspace(principal=object(), authorizer=_Authorizer("owner", 1),
                            clerk_client=clerk, store=_Store(),
                            clerk_organization_id="org_a")
        self.assertEqual(raised.exception.category, "sole_owner")
        self.assertEqual(clerk.deleted, [])

    def test_member_and_nonsole_owner_can_leave_without_touching_workspace(self):
        for role, owners in (("member", 1), ("admin", 1), ("owner", 2)):
            clerk = _Clerk()
            result = leave_workspace(
                principal=object(), authorizer=_Authorizer(role, owners),
                clerk_client=clerk, store=_Store(), clerk_organization_id="org_a")
            self.assertEqual(result["state"], "left")
            self.assertEqual(clerk.deleted, [("org_a", "user_a")])


if __name__ == "__main__": unittest.main()
