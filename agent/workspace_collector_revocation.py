"""Tenant-scoped collector token and identity revocation."""
from agent.workspace_credential_revocation import CUSTOMER_REMEDIATION


def revoke_collector_access(*, principal, authorizer, store, token_id=None):
    context = authorizer.require_admin_or_owner(principal)
    result = store.revoke_tenant_collector_access(
        tenant_id=context.tenant_id, token_id=token_id,
        initiated_by_clerk_user_id=context.clerk_user_id)
    return {**result, "customer_remediation": CUSTOMER_REMEDIATION[:2]}
