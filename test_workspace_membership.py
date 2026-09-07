from __future__ import annotations

import unittest

from agent.api.workspace_membership import (
    ClerkMembership,
    ClerkOrganizationSnapshot,
    WorkspaceMembershipAuthorizer,
    WorkspaceMembershipUnavailable,
    WorkspaceRoleDenied,
    project_memberships,
)
from agent.api.clerk_identity import ClerkPrincipal


class MembershipProjectionTests(unittest.TestCase):
    def snapshot(self, *memberships, creator="user_creator"):
        return ClerkOrganizationSnapshot(
            organization_id="org_2acme",
            created_by_user_id=creator,
            memberships=tuple(memberships),
            source_version="org-version-7",
        )

    def membership(self, user, role, identifier=None):
        return ClerkMembership(
            clerk_user_id=user,
            clerk_membership_id=identifier or f"mem_{user}",
            clerk_role_key=role,
            source_version=f"version_{user}",
        )

    def test_explicit_clerk_roles_project_to_owner_admin_and_member(self):
        projection = project_memberships(self.snapshot(
            self.membership("user_owner", "org:owner"),
            self.membership("user_admin", "admin"),
            self.membership("user_member", "org:member"),
        ))

        self.assertEqual(
            {row.clerk_user_id: row.role for row in projection.memberships},
            {"user_owner": "owner", "user_admin": "admin", "user_member": "member"},
        )
        self.assertEqual(projection.ownership_status, "authoritative")
        self.assertEqual(projection.active_owner_count, 1)

    def test_active_clerk_creator_is_owner_when_no_explicit_owner_exists(self):
        projection = project_memberships(self.snapshot(
            self.membership("user_creator", "org:admin"),
            self.membership("user_admin", "org:admin"),
        ))

        rows = {row.clerk_user_id: row for row in projection.memberships}
        self.assertEqual(rows["user_creator"].role, "owner")
        self.assertEqual(rows["user_creator"].role_basis, "organization_creator")
        self.assertEqual(rows["user_admin"].role, "admin")

    def test_missing_active_creator_does_not_invent_an_owner(self):
        projection = project_memberships(self.snapshot(
            self.membership("user_admin", "org:admin"),
        ))

        self.assertEqual(projection.ownership_status, "ambiguous")
        self.assertEqual(projection.active_owner_count, 0)
        self.assertEqual(projection.memberships[0].role, "admin")

    def test_creator_downgraded_to_member_is_not_restored_as_owner(self):
        projection = project_memberships(self.snapshot(
            self.membership("user_creator", "org:member"),
        ))
        self.assertEqual(projection.ownership_status, "ambiguous")
        self.assertEqual(projection.active_owner_count, 0)
        self.assertEqual(projection.memberships[0].role, "member")

    def test_unknown_clerk_role_maps_to_least_privileged_member(self):
        projection = project_memberships(self.snapshot(
            self.membership("user_creator", "org:owner"),
            self.membership("user_custom", "org:analyst"),
        ))
        rows = {row.clerk_user_id: row for row in projection.memberships}
        self.assertEqual(rows["user_custom"].role, "member")

    def test_explicit_owner_role_disables_creator_fallback(self):
        projection = project_memberships(self.snapshot(
            self.membership("user_creator", "org:admin"),
            self.membership("user_owner", "owner"),
        ))
        rows = {row.clerk_user_id: row for row in projection.memberships}
        self.assertEqual(rows["user_creator"].role, "admin")
        self.assertEqual(rows["user_owner"].role, "owner")

    def test_projection_rejects_duplicate_users_or_membership_ids(self):
        with self.assertRaises(ValueError):
            project_memberships(self.snapshot(
                self.membership("user_same", "member", "mem_one"),
                self.membership("user_same", "member", "mem_two"),
            ))
        with self.assertRaises(ValueError):
            project_memberships(self.snapshot(
                self.membership("user_one", "member", "mem_same"),
                self.membership("user_two", "member", "mem_same"),
            ))


class _Source:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error
        self.requested = []

    def organization_snapshot(self, organization_id):
        self.requested.append(organization_id)
        if self.error:
            raise self.error
        return self.snapshot


class _ChangingSource:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)

    def organization_snapshot(self, organization_id):
        return next(self.snapshots)


class _ProjectionStore:
    def __init__(self):
        self.saved = []
        self.rows = {}
        self.failures = []
        self.current_generation = None

    def begin_tenant_membership_sync(self, *, tenant_id,
                                     clerk_organization_id,
                                     sync_generation):
        self.current_generation = sync_generation
        return {"sync_generation": sync_generation}

    def replace_tenant_membership_projection(self, *, tenant_id, projection,
                                             sync_generation, synchronized_at):
        if sync_generation != self.current_generation:
            raise ValueError("sync attempt was not reserved")
        self.saved.append((tenant_id, projection, sync_generation, synchronized_at))
        self.rows = {row.clerk_user_id: {
            "role": row.role,
            "status": "active",
            "sync_generation": sync_generation,
        } for row in projection.memberships}

    def tenant_membership_authorization(self, *, tenant_id, clerk_user_id,
                                        sync_generation):
        row = self.rows.get(clerk_user_id)
        if row is None or row["sync_generation"] != sync_generation:
            return None
        projection = self.saved[-1][1]
        return {
            "tenant_id": tenant_id,
            "clerk_user_id": clerk_user_id,
            "role": row["role"],
            "status": row["status"],
            "sync_generation": sync_generation,
            "ownership_status": projection.ownership_status,
            "active_owner_count": projection.active_owner_count,
        }

    def record_tenant_membership_sync_failure(self, *, tenant_id,
                                              sync_generation, attempted_at,
                                              failure_category):
        self.failures.append({
            "tenant_id": tenant_id,
            "sync_generation": sync_generation,
            "attempted_at": attempted_at,
            "failure_category": failure_category,
        })


class WorkspaceAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = ClerkOrganizationSnapshot(
            organization_id="org_2acme",
            created_by_user_id="user_owner",
            memberships=(
                ClerkMembership("user_owner", "mem_owner", "org:admin"),
                ClerkMembership("user_admin", "mem_admin", "org:admin"),
                ClerkMembership("user_member", "mem_member", "org:member"),
            ),
        )
        self.store = _ProjectionStore()

    def principal(self, user="user_owner", tenant="ten_" + "1" * 32,
                  organization="org_2acme", jwt_role="org:member"):
        return ClerkPrincipal(
            clerk_user_id=user,
            clerk_organization_id=organization,
            tenant_id=tenant,
            clerk_organization_role=jwt_role,
        )

    def authorizer(self, snapshot=None):
        return WorkspaceMembershipAuthorizer(
            store=self.store,
            source=_Source(snapshot or self.snapshot),
        )

    def test_role_helpers_use_refreshed_projection_not_jwt_role(self):
        authorizer = self.authorizer()
        principal = self.principal(jwt_role="org:member")

        self.assertEqual(authorizer.current_workspace_role(principal), "owner")
        self.assertEqual(authorizer.active_owner_count(principal), 1)
        self.assertTrue(authorizer.is_sole_owner(principal))
        self.assertEqual(authorizer.require_owner(principal).role, "owner")

    def test_admin_or_owner_refuses_an_active_member(self):
        authorizer = self.authorizer()
        with self.assertRaises(WorkspaceRoleDenied):
            authorizer.require_admin_or_owner(self.principal(user="user_member"))
        self.assertEqual(
            authorizer.require_admin_or_owner(self.principal(user="user_admin")).role,
            "admin",
        )

    def test_browser_style_scope_values_are_not_part_of_the_api(self):
        authorizer = self.authorizer()
        with self.assertRaises(TypeError):
            authorizer.require_owner(
                self.principal(user="user_member"),
                tenant_id="ten_" + "9" * 32,
                clerk_user_id="user_owner",
                role="owner",
            )

    def test_missing_tenant_or_current_membership_fails_closed(self):
        authorizer = self.authorizer()
        with self.assertRaises(WorkspaceMembershipUnavailable):
            authorizer.require_owner(self.principal(tenant=None))
        with self.assertRaises(WorkspaceMembershipUnavailable):
            authorizer.require_owner(self.principal(user="user_absent"))

    def test_ambiguous_ownership_keeps_sensitive_helpers_unavailable(self):
        ambiguous = ClerkOrganizationSnapshot(
            organization_id="org_2acme",
            created_by_user_id=None,
            memberships=(ClerkMembership(
                "user_admin", "mem_admin", "org:admin"),),
        )
        authorizer = self.authorizer(ambiguous)
        principal = self.principal(user="user_admin")

        self.assertEqual(authorizer.current_workspace_role(principal), "admin")
        for operation in (
            authorizer.require_owner,
            authorizer.require_admin_or_owner,
            authorizer.active_owner_count,
            authorizer.is_sole_owner,
        ):
            with self.assertRaises(WorkspaceMembershipUnavailable):
                operation(principal)

    def test_authority_failure_is_recorded_without_reusing_stale_projection(self):
        source = _Source(error=WorkspaceMembershipUnavailable("secret detail"))
        authorizer = WorkspaceMembershipAuthorizer(store=self.store, source=source)

        with self.assertRaises(WorkspaceMembershipUnavailable):
            authorizer.require_owner(self.principal())

        self.assertEqual(len(self.store.failures), 1)
        self.assertEqual(
            self.store.failures[0]["failure_category"], "authority_unavailable")
        self.assertNotIn("secret", repr(self.store.failures[0]))

    def test_multiple_explicit_owners_are_counted_and_not_sole(self):
        snapshot = ClerkOrganizationSnapshot(
            organization_id="org_2acme", created_by_user_id="user_owner",
            memberships=(
                ClerkMembership("user_owner", "mem_owner", "org:owner"),
                ClerkMembership("user_coowner", "mem_coowner", "owner"),
            ),
        )
        authorizer = self.authorizer(snapshot)
        principal = self.principal()
        self.assertEqual(authorizer.active_owner_count(principal), 2)
        self.assertFalse(authorizer.is_sole_owner(principal))

    def test_snapshot_for_another_organization_fails_closed(self):
        snapshot = ClerkOrganizationSnapshot(
            organization_id="org_other", created_by_user_id="user_owner",
            memberships=(ClerkMembership(
                "user_owner", "mem_owner", "org:owner"),),
        )
        with self.assertRaises(WorkspaceMembershipUnavailable):
            self.authorizer(snapshot).require_owner(self.principal())

    def test_membership_change_between_complete_reads_fails_closed(self):
        changed = ClerkOrganizationSnapshot(
            organization_id="org_2acme", created_by_user_id="user_owner",
            memberships=(
                ClerkMembership("user_owner", "mem_owner", "org:admin"),
                ClerkMembership("user_new", "mem_new", "org:member"),
            ),
        )
        authorizer = WorkspaceMembershipAuthorizer(
            store=self.store, source=_ChangingSource((self.snapshot, changed)))
        with self.assertRaises(WorkspaceMembershipUnavailable):
            authorizer.require_owner(self.principal())
        self.assertEqual(self.store.saved, [])


if __name__ == "__main__":
    unittest.main()
