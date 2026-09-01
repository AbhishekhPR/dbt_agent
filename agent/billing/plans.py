"""Plans, and what a Polar subscription status entitles a workspace to.

###################################################################
# ONE PLACE ANSWERS "WHAT DOES THIS WORKSPACE HAVE".              #
###################################################################

Every entitlement question goes through ``plan_for_product`` and
``access_state``. Scattering ``status == "active"`` checks through the
application is how a subscription state that Polar added later ends up granting
access somewhere and refusing it somewhere else.

THE STATUS VOCABULARY IS POLAR'S
--------------------------------
The names below are Polar's documented ``SubscriptionStatus`` enum, copied
verbatim: incomplete, incomplete_expired, trialing, active, past_due, canceled,
unpaid, paused. They are an ALLOW-LIST, not a translation table — a status this
module has never seen grants nothing, which is the safe direction for a
vocabulary the payment provider owns and may extend.

WHAT POLAR DOCUMENTS, AND WHAT RELIUM DOES WITH IT
--------------------------------------------------
    active     Paid access. This is also the status of a subscription that the
               customer has scheduled to cancel: Polar keeps it `active` with
               `cancel_at_period_end = true` until the period actually ends,
               "the customer keeps their benefits — they paid for that period".
               So a cancellation request never downgrades anybody here; the
               `subscription.revoked` delivery at the end of the period does.
    trialing   Paid access. Polar treats a trial as an entitled state.
    past_due   A renewal charge failed and Polar is retrying on its documented
               schedule (2, 5, 7, 7 days). Polar's own default is to revoke
               benefits immediately, with an organization-level grace period as
               an option. Relium cannot read that setting through the API, so it
               is mirrored as explicit configuration and defaults to the same
               answer Polar defaults to: no grace.
    paused     No access. Benefits are revoked when a pause takes effect.
    canceled   No access. Reached when the period ends, when a subscription is
    unpaid     revoked outright, or when payment recovery gives up.
    incomplete
    incomplete_expired
               No access. The first charge never succeeded.
"""
from __future__ import annotations

from datetime import timedelta

#: Relium's internal plan names. Stable, ours, and never derived from a price.
PLAN_FREE = "free"
PLAN_STARTER = "starter"
PLAN_PRO = "pro"

#: Every plan a workspace may be on, weakest first. The order is what makes
#: "is this at least Starter" answerable without a chain of comparisons.
PLAN_ORDER = (PLAN_FREE, PLAN_STARTER, PLAN_PRO)

PAID_PLANS = frozenset({PLAN_STARTER, PLAN_PRO})

#: Plans a customer may buy through Relium's own checkout. `free` is absent
#: deliberately: there is no product to sell, and accepting it would be a
#: checkout request that can never produce a Polar session.
PURCHASABLE_PLANS = (PLAN_STARTER, PLAN_PRO)

#: Polar's documented subscription statuses.
STATUS_INCOMPLETE = "incomplete"
STATUS_INCOMPLETE_EXPIRED = "incomplete_expired"
STATUS_TRIALING = "trialing"
STATUS_ACTIVE = "active"
STATUS_PAST_DUE = "past_due"
STATUS_CANCELED = "canceled"
STATUS_UNPAID = "unpaid"
STATUS_PAUSED = "paused"

KNOWN_STATUSES = frozenset({
    STATUS_INCOMPLETE, STATUS_INCOMPLETE_EXPIRED, STATUS_TRIALING,
    STATUS_ACTIVE, STATUS_PAST_DUE, STATUS_CANCELED, STATUS_UNPAID,
    STATUS_PAUSED,
})

#: Statuses that entitle a workspace to its paid plan unconditionally.
ENTITLED_STATUSES = frozenset({STATUS_ACTIVE, STATUS_TRIALING})

#: Entitled only while the configured recovery grace period has not elapsed.
GRACE_STATUSES = frozenset({STATUS_PAST_DUE})

