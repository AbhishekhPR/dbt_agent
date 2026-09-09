"""Authorization checks shared by destructive lifecycle operations."""
from __future__ import annotations


RECENT_VERIFICATION_MINUTES = 10


class RecentVerificationRequired(RuntimeError):
    def __init__(self, category="recent_verification_required"):
        super().__init__(category.replace("_", " "))
        self.category = category


def require_recent_verification(principal, *, maximum_age_minutes=None):
    """Require Clerk's signed strongest-available factor to be recent.

    Clerk represents an unavailable second factor as ``-1``. When a second
    factor exists, both factors must be within the strict window. A boundary
    value equal to the window is stale: the proof must be *within* it.
    """
    if (principal is None
            or getattr(principal, "identity_provider", None) != "clerk"):
        raise RecentVerificationRequired()
    if getattr(principal, "is_impersonated", False):
        raise RecentVerificationRequired("impersonated_session")
    ages = getattr(principal, "factor_verification_age", None)
    limit = (RECENT_VERIFICATION_MINUTES if maximum_age_minutes is None
             else maximum_age_minutes)
    if (not isinstance(ages, tuple) or len(ages) != 2
            or ages[0] < 0 or ages[0] >= limit
            or (ages[1] != -1 and (ages[1] < 0 or ages[1] >= limit))):
        raise RecentVerificationRequired()
