"""Provider-authoritative controls for leaving an active workspace."""
from __future__ import annotations

from agent.api.clerk_management import ClerkMembershipUnavailable, ClerkResourceAbsent
from agent.lifecycle_authorization import RecentVerificationRequired, require_recent_verification


class AccessControlBlocked(RuntimeError):
    def __init__(self, category, *, disposition="blocked"):
        super().__init__(category.replace("_", " "))
        self.category = category
        self.disposition = disposition


def leave_workspace(*, principal, authorizer, clerk_client, store,
                    clerk_organization_id):
    try:
        require_recent_verification(principal)
    except RecentVerificationRequired as exc:
        raise AccessControlBlocked(exc.category) from None
    context = authorizer.authorization_context(principal)
    if context.role == "owner" and context.active_owner_count <= 1:
        raise AccessControlBlocked("sole_owner")
    if clerk_organization_id != getattr(principal, "clerk_organization_id", clerk_organization_id):
        raise AccessControlBlocked("workspace_scope_mismatch")
    try:
        operation = store.begin_workspace_departure(
            tenant_id=context.tenant_id,
            clerk_organization_id=clerk_organization_id,
            clerk_user_id=context.clerk_user_id,
            clerk_membership_id=context.clerk_membership_id,
            role=context.role, active_owner_count=context.active_owner_count)
    except ValueError as exc:
        category = str(exc) if str(exc) in {"sole_owner", "concurrent_membership_departure"} \
            else "workspace_departure_conflict"
        raise AccessControlBlocked(category) from None
    return resume_workspace_departure(
        operation=operation, clerk_client=clerk_client, store=store)


def resume_workspace_departure(*, operation, clerk_client, store):
    """Resume from a durable guard after the user's Clerk session disappears."""
    organization_id = operation["clerk_organization_id"]
    user_id = operation["clerk_user_id"]
    try:
        memberships = clerk_client.user_organization_memberships(user_id)
        if any(row.organization_id == organization_id for row in memberships):
            # The authorization path established owner safety immediately
            # before this guard was written. A retry does not guess from that
            # snapshot: it must re-establish the current safe verdict.
            from agent.api.workspace_membership import project_memberships
            first = project_memberships(clerk_client.organization_snapshot(
                organization_id))
            second = project_memberships(clerk_client.organization_snapshot(
                organization_id))
            current = next((row for row in second.memberships
                            if row.clerk_user_id == user_id), None)
            if (first.source_fingerprint != second.source_fingerprint
                    or current is None or second.ownership_status != "authoritative"):
                raise AccessControlBlocked("membership_authority_changed")
            if current.role == "owner" and second.active_owner_count <= 1:
                raise AccessControlBlocked("sole_owner")
            try:
                clerk_client.delete_organization_membership(organization_id, user_id)
            except ClerkResourceAbsent:
                pass
            memberships = clerk_client.user_organization_memberships(user_id)
        if any(row.organization_id == organization_id for row in memberships):
            raise AccessControlBlocked("clerk_membership_not_terminal",
                                       disposition="retryable")
    except AccessControlBlocked:
        raise
    except ClerkMembershipUnavailable:
        raise AccessControlBlocked("clerk_provider_failure",
                                   disposition="retryable") from None
    store.complete_workspace_departure(
        operation_id=operation["operation_id"],
        clerk_organization_id=organization_id, clerk_user_id=user_id)
    return {"state": "left"}
