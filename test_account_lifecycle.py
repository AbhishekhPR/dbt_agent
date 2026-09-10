from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agent.account_lifecycle import AccountLifecycleEngine, AccountLifecycleBlocked
from agent.api.clerk_identity import ClerkPrincipal
from agent.api.workspace_membership import ClerkMembership, ClerkOrganizationSnapshot
from agent.api.clerk_management import ClerkResourceAbsent


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _principal(**changes):
    values = dict(clerk_user_id="user_a", clerk_organization_id="org_a",
                  tenant_id="ten_a", factor_verification_age=(0, -1),
                  clerk_token_issued_at=NOW, is_impersonated=False)
    values.update(changes)
    return ClerkPrincipal(**values)


def _snapshot(org, members):
    return ClerkOrganizationSnapshot(
        organization_id=org, created_by_user_id=members[0][0],
        memberships=tuple(ClerkMembership(
            clerk_user_id=user, clerk_membership_id=f"mem_{org}_{user}",
            clerk_role_key=role) for user, role in members))


class _Clerk:
    def __init__(self, organizations):
        self.organizations = organizations
        self.deleted_memberships = []
        self.deleted_users = []

    def user_organization_memberships(self, user_id):
        from agent.api.clerk_management import ClerkUserOrganizationMembership
        return tuple(ClerkUserOrganizationMembership(
            organization_id=org, clerk_membership_id=f"mem_{org}_{user_id}",
            clerk_role_key=next(role for user, role in members if user == user_id))
            for org, members in self.organizations.items()
            if any(user == user_id for user, _ in members))

    def organization_snapshot(self, organization_id):
        return _snapshot(organization_id, self.organizations[organization_id])

    def delete_organization_membership(self, organization_id, user_id):
        self.deleted_memberships.append((organization_id, user_id))
        members = self.organizations[organization_id]
        self.organizations[organization_id] = [m for m in members if m[0] != user_id]

    def delete_user(self, user_id):
        self.deleted_users.append(user_id)

    def get_user(self, user_id):
        if user_id in self.deleted_users:
            raise ClerkResourceAbsent("absent")
        return {"id": user_id}


class _Store:
    def __init__(self):
        self.operation = None
        self.memberships = []
        self.local_revoked = False
        self.finalized = False

    def account_lifecycle_operation_for_user(self, user_id):
        return dict(self.operation) if self.operation else None

    def account_lifecycle_operation(self, operation_id):
        return (dict(self.operation) if self.operation
                and self.operation["operation_id"] == operation_id else None)

    def deletion_receipt(self, operation_id):
        return None

    def begin_account_deletion(self, **values):
        self.memberships = list(values["memberships"])
        self.operation = {"operation_id": "ald_1", "clerk_user_id": values["clerk_user_id"],
                          "phase": "leaving_workspaces", "disposition": "running",
                          "dissociated_actor_ref": values["dissociated_actor_ref"]}
        return dict(self.operation)

    def account_lifecycle_memberships(self, operation_id):
        return [dict(row) for row in self.memberships]

    def mark_account_membership_absent(self, *, organization_id, **unused):
        for row in self.memberships:
            if row["clerk_organization_id"] == organization_id:
                row["state"] = "verified_absent"

    def set_account_lifecycle_phase(self, *, phase, **unused):
        self.operation["phase"] = phase
        self.operation["disposition"] = "running"
        self.operation["failure_category"] = None
        return dict(self.operation)

    def fail_account_lifecycle_phase(self, **values):
        self.operation["disposition"] = values["disposition"]
        self.operation["failure_category"] = values["failure_category"]

    def revoke_account_local_access(self, clerk_user_id, dissociated_actor_ref,
                                    **unused):
        self.local_revoked = True
        return {"state": "revoked"}

    def finalize_account_deletion(self, **unused):
        self.finalized = True
        self.operation = None
        return {"receipt_id": "ald_1", "state": "completed"}


