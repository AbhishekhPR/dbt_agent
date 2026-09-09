from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.api.clerk_identity import ClerkPrincipal
from agent.api.clerk_management import ClerkResourceAbsent
from agent.api.workspace_membership import WorkspaceAuthorizationContext
from agent.billing.lifecycle import BillingLifecycleError
from agent.github_app.client import GitHubAPIError, GitHubNotFoundError
from agent.workspace_deletion_lifecycle import (
    LifecycleBlocked,
    WorkspaceDeletionEngine,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class _Authorizer:
    def require_owner(self, principal):
        if principal.clerk_user_id != "user_owner":
            raise AssertionError("not owner")
        return WorkspaceAuthorizationContext(
            tenant_id="ten_a", clerk_user_id="user_owner", role="owner",
            ownership_status="authoritative", active_owner_count=1,
            sync_generation="sync_1")


class _Store:
    def __init__(self):
        self.operation = None
        self.failures = []
        self.purged = False
        self.finalized = False
        self.provider_results = []

    def tenant_by_id(self, tenant_id):
        return {"tenant_id": tenant_id, "organization_name": "Exact Workspace",
                "clerk_organization_id": "org_a"}

    def begin_workspace_deletion(self, **values):
        self.operation = {
            "operation_id": "wld_test", "tenant_id": values["tenant_id"],
            "phase": "frozen", "disposition": "running",
            "created_at": NOW,
        }
        return dict(self.operation)

    def workspace_lifecycle_operation_for_tenant(self, tenant_id, operation_id):
        if not self.operation or operation_id != self.operation["operation_id"]:
            return None
        return dict(self.operation)

    def set_workspace_lifecycle_phase(self, *, tenant_id, operation_id,
                                      expected_phases, phase, **values):
        if self.operation["phase"] not in expected_phases:
            raise AssertionError("stale phase")
        self.operation.update(phase=phase, disposition="running",
                              failure_category=None)
        self.operation.update(values)
        return dict(self.operation)

    def fail_workspace_lifecycle_phase(self, **values):
        self.operation.update(disposition=values["disposition"],
                              failure_category=values["failure_category"])
        self.failures.append(dict(values))

    def tenant_github_installations(self, tenant_id, *, include_deleted=False):
        return [{"github_installation_id": 9, "status": "active"}]

    def record_workspace_lifecycle_provider_result(self, **values):
        self.provider_results.append(values)

    def tenant_repository_storage_ids(self, tenant_id):
        return [101]

    def purge_workspace_operational_data(self, *, tenant_id, operation_id,
                                         **_guards):
        self.purged = True
        return {"operational_records_deleted": 42}

    def finalize_workspace_deletion(self, **values):
        self.finalized = True
        self.operation = None
        return {"receipt_id": values["operation_id"], "state": "completed"}


class _GitHub:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.deleted = []

    def delete_installation(self, installation_id, app_jwt):
        self.deleted.append(installation_id)
        if self.failure:
            raise self.failure
        return {}

    def get_installation(self, installation_id, app_jwt):
        raise GitHubNotFoundError("absent", status_code=404)


class _Clerk:
    def __init__(self):
        self.deleted = []

    def delete_organization(self, organization_id):
        self.deleted.append(organization_id)
        return {}

    def get_organization(self, organization_id):
        if organization_id in self.deleted:
            raise ClerkResourceAbsent("absent")
        return {"id": organization_id}


def _principal(**overrides):
    values = dict(
        clerk_user_id="user_owner", clerk_organization_id="org_a",
        tenant_id="ten_a", factor_verification_age=(0, -1),
        clerk_token_issued_at=NOW, is_impersonated=False)
    values.update(overrides)
    return ClerkPrincipal(**values)


class WorkspaceDeletionLifecycleTests(unittest.TestCase):
    def _engine(self, store=None, **overrides):
        store = store or _Store()
        values = dict(
            authorizer=_Authorizer(), store=store,
            polar_client=object(), github_client=_GitHub(),
            github_app_jwt=lambda: "test-app-jwt", clerk_client=_Clerk(),
            billing_revoker=lambda **kwargs: {"state": "verified_safe"},
            credential_revoker=lambda **kwargs: {"state": "revoked"},
            repository_storage=None, clock=lambda: NOW)
        values.update(overrides)
        return WorkspaceDeletionEngine(**values), store

    def test_request_requires_exact_name_before_freezing(self):
        engine, store = self._engine()
        with self.assertRaises(LifecycleBlocked) as raised:
            engine.request(_principal(), confirmation=" exact workspace ")
        self.assertEqual(raised.exception.category, "confirmation_mismatch")
        self.assertIsNone(store.operation)

    def test_request_requires_recent_non_impersonated_session(self):
        engine, store = self._engine()
        with self.assertRaises(LifecycleBlocked) as raised:
            engine.request(_principal(factor_verification_age=None),
                           confirmation="Exact Workspace")
        self.assertEqual(raised.exception.category,
                         "recent_verification_required")
        self.assertIsNone(store.operation)

    def test_advances_every_guarded_phase_and_clerk_is_last(self):
        calls = []
        store = _Store()
        github = _GitHub()
        clerk = _Clerk()
        engine, _ = self._engine(
            store,
            github_client=github,
            clerk_client=clerk,
            billing_revoker=lambda **kwargs: calls.append("billing") or {
                "state": "verified_safe"},
            credential_revoker=lambda **kwargs: calls.append("credentials") or {
                "state": "revoked", "claimed_work_remaining": 0},
        )
        operation = engine.request(_principal(), confirmation="Exact Workspace")

        for _ in range(5):
            result = engine.advance(_principal(), operation["operation_id"])

        self.assertEqual(result["state"], "completed")
        self.assertEqual(calls, ["billing", "credentials"])
        self.assertEqual(github.deleted, [9])
        self.assertTrue(store.purged)
        self.assertEqual(clerk.deleted, ["org_a"])
        self.assertTrue(store.finalized)

    def test_billing_failure_stops_before_github_or_purge(self):
        github = _GitHub()
        engine, store = self._engine(
            github_client=github,
            billing_revoker=lambda **kwargs: (_ for _ in ()).throw(
                BillingLifecycleError("provider_timeout")))
        operation = engine.request(_principal(), confirmation="Exact Workspace")

        with self.assertRaises(LifecycleBlocked) as raised:
            engine.advance(_principal(), operation["operation_id"])

        self.assertEqual(raised.exception.category, "provider_timeout")
        self.assertEqual(store.operation["phase"], "billing_reconciliation")
        self.assertFalse(store.purged)
        self.assertEqual(github.deleted, [])

    def test_github_ambiguity_stops_before_credentials_and_purge(self):
        credentials = []
        github = _GitHub(failure=GitHubAPIError(
            "timeout", status_code=None, operation="delete_installation"))
        engine, store = self._engine(
            github_client=github,
            credential_revoker=lambda **kwargs: credentials.append(True))
        operation = engine.request(_principal(), confirmation="Exact Workspace")
        engine.advance(_principal(), operation["operation_id"])

        with self.assertRaises(LifecycleBlocked) as raised:
            engine.advance(_principal(), operation["operation_id"])

        self.assertEqual(raised.exception.category, "github_provider_timeout")
        self.assertEqual(credentials, [])
        self.assertFalse(store.purged)

    def test_artifact_purge_removes_only_authoritative_numeric_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "101"
            other = root / "202"
            owned.mkdir()
            other.mkdir()
            (owned / "manifest.json").write_text("private", encoding="utf-8")
            (other / "manifest.json").write_text("other", encoding="utf-8")
            engine, store = self._engine(repository_storage=root)
            operation = engine.request(_principal(), confirmation="Exact Workspace")
            engine.advance(_principal(), operation["operation_id"])
            engine.advance(_principal(), operation["operation_id"])
            engine.advance(_principal(), operation["operation_id"])

            self.assertFalse(owned.exists())
            self.assertTrue(other.exists())
            self.assertEqual(store.operation["artifact_files_deleted"], 1)

    def test_artifact_count_survives_crash_after_files_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "101"
            owned.mkdir()
            (owned / "manifest.json").write_text("private", encoding="utf-8")
            engine, store = self._engine(repository_storage=root)
            operation = engine.request(_principal(), confirmation="Exact Workspace")
            engine.advance(_principal(), operation["operation_id"])
            engine.advance(_principal(), operation["operation_id"])

            from agent.workspace_deletion_lifecycle import purge_repository_storage
            def remove_then_crash(storage, repository_ids):
                purge_repository_storage(storage, repository_ids)
                raise OSError("simulated crash boundary")

            with patch("agent.workspace_deletion_lifecycle.purge_repository_storage",
                       side_effect=remove_then_crash):
                with self.assertRaises(LifecycleBlocked):
                    engine.advance(_principal(), operation["operation_id"])
            self.assertEqual(store.operation["phase"], "artifact_purge")
            self.assertEqual(store.operation["artifact_files_deleted"], 1)
            self.assertFalse(owned.exists())

            engine.advance(_principal(), operation["operation_id"])
            self.assertEqual(store.operation["phase"], "artifacts_purged")
            self.assertEqual(store.operation["artifact_files_deleted"], 1)


if __name__ == "__main__":
    unittest.main()
