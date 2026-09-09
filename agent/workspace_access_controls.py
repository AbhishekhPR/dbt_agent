"""Provider-authoritative controls for leaving an active workspace."""
from __future__ import annotations

from agent.api.clerk_management import ClerkMembershipUnavailable, ClerkResourceAbsent


class AccessControlBlocked(RuntimeError):
    def __init__(self, category, *, disposition="blocked"):
        super().__init__(category.replace("_", " "))
        self.category = category
        self.disposition = disposition


def leave_workspace(*, principal, authorizer, clerk_client, store,
                    clerk_organization_id):
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
    try:
        try:
            clerk_client.delete_organization_membership(
                clerk_organization_id, context.clerk_user_id)
        except ClerkResourceAbsent:
            pass
        memberships = clerk_client.user_organization_memberships(
            context.clerk_user_id)
        if any(row.organization_id == clerk_organization_id for row in memberships):
            raise AccessControlBlocked("clerk_membership_not_terminal",
                                       disposition="retryable")
    except AccessControlBlocked:
        raise
    except ClerkMembershipUnavailable:
        raise AccessControlBlocked("clerk_provider_failure",
                                   disposition="retryable") from None
    store.complete_workspace_departure(
        operation_id=operation["operation_id"],
        clerk_organization_id=clerk_organization_id,
        clerk_user_id=context.clerk_user_id)
    return {"state": "left"}
