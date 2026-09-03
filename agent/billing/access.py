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

from agent.billing.entitlements import UNMETERED, entitlements_for
from agent.billing.plans import PAID_PLANS, PLAN_FREE, access_state, at_least

__all__ = [
    "get_workspace_plan", "workspace_has_paid_access", "at_least",
    "get_workspace_entitlements", "entitlements_for_scope", "tenant_for_scope",
    "tenant_for_repository", "entitlements_for_repository",
    "review_entitlements",
]


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


def get_workspace_entitlements(store, tenant_id, settings=None, *, now=None):
    """What this workspace may do right now. The question feature code asks.

    ###################################################################
    # NO POLAR CONFIGURATION MEANS NO METERING.                       #
    ###################################################################

    ``settings is None`` is how the process says this deployment has no Polar
    configuration -- the billing service was never built, ``/api/billing/*``
    answers 503, and nobody on this deployment can reach a checkout. Metering
    it would leave a self-hosted or pre-launch install permanently pinned to
    Free with no way to lift the limit, so it is UNMETERED instead.

    That branch is reachable only from operator-set environment. Tenant state
    cannot produce it: a configured deployment always passes its settings, and
    every workspace on it is metered from the webhook-backed row.
    """
    if settings is None:
        return UNMETERED
    return entitlements_for(get_workspace_plan(store, tenant_id, settings, now=now))


def tenant_for_repository(store, organization_id, repository_id):
    """The tenant that owns a repository, by GitHub owner login and name.

    Billing is keyed by tenant while the review lifecycle knows only the
    repository coordinates a webhook carried. This is the join, and it reads a
    mapping Relium wrote during onboarding; it never creates one.

    Returns None when the repository was never onboarded through a tenant,
    which is the case for a deployment that predates Clerk tenancy. Callers
    treat None as "not metered": refusing evidence for a repository that has no
    workspace would break an install that was working before entitlements
    existed, and there is no subscription to enforce against anyway.
    """
    if not organization_id or not repository_id:
        return None
    reader = getattr(store, "tenant_for_repository_slug", None)
    if reader is None:
        return None
    return reader(str(organization_id), str(repository_id))


def tenant_for_scope(store, scope):
    """The tenant that owns the repository a service-token scope points at.

    Service tokens are scoped to an organization and a repository -- the GitHub
    owner login and repository name -- which is exactly the pair
    ``tenant_for_repository`` resolves.
    """
    return tenant_for_repository(
        store,
        getattr(scope, "organization_id", None),
        getattr(scope, "repository_id", None),
    )


def entitlements_for_scope(store, scope, settings=None, *, now=None):
    """Entitlements for a service-token caller. See ``tenant_for_scope``."""
    return entitlements_for_repository(
        store,
        getattr(scope, "organization_id", None),
        getattr(scope, "repository_id", None),
        settings, now=now)


def entitlements_for_repository(store, organization_id, repository_id,
                                settings=None, *, now=None):
    """Entitlements for the workspace that owns a repository.

    The same question ``entitlements_for_scope`` answers, asked with the plain
    coordinates the review lifecycle holds instead of a token scope.
    """
    if settings is None:
        return UNMETERED
    tenant_id = tenant_for_repository(store, organization_id, repository_id)
    if tenant_id is None:
        return UNMETERED
    return get_workspace_entitlements(store, tenant_id, settings, now=now)


def review_entitlements(store, organization_id, repository_id, *,
                        environ=None, now=None):
    """Entitlements for a review, resolved outside any request path.

    ###################################################################
    # THE ONE ENTITLEMENT ANSWER THE REVIEW LIFECYCLE USES.           #
    ###################################################################

    The webhook runner and the durable lifecycle worker are separate processes
    that both begin reviews, and neither of them is handed built billing
    configuration the way an API route is. Reading the environment here is
    what makes the two agree: a review resumed by the worker must reach the
    same answer as the same review begun by the runner, and it cannot do that
    from configuration only one of them was given.

    ``environ`` is a parameter rather than an implicit read so a test can vary
    it. A deployment with no Polar configuration resolves to UNMETERED, which
    is exactly what every deployment did before entitlements existed.

    A PolarConfigurationError is deliberately NOT swallowed. Both composition
    roots validate this configuration at boot, so reaching this function with
    broken configuration means it changed under a running process; failing the
    job leaves it on the outbox to retry rather than silently deciding the
    workspace is unmetered and collecting paid evidence for free.
    """
    from agent.billing.config import PolarSettings

    settings = PolarSettings.from_environ(environ)
    return entitlements_for_repository(
        store, organization_id, repository_id, settings, now=now)


def _grace(settings):
    from datetime import timedelta

    return getattr(settings, "past_due_grace", None) or timedelta(0)
