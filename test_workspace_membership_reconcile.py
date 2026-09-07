from __future__ import annotations

import unittest

from agent.api.workspace_membership import ClerkMembership, ClerkOrganizationSnapshot
from agent.workspace_membership_reconcile import reconcile_tenant_memberships


class _Store:
    def __init__(self):
        self.saved = []
        self.current_generation = None

    def tenants_for_membership_reconciliation(self):
        return [
            {"tenant_id": "ten_" + "1" * 32,
             "clerk_organization_id": "org_authoritative"},
            {"tenant_id": "ten_" + "2" * 32,
             "clerk_organization_id": "org_ambiguous"},
        ]

    def replace_tenant_membership_projection(self, **values):
        if values["sync_generation"] != self.current_generation:
            raise ValueError("sync attempt was not reserved")
        self.saved.append(values)

    def begin_tenant_membership_sync(self, **values):
        self.current_generation = values["sync_generation"]


class _Source:
    def organization_snapshot(self, organization_id):
        if organization_id == "org_authoritative":
            return ClerkOrganizationSnapshot(
                organization_id=organization_id,
                created_by_user_id="user_owner",
                memberships=(ClerkMembership(
                    "user_owner", "mem_owner", "org:admin"),),
            )
        return ClerkOrganizationSnapshot(
            organization_id=organization_id,
            created_by_user_id=None,
            memberships=(ClerkMembership(
                "user_admin", "mem_admin", "org:admin"),),
        )


class MembershipReconciliationTests(unittest.TestCase):
    def test_dry_run_reports_without_mutating(self):
        store = _Store()
        report = reconcile_tenant_memberships(store, _Source(), apply=False)

        self.assertEqual(store.saved, [])
        self.assertEqual([row["ownership_status"] for row in report],
                         ["authoritative", "ambiguous"])

    def test_apply_persists_authoritative_and_ambiguous_snapshots(self):
        store = _Store()
        report = reconcile_tenant_memberships(store, _Source(), apply=True)

        self.assertEqual(len(store.saved), 2)
        self.assertEqual(len(report), 2)
        self.assertEqual(store.saved[0]["tenant_id"], "ten_" + "1" * 32)
        self.assertEqual(store.saved[1]["projection"].active_owner_count, 0)


if __name__ == "__main__":
    unittest.main()
