"""Durable orchestration for deleting one authenticated workspace."""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.api.clerk_management import ClerkMembershipUnavailable, ClerkResourceAbsent
from agent.billing.lifecycle import BillingLifecycleError, revoke_workspace_subscriptions
from agent.github_app.client import GitHubAPIError, GitHubNotFoundError
from agent.lifecycle_authorization import (
    RecentVerificationRequired,
    require_recent_verification,
)
from agent.workspace_credential_revocation import (
    CredentialRevocationError,
    revoke_workspace_credentials,
)


class LifecycleBlocked(RuntimeError):
    """A safe, bounded lifecycle failure suitable for an API status code."""

    def __init__(self, category, *, disposition="blocked"):
        super().__init__(category.replace("_", " "))
        self.category = category
        self.disposition = disposition


class WorkspaceDeletionEngine:
    """Advance one deletion through durable, independently retryable phases."""

    def __init__(self, *, authorizer, store, polar_client, github_client,
                 github_app_jwt, clerk_client, repository_storage=None,
                 billing_revoker=revoke_workspace_subscriptions,
                 credential_revoker=revoke_workspace_credentials, clock=None):
        self.authorizer = authorizer
        self.store = store
        self.polar_client = polar_client
        self.github_client = github_client
        self.github_app_jwt = github_app_jwt
        self.clerk_client = clerk_client
        self.repository_storage = repository_storage
        self.billing_revoker = billing_revoker
        self.credential_revoker = credential_revoker
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def request(self, principal, *, confirmation):
        self._require_recent(principal)
        context = self.authorizer.require_owner(principal)
        tenant = self.store.tenant_by_id(context.tenant_id)
        if tenant is None:
            raise LifecycleBlocked("workspace_not_found")
        if confirmation != tenant["organization_name"]:
            raise LifecycleBlocked("confirmation_mismatch")
        try:
            return self.store.begin_workspace_deletion(
                tenant_id=context.tenant_id,
                initiated_by_clerk_user_id=context.clerk_user_id,
                confirmation_verified_at=self.clock(),
            )
        except ValueError as exc:
            category = str(exc) if str(exc) in {
                "operational_ownership_incomplete",
                "operational_ownership_inconsistent",
            } else "workspace_lifecycle_conflict"
            raise LifecycleBlocked(category) from None

    def advance(self, principal, operation_id):
        self._require_recent(principal)
        context = self.authorizer.require_owner(principal)
        operation = self.store.workspace_lifecycle_operation_for_tenant(
            context.tenant_id, operation_id)
        if operation is None:
            receipt_reader = getattr(self.store, "deletion_receipt", None)
            receipt = receipt_reader(operation_id) if receipt_reader else None
            if receipt is not None:
                return {"receipt_id": receipt["receipt_id"], "state": "completed"}
            raise LifecycleBlocked("lifecycle_operation_not_found")

        operation, lease_id = self._claim(operation)
        try:
            return self._dispatch(principal, operation)
        finally:
            self._release(operation, lease_id)

    def advance_server(self, operation_id):
        """Finish the post-Clerk crash window from trusted durable state."""
        operation_reader = getattr(self.store, "workspace_lifecycle_operation", None)
        operation = operation_reader(operation_id) if operation_reader else None
        if operation is None:
            receipt = self.store.deletion_receipt(operation_id)
            if receipt:
                return {"receipt_id": receipt["receipt_id"], "state": "completed"}
            raise LifecycleBlocked("lifecycle_operation_not_found")
        if operation["phase"] != "clerk_organization_deletion":
            raise LifecycleBlocked("server_resume_not_available")
        operation, lease_id = self._claim(operation)
        try:
            return self._clerk_and_finalize(operation)
        finally:
            self._release(operation, lease_id)

    def _dispatch(self, principal, operation):
        operation = self._renew(operation)
        phase = operation["phase"]
        if phase in {"frozen", "billing_reconciliation"}:
            return self._billing(principal, operation)
        if phase in {"billing_revoked", "github_access_revocation"}:
            return self._github_and_credentials(principal, operation)
        if phase in {"credentials_revoked", "artifact_purge"}:
            return self._artifacts(operation)
        if phase == "artifacts_purged":
            return self._database(operation)
        if phase in {"database_purge", "clerk_organization_deletion"}:
            return self._clerk_and_finalize(operation)
        raise LifecycleBlocked("invalid_lifecycle_phase")

    def _renew(self, operation):
        renew = getattr(self.store, "renew_workspace_lifecycle_operation", None)
        if renew is None or operation.get("lease_id") is None:
            return operation
        now = self.clock()
        try:
            return renew(
                tenant_id=operation["tenant_id"],
                operation_id=operation["operation_id"],
                lease_id=operation["lease_id"],
                expected_generation=operation["generation"], now=now,
                lease_expires_at=now + timedelta(minutes=10))
        except ValueError:
            raise LifecycleBlocked("lifecycle_operation_busy",
                                   disposition="retryable") from None

    def _claim(self, operation):
        claim = getattr(self.store, "claim_workspace_lifecycle_operation", None)
        if claim is None:
            return operation, None
        now = self.clock()
        lease_id = f"lease_{uuid.uuid4().hex}"
        claimed = claim(
            tenant_id=operation["tenant_id"],
            operation_id=operation["operation_id"], lease_id=lease_id, now=now,
            lease_expires_at=now + timedelta(minutes=2))
        if claimed is None:
            raise LifecycleBlocked("lifecycle_operation_busy",
                                   disposition="retryable")
        return claimed, lease_id

    def _release(self, operation, lease_id):
        release = getattr(self.store, "release_workspace_lifecycle_operation", None)
        if lease_id is not None and release is not None:
            release(tenant_id=operation["tenant_id"],
                    operation_id=operation["operation_id"], lease_id=lease_id)

    def _billing(self, principal, operation):
        operation = self._phase(operation, {"frozen", "billing_reconciliation"},
                                "billing_reconciliation")
        try:
            result = self.billing_revoker(
                principal=principal, authorizer=self.authorizer,
                store=self.store, client=self.polar_client, clock=self.clock)
            if result.get("state") != "verified_safe":
                raise BillingLifecycleError("billing_not_terminal")
        except BillingLifecycleError as exc:
            self._fail(operation, exc.category, disposition="retryable")
        return self._phase(operation, {"billing_reconciliation"},
                           "billing_revoked",
                           billing_terminal_verified_at=self.clock())

    def _github_and_credentials(self, principal, operation):
        operation = self._phase(
            operation, {"billing_revoked", "github_access_revocation"},
            "github_access_revocation")
        app_jwt = self.github_app_jwt()
        for binding in self.store.tenant_github_installations(
                operation["tenant_id"], include_deleted=True):
            installation_id = binding["github_installation_id"]
            try:
                try:
                    self.github_client.delete_installation(installation_id, app_jwt)
                    outcome = "accepted"
                except GitHubNotFoundError:
                    outcome = "verified_absent"
                if outcome != "verified_absent":
                    try:
                        self.github_client.get_installation(installation_id, app_jwt)
                    except GitHubNotFoundError:
                        outcome = "verified_absent"
                    else:
                        self._fail(operation, "github_not_terminal",
                                   disposition="retryable")
                self.store.record_workspace_lifecycle_provider_result(
                    operation_id=operation["operation_id"],
                    tenant_id=operation["tenant_id"], provider="github",
                    target_kind="installation",
                    target_reference=str(installation_id), outcome=outcome,
                    failure_category=None, recorded_at=self.clock(),
                    expected_generation=operation.get("generation"),
                    lease_id=operation.get("lease_id"))
            except GitHubAPIError as exc:
                self._fail(operation, _github_failure(exc),
                           disposition=("retryable" if exc.retryable
                                        or exc.status_code is None else "blocked"))
        try:
            revoked = self.credential_revoker(
                principal=principal, authorizer=self.authorizer, store=self.store)
        except CredentialRevocationError as exc:
            self._fail(operation, exc.category)
        if revoked.get("state") != "revoked" or int(
                revoked.get("claimed_work_remaining", 0)) != 0:
            self._fail(operation, "claimed_work_remaining",
                       disposition="retryable")
        return self._phase(
            operation, {"github_access_revocation"}, "credentials_revoked",
            github_terminal_verified_at=self.clock(),
            credentials_revoked_at=self.clock())

    def _artifacts(self, operation):
        repository_ids = self.store.tenant_repository_storage_ids(
            operation["tenant_id"])
        if operation["phase"] == "credentials_revoked":
            try:
                planned = count_repository_storage(
                    self.repository_storage, repository_ids)
            except (OSError, ValueError):
                self._fail(operation, "artifact_purge_failed",
                           disposition="retryable")
            operation = self._phase(
                operation, {"credentials_revoked"}, "artifact_purge",
                artifact_files_deleted=planned)
        try:
            purge_repository_storage(self.repository_storage, repository_ids)
        except (OSError, ValueError):
            self._fail(operation, "artifact_purge_failed", disposition="retryable")
        return self._phase(operation, {"artifact_purge"}, "artifacts_purged")

    def _database(self, operation):
        try:
            result = self.store.purge_workspace_operational_data(
                tenant_id=operation["tenant_id"],
                operation_id=operation["operation_id"],
                expected_generation=operation.get("generation"),
                lease_id=operation.get("lease_id"))
        except ValueError as exc:
            category = str(exc) if str(exc).startswith("operational_ownership_") \
                else "database_purge_failed"
            self._fail(operation, category)
        return self._phase(
            operation, {"artifacts_purged"}, "database_purge",
            operational_records_deleted=result["operational_records_deleted"])

    def _clerk_and_finalize(self, operation):
        operation = self._phase(
            operation, {"database_purge", "clerk_organization_deletion"},
            "clerk_organization_deletion")
        tenant = self.store.tenant_by_id(operation["tenant_id"])
        if tenant is None:
            self._fail(operation, "workspace_identity_missing")
        try:
            try:
                self.clerk_client.delete_organization(
                    tenant["clerk_organization_id"])
            except ClerkResourceAbsent:
                pass
            try:
                self.clerk_client.get_organization(tenant["clerk_organization_id"])
            except ClerkResourceAbsent:
                outcome = "verified_absent"
            else:
                self._fail(operation, "clerk_organization_not_terminal",
                           disposition="retryable")
            self.store.record_workspace_lifecycle_provider_result(
                operation_id=operation["operation_id"],
                tenant_id=operation["tenant_id"], provider="clerk",
                target_kind="organization", target_reference=tenant[
                    "clerk_organization_id"], outcome=outcome,
                failure_category=None, recorded_at=self.clock(),
                expected_generation=operation.get("generation"),
                lease_id=operation.get("lease_id"))
        except ClerkMembershipUnavailable:
            self._fail(operation, "clerk_provider_failure",
                       disposition="retryable")
        return self.store.finalize_workspace_deletion(
            tenant_id=operation["tenant_id"],
            operation_id=operation["operation_id"], completed_at=self.clock(),
            expected_generation=operation.get("generation"),
            lease_id=operation.get("lease_id"))

    def _phase(self, operation, expected, phase, **values):
        return self.store.set_workspace_lifecycle_phase(
            tenant_id=operation["tenant_id"],
            operation_id=operation["operation_id"],
            expected_phases=expected, phase=phase, updated_at=self.clock(),
            expected_generation=operation.get("generation"),
            lease_id=operation.get("lease_id"),
            **values)

    def _fail(self, operation, category, *, disposition="blocked"):
        self.store.fail_workspace_lifecycle_phase(
            tenant_id=operation["tenant_id"],
            operation_id=operation["operation_id"],
            disposition=disposition, failure_category=category,
            updated_at=self.clock(),
            expected_generation=operation.get("generation"),
            lease_id=operation.get("lease_id"))
        raise LifecycleBlocked(category, disposition=disposition)

    @staticmethod
    def _require_recent(principal):
        try:
            require_recent_verification(principal)
        except RecentVerificationRequired as exc:
            raise LifecycleBlocked(exc.category) from None