#: Statuses in which POLAR STILL HAS A SUBSCRIPTION THAT CAN CHARGE AGAIN.
#:
#: ###################################################################
#: # "NOT ENTITLED" IS NOT THE SAME AS "FREE TO BUY AGAIN".          #
#: ###################################################################
#:
#: Deliberately WIDER than ``ENTITLED_STATUSES``. `past_due` and `paused` grant
#: no access, but Polar has not finished with either of them:
#:
#:   past_due  is inside the documented 21-day retry schedule and returns to
#:             `active` the instant one of those retries succeeds
#:   paused    stops billing for now and resumes -- charging immediately -- on
#:             its resume date or when the customer resumes it
#:
#: So a workspace in one of these states already owns a subscription that can
#: start charging again on its own. Selling it a second one through checkout
#: would leave the customer paying twice, with only the newer subscription
#: recorded here and the older one invisible and still live. Terminal statuses
#: -- canceled, unpaid, incomplete, incomplete_expired -- are absent: Polar is
#: done with those, and a customer whose plan ended must be able to come back.
LIVE_STATUSES = frozenset({
    STATUS_ACTIVE, STATUS_TRIALING, STATUS_PAST_DUE, STATUS_PAUSED,
})


def has_live_subscription(status) -> bool:
    """True when Polar still holds a subscription that can bill this workspace.

    The question ``create_checkout`` must ask before selling anything. It is NOT
    the entitlement question -- see ``access_state`` for that -- and the two
    deliberately disagree for `past_due` and `paused`.
    """
    return status in LIVE_STATUSES


def plan_for_product(product_id, *, starter_product_id, pro_product_id):
    """The internal plan a configured Polar product maps to.

    ###################################################################
    # AN UNCONFIGURED PRODUCT NEVER GRANTS A PAID PLAN.               #
    ###################################################################

    Returns ``free`` for anything that is not one of the two configured product
    ids — an unknown product, a product from another Polar organization, a
    product this deployment has not been told about, or nothing at all. A
    subscription to a product Relium cannot name is not an entitlement Relium
    can honour, and guessing from an amount would make $149 charged for
    something else look like Starter.
    """
    if not isinstance(product_id, str) or not product_id:
        return PLAN_FREE
    if starter_product_id and product_id == starter_product_id:
        return PLAN_STARTER
    if pro_product_id and product_id == pro_product_id:
        return PLAN_PRO
    return PLAN_FREE


def product_for_plan(plan, *, starter_product_id, pro_product_id):
    """The configured Polar product id for a plan, or None.

    The inverse of ``plan_for_product`` and the only way a checkout learns which
    product to sell. A product id supplied by a caller is never consulted.
    """
    if plan == PLAN_STARTER:
        return starter_product_id or None
    if plan == PLAN_PRO:
        return pro_product_id or None
    return None


def access_state(*, plan, status, past_due_at=None, now=None,
                 past_due_grace=timedelta(0)):
    """Whether a workspace currently has its paid plan, and the plan it has.

    Returns ``(effective_plan, is_active)``. ``effective_plan`` is what the
    application should enforce: the paid plan while the subscription entitles
    it, and ``free`` the moment it stops. ``is_active`` says whether Polar
    considers the subscription live, which is what the UI reports.

    A row can therefore say ``plan = 'pro'`` while this returns ``free``: the
    stored plan is what was bought, and this is what is currently owed. Nothing
    outside this function is allowed to make that judgement.
    """
    if plan not in PAID_PLANS:
        return PLAN_FREE, False
    if status in ENTITLED_STATUSES:
        return plan, True
    if status in GRACE_STATUSES:
        # Payment recovery. Entitled only for as long as the deployment's
        # configured grace period, measured — as Polar measures it — from the
        # instant of the first failed charge. With no grace configured, and with
        # no timestamp to measure from, this is False, which matches Polar's own
        # default of revoking immediately.
        if past_due_grace <= timedelta(0) or past_due_at is None or now is None:
            return PLAN_FREE, False
        return (plan, True) if now <= past_due_at + past_due_grace else (PLAN_FREE, False)
    return PLAN_FREE, False


def at_least(plan, required) -> bool:
    """True when ``plan`` is ``required`` or stronger.

    The comparison feature gating will use once limits exist, so that adding one
    does not mean writing another set of plan-name equality checks.
    """
    try:
        return PLAN_ORDER.index(plan) >= PLAN_ORDER.index(required)
    except ValueError:
        return False