class AccountLifecycleTests(unittest.TestCase):
    def test_request_blocks_before_state_when_user_is_sole_owner(self):
        clerk = _Clerk({"org_a": [("user_a", "org:admin")]})
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        with self.assertRaises(AccountLifecycleBlocked) as raised:
            engine.request(_principal(), confirmation="DELETE MY ACCOUNT")
        self.assertEqual(raised.exception.category, "sole_owner")
        self.assertIsNone(store.operation)

    def test_request_requires_exact_confirmation_and_recent_auth(self):
        clerk = _Clerk({"org_a": [("user_a", "org:member"),
                                   ("owner", "org:owner")]})
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        with self.assertRaises(AccountLifecycleBlocked):
            engine.request(_principal(), confirmation="delete my account")
        with self.assertRaises(AccountLifecycleBlocked):
            engine.request(_principal(factor_verification_age=None),
                           confirmation="DELETE MY ACCOUNT")
        self.assertIsNone(store.operation)

    def test_shared_memberships_are_left_before_local_and_clerk_user_revocation(self):
        clerk = _Clerk({"org_a": [("user_a", "org:member"),
                                   ("owner", "org:owner")],
                        "org_b": [("user_a", "org:owner"),
                                   ("owner_b", "org:owner")]})
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        op = engine.request(_principal(), confirmation="DELETE MY ACCOUNT")
        result = engine.advance(_principal(), op["operation_id"])
        self.assertEqual(result["phase"], "credentials_revoked")
        self.assertTrue(store.local_revoked)
        self.assertCountEqual(clerk.deleted_memberships,
                              [("org_a", "user_a"), ("org_b", "user_a")])
        result = engine.advance(_principal(), op["operation_id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(clerk.deleted_users, ["user_a"])
        self.assertTrue(store.finalized)

    def test_unstable_authority_fails_closed_without_persisting(self):
        clerk = _Clerk({"org_a": [("user_a", "org:member"),
                                   ("owner", "org:owner")]})
        calls = 0
        original = clerk.user_organization_memberships
        def unstable(user_id):
            nonlocal calls
            calls += 1
            result = original(user_id)
            return result if calls == 1 else ()
        clerk.user_organization_memberships = unstable
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        with self.assertRaises(AccountLifecycleBlocked) as raised:
            engine.request(_principal(), confirmation="DELETE MY ACCOUNT")
        self.assertEqual(raised.exception.category, "membership_authority_changed")
        self.assertIsNone(store.operation)

    def test_new_membership_after_request_blocks_before_any_leave(self):
        clerk = _Clerk({"org_a": [("user_a", "org:member"),
                                   ("owner", "org:owner")]})
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        op = engine.request(_principal(), confirmation="DELETE MY ACCOUNT")
        clerk.organizations["org_new"] = [("user_a", "org:admin")]
        with self.assertRaises(AccountLifecycleBlocked) as raised:
            engine.advance(_principal(), op["operation_id"])
        self.assertEqual(raised.exception.category, "membership_inventory_changed")
        self.assertEqual(clerk.deleted_memberships, [])

    def test_later_sole_owner_blocks_before_first_membership_is_removed(self):
        clerk = _Clerk({"org_a": [("user_a", "org:member"),
                                   ("owner", "org:owner")],
                        "org_b": [("user_a", "org:owner"),
                                   ("owner_b", "org:owner")]})
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        op = engine.request(_principal(), confirmation="DELETE MY ACCOUNT")
        clerk.organizations["org_b"] = [("user_a", "org:owner")]
        with self.assertRaises(AccountLifecycleBlocked) as raised:
            engine.advance(_principal(), op["operation_id"])
        self.assertEqual(raised.exception.category, "sole_owner")
        self.assertEqual(clerk.deleted_memberships, [])

    def test_rejoined_membership_is_removed_even_after_prior_absence_was_recorded(self):
        clerk = _Clerk({"org_a": [("user_a", "org:member"),
                                   ("owner", "org:owner")]})
        store = _Store()
        engine = AccountLifecycleEngine(store=store, clerk_client=clerk,
                                        clock=lambda: NOW)
        op = engine.request(_principal(), confirmation="DELETE MY ACCOUNT")
        store.memberships[0]["state"] = "verified_absent"

        result = engine.advance(_principal(), op["operation_id"])

        self.assertEqual(result["phase"], "credentials_revoked")
        self.assertEqual(clerk.deleted_memberships, [("org_a", "user_a")])


if __name__ == "__main__":
    unittest.main()
