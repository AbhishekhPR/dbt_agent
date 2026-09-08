"""Fail-closed billing lifecycle classifications and orchestration."""
from __future__ import annotations


POTENTIALLY_BILLABLE_STATUSES = frozenset({
    "incomplete", "trialing", "active", "past_due",
})
TERMINAL_STATUSES = frozenset({"canceled", "unpaid", "incomplete_expired"})


def classify_subscription(subscription) -> str:
    """Classify one provider object without granting entitlement.

    Unknown or malformed states stay unknown so a lifecycle caller fails closed.
    """
    if not isinstance(subscription, dict):
        return "unknown"
    status = subscription.get("status")
    if status in TERMINAL_STATUSES:
        return "terminal"
    if status in POTENTIALLY_BILLABLE_STATUSES:
        if status == "active" and subscription.get("cancel_at_period_end") is True:
            return "scheduled_cancel"
        return "potentially_billable"
    return "unknown"

