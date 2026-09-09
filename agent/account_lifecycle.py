"""Durable account deletion without deleting any shared workspace data."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from agent.api.clerk_management import ClerkMembershipUnavailable, ClerkResourceAbsent
from agent.api.workspace_membership import project_memberships
from agent.lifecycle_authorization import RecentVerificationRequired, require_recent_verification


class AccountLifecycleBlocked(RuntimeError):
    def __init__(self, category, *, disposition="blocked"):
        super().__init__(category.replace("_", " "))
        self.category = category
        self.disposition = disposition


class AccountLifecycleEngine:
    def __init__(self, *, store, clerk_client, clock=None):
        self.store = store
        self.clerk_client = clerk_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def request(self, principal, *, confirmation):
        self._require_recent(principal)
        user_id = self._user(principal)
        if confirmation != "DELETE MY ACCOUNT":
            raise AccountLifecycleBlocked("confirmation_mismatch")
        existing = self.store.account_lifecycle_operation_for_user(user_id)
        if existing is not None:
            return existing
        try:
            first = self.clerk_client.user_organization_memberships(user_id)
            second = self.clerk_client.user_organization_memberships(user_id)
            if self._membership_fingerprint(first) != self._membership_fingerprint(second):
                raise AccountLifecycleBlocked("membership_authority_changed")
            records = []
            for membership in second:
                snapshot_a = self.clerk_client.organization_snapshot(
                    membership.organization_id)
                snapshot_b = self.clerk_client.organization_snapshot(
                    membership.organization_id)
                projected_a = project_memberships(snapshot_a)
                projected = project_memberships(snapshot_b)
                if projected_a.source_fingerprint != projected.source_fingerprint:
                    raise AccountLifecycleBlocked("membership_authority_changed")
                if projected.ownership_status != "authoritative":
                    raise AccountLifecycleBlocked("workspace_ownership_ambiguous")
                current = next((row for row in projected.memberships
                                if row.clerk_user_id == user_id), None)
                if (current is None
                        or current.clerk_membership_id != membership.clerk_membership_id):
                    raise AccountLifecycleBlocked("membership_authority_changed")
                if current.role == "owner" and projected.active_owner_count <= 1:
                    raise AccountLifecycleBlocked("sole_owner")
                records.append({
                    "clerk_organization_id": membership.organization_id,
                    "clerk_membership_id": membership.clerk_membership_id,
                    "authoritative_role": current.role,
                    "active_owner_count": projected.active_owner_count,
                    "state": "verified_shared",
                })
        except AccountLifecycleBlocked:
            raise
        except (ClerkMembershipUnavailable, ValueError):
            raise AccountLifecycleBlocked(
                "membership_authority_unavailable", disposition="retryable") from None
        try:
            return self.store.begin_account_deletion(
                clerk_user_id=user_id, memberships=tuple(records),
                dissociated_actor_ref=f"deleted_actor_{secrets.token_hex(16)}",
                confirmation_verified_at=self.clock())
        except ValueError as exc:
            category = str(exc) if str(exc) in {
                "sole_owner", "active_workspace_deletion",
                "concurrent_membership_departure",
            } else "account_lifecycle_conflict"
            raise AccountLifecycleBlocked(category) from None

    def advance(self, principal, operation_id):
        self._require_recent(principal)
        user_id = self._user(principal)
        operation = self.store.account_lifecycle_operation_for_user(user_id)
        if operation is None or operation.get("operation_id") != operation_id:
            receipt = self.store.deletion_receipt(operation_id)
            if receipt:
                return {"receipt_id": receipt["receipt_id"], "state": "completed"}
            raise AccountLifecycleBlocked("lifecycle_operation_not_found")
        return self.advance_server(operation_id, expected_user_id=user_id)

    def advance_server(self, operation_id, *, expected_user_id=None):
        """Resume a requested operation from trusted server-side state.

        This entry point exists because Clerk deletion invalidates the browser
        session. It accepts no tenant scope and may only act on the durable
        operation selected by its opaque identifier.
        """
        operation = self.store.account_lifecycle_operation(operation_id)
        if operation is None:
            receipt = self.store.deletion_receipt(operation_id)
            if receipt:
                return {"receipt_id": receipt["receipt_id"], "state": "completed"}
            raise AccountLifecycleBlocked("lifecycle_operation_not_found")
        user_id = operation["clerk_user_id"]
        if expected_user_id is not None and user_id != expected_user_id:
            raise AccountLifecycleBlocked("lifecycle_operation_not_found")
        operation, lease_id = self._claim(operation)
        try:
            if operation["phase"] == "leaving_workspaces":
                return self._leave_memberships(operation)
            if operation["phase"] in {"credentials_revoked", "clerk_user_deletion"}:
                return self._delete_clerk_user(operation)
            raise AccountLifecycleBlocked("invalid_lifecycle_phase")
        finally:
            release = getattr(self.store, "release_account_lifecycle_operation", None)
            if lease_id is not None and release is not None:
                release(operation_id=operation_id, lease_id=lease_id)

    def _claim(self, operation):
        claim = getattr(self.store, "claim_account_lifecycle_operation", None)
        if claim is None:
            return operation, None
        now = self.clock()
        lease_id = f"lease_{uuid.uuid4().hex}"
        claimed = claim(operation_id=operation["operation_id"], lease_id=lease_id,
                        now=now, lease_expires_at=now + timedelta(minutes=2))
        if claimed is None:
            raise AccountLifecycleBlocked("lifecycle_operation_busy",
                                          disposition="retryable")
        return claimed, lease_id

    def _leave_memberships(self, operation):
        user_id = operation["clerk_user_id"]
        try:
            for row in self.store.account_lifecycle_memberships(
                    operation["operation_id"]):
                if row["state"] == "verified_absent":
                    continue
                first = project_memberships(self.clerk_client.organization_snapshot(
                    row["clerk_organization_id"]))
                second = project_memberships(self.clerk_client.organization_snapshot(
                    row["clerk_organization_id"]))
                if first.source_fingerprint != second.source_fingerprint:
                    return self._fail(operation, "membership_authority_changed")
                current = next((member for member in second.memberships
                                if member.clerk_user_id == user_id), None)
                if current is None:
                    self.store.mark_account_membership_absent(
                        operation_id=operation["operation_id"],
                        organization_id=row["clerk_organization_id"],
                        updated_at=self.clock())
                    continue
                if (second.ownership_status != "authoritative"
                        or (current.role == "owner"
                            and second.active_owner_count <= 1)):
                    return self._fail(operation, "sole_owner")
                try:
                    self.clerk_client.delete_organization_membership(
                        row["clerk_organization_id"], user_id)
                except ClerkResourceAbsent:
                    pass
            remaining = {
                row.organization_id
                for row in self.clerk_client.user_organization_memberships(user_id)
            }
            for row in self.store.account_lifecycle_memberships(
                    operation["operation_id"]):
                organization_id = row["clerk_organization_id"]
                if organization_id in remaining:
                    return self._fail(operation, "clerk_membership_not_terminal",
                                      disposition="retryable")
                self.store.mark_account_membership_absent(
                    operation_id=operation["operation_id"],
                    organization_id=organization_id, updated_at=self.clock())
        except ClerkMembershipUnavailable:
            return self._fail(operation, "clerk_provider_failure",
                              disposition="retryable")
        result = self.store.revoke_account_local_access(
            user_id, operation["dissociated_actor_ref"])
        if result.get("state") != "revoked":
            return self._fail(operation, "account_credentials_not_revoked")
        return self.store.set_account_lifecycle_phase(
            operation_id=operation["operation_id"],
            expected_phases={"leaving_workspaces"}, phase="credentials_revoked",
            updated_at=self.clock())

    def _delete_clerk_user(self, operation):
        operation = self.store.set_account_lifecycle_phase(
            operation_id=operation["operation_id"],
            expected_phases={"credentials_revoked", "clerk_user_deletion"},
            phase="clerk_user_deletion", updated_at=self.clock())
        try:
            try:
                self.clerk_client.delete_user(operation["clerk_user_id"])
            except ClerkResourceAbsent:
                pass
            try:
                self.clerk_client.get_user(operation["clerk_user_id"])
            except ClerkResourceAbsent:
                return self.store.finalize_account_deletion(
                    operation_id=operation["operation_id"],
                    completed_at=self.clock())
            return self._fail(operation, "clerk_user_not_terminal",
                              disposition="retryable")
        except ClerkMembershipUnavailable:
            return self._fail(operation, "clerk_provider_failure",
                              disposition="retryable")

    def _fail(self, operation, category, *, disposition="blocked"):
        self.store.fail_account_lifecycle_phase(
            operation_id=operation["operation_id"], disposition=disposition,
            failure_category=category, updated_at=self.clock())
        raise AccountLifecycleBlocked(category, disposition=disposition)

    @staticmethod
    def _membership_fingerprint(rows):
        return tuple(sorted((row.organization_id, row.clerk_membership_id,
                             row.clerk_role_key, row.source_version)
                            for row in rows))

    @staticmethod
    def _require_recent(principal):
        try:
            require_recent_verification(principal)
        except RecentVerificationRequired as exc:
            raise AccountLifecycleBlocked(exc.category) from None

    @staticmethod
    def _user(principal):
        user_id = getattr(principal, "clerk_user_id", None)
        if (getattr(principal, "identity_provider", None) != "clerk"
                or not isinstance(user_id, str) or not user_id):
            raise AccountLifecycleBlocked("authenticated_clerk_user_required")
        return user_id
