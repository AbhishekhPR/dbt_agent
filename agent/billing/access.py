"""The one place the rest of Relium asks what a workspace is entitled to.

###################################################################
# NOTHING OUTSIDE THIS MODULE READS A SUBSCRIPTION STATUS.        #
###################################################################

Feature limits do not exist yet, and this file deliberately does not invent
any. What it does is fix the shape of the question, so that when the first limit
arrives it is written as

    if not workspace_has_paid_access(store, tenant_id, settings): ...
    if at_least(get_workspace_plan(store, tenant_id, settings), PLAN_PRO): ...

rather than as another copy of the status vocabulary. Every entitlement decision
resolves through ``agent.billing.plans.access_state``, so a change to what
`past_due` means changes it everywhere at once.

``settings`` is passed in rather than read from the environment here: this is
called from request paths that already hold the built configuration, and a
module-level read would make the answer depend on process state that a test
cannot vary.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.billing.plans import PAID_PLANS, PLAN_FREE, access_state, at_least

__all__ = ["get_workspace_plan", "workspace_has_paid_access", "at_least"]


def get_workspace_plan(store, tenant_id, settings=None, *, now=None):
    """The plan a workspace is entitled to RIGHT NOW: free, starter or pro.

    Not the plan it once bought. A workspace whose subscription Polar has
    revoked returns ``free`` here even though the row still records which
    product it was, because the row is history and this is entitlement.
    """
    if not tenant_id:
        return PLAN_FREE
    record = store.billing_for_tenant(tenant_id)
    if not record:
        return PLAN_FREE
    plan, _ = access_state(
        plan=record.get("plan") or PLAN_FREE,
        status=record.get("subscription_status"),
        past_due_at=record.get("past_due_at"),
        now=now or datetime.now(timezone.utc),
        past_due_grace=_grace(settings))
    return plan


def workspace_has_paid_access(store, tenant_id, settings=None, *, now=None):
    """True when the workspace is on any paid plan."""
    return get_workspace_plan(store, tenant_id, settings, now=now) in PAID_PLANS


def _grace(settings):
    from datetime import timedelta

    return getattr(settings, "past_due_grace", None) or timedelta(0)