def purge_repository_storage(storage_root, repository_ids):
    """Delete only exact numeric repository directories beneath one root."""
    if storage_root is None:
        return 0
    root = Path(storage_root).resolve()
    if not root.exists():
        return 0
    if not root.is_dir() or root.is_symlink():
        raise ValueError("repository storage root is unsafe")
    deleted_files = 0
    for repository_id in sorted({int(value) for value in repository_ids}):
        if repository_id <= 0:
            raise ValueError("repository id is unsafe")
        target = root / str(repository_id)
        quarantine = root / f".relium-delete-{repository_id}"
        candidate = target if target.exists() else quarantine
        if not candidate.exists():
            continue
        if candidate.is_symlink() or candidate.resolve().parent != root:
            raise ValueError("repository storage target is unsafe")
        for current, directories, files in os.walk(candidate, followlinks=False):
            current_path = Path(current)
            for name in directories + files:
                if (current_path / name).is_symlink():
                    raise ValueError("repository storage contains a symlink")
            deleted_files += len(files)
        if candidate == target:
            if quarantine.exists():
                raise ValueError("repository quarantine already exists")
            target.rename(quarantine)
        shutil.rmtree(quarantine)
    return deleted_files


def count_repository_storage(storage_root, repository_ids):
    """Count the frozen deletion set before the non-transactional purge."""
    if storage_root is None:
        return 0
    root = Path(storage_root).resolve()
    if not root.exists():
        return 0
    if not root.is_dir() or root.is_symlink():
        raise ValueError("repository storage root is unsafe")
    count = 0
    for repository_id in sorted({int(value) for value in repository_ids}):
        if repository_id <= 0:
            raise ValueError("repository id is unsafe")
        target = root / str(repository_id)
        quarantine = root / f".relium-delete-{repository_id}"
        candidate = target if target.exists() else quarantine
        if not candidate.exists():
            continue
        if candidate.is_symlink() or candidate.resolve().parent != root:
            raise ValueError("repository storage target is unsafe")
        for current, directories, files in os.walk(candidate, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink()
                   for name in directories + files):
                raise ValueError("repository storage contains a symlink")
            count += len(files)
    return count


def _github_failure(error):
    if error.status_code is None:
        return "github_provider_timeout"
    if error.status_code == 429:
        return "github_provider_rate_limit"
    if 500 <= error.status_code <= 599:
        return "github_provider_server"
    if error.status_code in {401, 403}:
        return "github_provider_auth"
    return "github_provider_refused"
