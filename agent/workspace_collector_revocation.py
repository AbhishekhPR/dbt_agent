"""Tenant-scoped collector token and identity revocation."""
from agent.workspace_credential_revocation import CUSTOMER_REMEDIATION
from agent.lifecycle_authorization import RecentVerificationRequired, require_recent_verification


class CollectorAccessBlocked(RuntimeError):
    def __init__(self, category):
        super().__init__(category.replace("_", " "))
        self.category, self.disposition = category, "blocked"


def revoke_collector_access(*, principal, authorizer, store, token_id=None):
    try:
        require_recent_verification(principal)
    except RecentVerificationRequired as exc:
        raise CollectorAccessBlocked(exc.category) from None
    context = authorizer.require_admin_or_owner(principal)
    result = store.revoke_tenant_collector_access(
        tenant_id=context.tenant_id, token_id=token_id,
        initiated_by_clerk_user_id=context.clerk_user_id)
    return {**result, "customer_remediation": CUSTOMER_REMEDIATION[:2]}
