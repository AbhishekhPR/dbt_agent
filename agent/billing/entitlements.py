"""What each plan is entitled to. The ONE authoritative catalog.

###################################################################
# NOTHING OUTSIDE THIS MODULE DECIDES WHAT A PLAN INCLUDES.       #
###################################################################

Feature code never writes ``if plan == "pro"``. It asks a capability question:

    entitlements = get_workspace_entitlements(store, tenant_id, settings)
    if not entitlements.warehouse_evidence: ...
    if entitlements.exceeds_repositories(count): ...

so that changing what Starter includes is an edit to ONE table below, not a
search across the codebase for plan-name comparisons that have quietly drifted
apart.

WHAT RELIUM CHARGES FOR, AND WHAT IT NEVER CHARGES FOR
------------------------------------------------------
The paid tiers buy evidence depth, scale, retention, automation, enforcement
and governance. They do NOT buy correctness. A Free workspace gets the same
SQL/dbt analysis, the same downstream blast radius, the same schema-breaking
detection, the same core semantic change detection and the same ALLOW/WARN/BLOCK
decision as a Pro workspace, computed the same way.

That is a rule about this file as much as about the product: no capability here
may ever be one that makes the analysis deliberately worse. Where a paid input
(warehouse evidence, runtime evidence) is unavailable, the answer is an explicit
capability state the caller can see and act on -- never a quietly degraded
verdict that looks like a real one.

FAIL CLOSED, WITH ONE DELIBERATE EXCEPTION
------------------------------------------
An unrecognised plan name resolves to FREE. A plan name is only ever read back
out of Relium's own database, written there by a signature-verified Polar
webhook -- but if one ever arrives that this catalog does not know, the safe
answer is the weakest one.

The exception is ``UNMETERED``, used when the deployment has no Polar
configuration at all. Such a deployment sells nothing and nobody on it can
upgrade, so metering it would cripple a self-hosted or pre-launch install with
a limit it has no way to lift. That is a property of DEPLOYMENT configuration,
which only an operator can set -- never of anything a tenant or a browser can
influence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from agent.billing.plans import PLAN_FREE, PLAN_PRO, PLAN_STARTER

#: Capability names, as they appear in the API payload and in tests. Constants
#: rather than bare strings so a typo is an ImportError instead of a silently
#: false capability check.
REPOSITORY_LIMIT = "repository_limit"
MEMBER_LIMIT = "member_limit"
HISTORY_RETENTION_DAYS = "history_retention_days"
WAREHOUSE_EVIDENCE = "warehouse_evidence"
RUNTIME_EVIDENCE = "runtime_evidence"
CUSTOM_REVIEW_POLICIES = "custom_review_policies"
MERGE_BLOCKING = "merge_blocking"
GOVERNANCE_CONTROLS = "governance_controls"

#: Every capability, in the order the plan comparison renders them.
CAPABILITIES = (
    REPOSITORY_LIMIT,
    MEMBER_LIMIT,
    HISTORY_RETENTION_DAYS,
    WAREHOUSE_EVIDENCE,
    RUNTIME_EVIDENCE,
    CUSTOM_REVIEW_POLICIES,
    MERGE_BLOCKING,
    GOVERNANCE_CONTROLS,
)

#: The machine-readable refusal a caller gets when a capability is not included.
#: One code, with the capability named in the body, so a client can branch on
#: the code and render the right upgrade prompt from the capability.
CODE_PLAN_UPGRADE_REQUIRED = "plan_upgrade_required"


@dataclass(frozen=True)
class PlanEntitlements:
    """What one plan includes.

    A ``None`` limit means unlimited. It is deliberately not a large integer:
    "unlimited" and "a big number" behave differently at the boundary, and a
    sentinel that reads as a number is how an off-by-one becomes a customer
    who cannot connect their tenth repository.
    """

    repository_limit: int | None
    member_limit: int | None
    history_retention_days: int | None
    warehouse_evidence: bool
    runtime_evidence: bool
    custom_review_policies: bool
    merge_blocking: bool
    governance_controls: bool

    def allows(self, capability) -> bool:
        """True when a BOOLEAN capability is included.

        Limits are not booleans and are deliberately refused here: asking
        ``allows(REPOSITORY_LIMIT)`` would be true for every plan including a
        limit of zero, which is exactly the kind of check that looks correct
        and enforces nothing.
        """
        value = getattr(self, capability, None)
        if not isinstance(value, bool):
            raise ValueError(
                f"{capability!r} is a limit, not a boolean capability; "
                f"compare it with the appropriate exceeds_* helper")
        return value

    # -- limits ------------------------------------------------------------
    #
    # Each asks "would this many be too many", never "what is the limit", so
    # the None-means-unlimited rule is applied in one place per limit rather
    # than at every call site.

    def exceeds_repositories(self, count) -> bool:
        return self.repository_limit is not None and count > self.repository_limit

    def exceeds_members(self, count) -> bool:
        return self.member_limit is not None and count > self.member_limit

    @property
    def unlimited_history(self) -> bool:
        return self.history_retention_days is None

    def as_payload(self) -> dict:
        """The capability object sent to the dashboard.

        Only capabilities. No product id, no customer id, no subscription id,
        no amount, no Polar metadata of any kind -- a browser needs to know
        what it may do, and every identifier that appears in a response is one
        that will eventually be sent back in a request.
        """
        return asdict(self)


#: FREE -- UNDERSTAND. The full core change-intelligence loop on one
#: repository: SQL/dbt analysis, downstream blast radius, schema-breaking
#: detection, core semantic change detection, the ALLOW/WARN/BLOCK decision and
#: the GitHub check. What it does not include is paid EVIDENCE, scale,
#: retention and enforcement.
FREE = PlanEntitlements(
    repository_limit=1,
    member_limit=2,
    history_retention_days=7,
    warehouse_evidence=False,
    runtime_evidence=False,
    custom_review_policies=False,
    merge_blocking=False,
    governance_controls=False,
)

#: STARTER -- VERIFY. Production verification: the same analysis, now with
#: warehouse and runtime evidence behind it and a history long enough to review
#: against. Policy authoring stays closed; Starter uses Relium's standard
#: production policy set.
STARTER = PlanEntitlements(
    repository_limit=3,
    member_limit=10,
    history_retention_days=90,
    warehouse_evidence=True,
    runtime_evidence=True,
    custom_review_policies=False,
    merge_blocking=False,
    governance_controls=False,
)

#: PRO -- ENFORCE. Relium becomes a release gate: merge blocking, custom
#: policies, governance, and no ceiling on scale or retention.
PRO = PlanEntitlements(
    repository_limit=None,
    member_limit=None,
    history_retention_days=None,
    warehouse_evidence=True,
    runtime_evidence=True,
    custom_review_policies=True,
    merge_blocking=True,
    governance_controls=True,
)

#: A deployment with no Polar configuration. See the module docstring: this is
#: reachable only from operator-set environment, never from tenant state.
UNMETERED = PlanEntitlements(
    repository_limit=None,
    member_limit=None,
    history_retention_days=None,
    warehouse_evidence=True,
    runtime_evidence=True,
    custom_review_policies=True,
    merge_blocking=True,
    governance_controls=True,
)

#: plan name -> entitlements. The authoritative mapping, and the only one.
ENTITLEMENTS = {
    PLAN_FREE: FREE,
    PLAN_STARTER: STARTER,
    PLAN_PRO: PRO,
}


def entitlements_for(plan) -> PlanEntitlements:
    """The entitlements of a plan name. Anything unrecognised is FREE.

    Deliberately total: there is no "unknown plan" error to handle at a call
    site, because a call site that has to handle one is a call site that might
    handle it by continuing.
    """
    return ENTITLEMENTS.get(plan, FREE)


def plan_including(capability) -> str | None:
    """The weakest plan that includes a boolean capability, for upgrade copy.

    Returns None when no plan includes it, which today cannot happen but would
    be the honest answer if a capability were ever added ahead of the plan that
    sells it.
    """
    for plan in (PLAN_FREE, PLAN_STARTER, PLAN_PRO):
        entitlements = ENTITLEMENTS[plan]
        value = getattr(entitlements, capability, None)
        if value is True:
            return plan
    return None
