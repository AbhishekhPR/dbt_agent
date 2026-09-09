"""Owner-authorized, tenant-scoped credential revocation primitives."""
from __future__ import annotations


CUSTOMER_REMEDIATION = (
    "rotate_or_drop_warehouse_role",
    "remove_warehouse_collector_configuration",
    "remove_github_actions_relium_ci_token",
    "historical_external_publications_are_not_erased",
)


class CredentialRevocationError(RuntimeError):
    def __init__(self, category):
        super().__init__(category.replace("_", " "))
        self.category = category


def revoke_workspace_credentials(*, principal, authorizer, store):
    """Revoke credentials only for the refreshed owner workspace context."""
    context = authorizer.require_owner(principal)
    status_reader = getattr(store, "workspace_credential_revocation_status", None)
    existing = status_reader(context.tenant_id) if status_reader else None
    if existing is not None:
        return {**existing, "customer_remediation": CUSTOMER_REMEDIATION}
    inventory = store.tenant_operational_inventory(context.tenant_id)
    if inventory.get("ownership_status") != "complete":
        raise CredentialRevocationError("operational_ownership_incomplete")
    roots = tuple(sorted(inventory.get("operational_roots") or ()))
    result = store.revoke_workspace_credentials_for_tenant(
        tenant_id=context.tenant_id, expected_operational_roots=roots,
        clerk_user_id=context.clerk_user_id)
    return {**result, "customer_remediation": CUSTOMER_REMEDIATION}


def revoke_current_user_github_identity(*, principal, store):
    """Destroy only the authenticated Clerk user's stored GitHub credential."""
    clerk_user_id = getattr(principal, "clerk_user_id", None)
    if (getattr(principal, "identity_provider", None) != "clerk"
            or not isinstance(clerk_user_id, str) or not clerk_user_id):
        raise CredentialRevocationError("authenticated_clerk_user_required")
    return store.revoke_current_user_github_identity(clerk_user_id)
