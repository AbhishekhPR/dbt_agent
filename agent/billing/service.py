"""Billing operations, between the HTTP routes and Polar.

The routes resolve the tenant from a verified Clerk token and then call one of
these. Nothing here reads a tenant, a customer id, a subscription id, a product
id or a price out of a request body — every one of those is either the tenant id
the route proved, or a value this deployment was configured with.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from agent.billing.client import PolarAPIError
from agent.billing.plans import (
    PLAN_FREE, PURCHASABLE_PLANS, access_state, has_live_subscription,
    plan_for_product, product_for_plan,
)

logger = logging.getLogger(__name__)

#: Relium's tenant identifier format, from migration 0014's CHECK constraint.
#: Applied to values arriving from a webhook payload before they are used as a
#: tenant id: a signature proves Polar sent the delivery, not that a field
#: inside it is one of our identifiers.
_TENANT_ID = re.compile(r"^ten_[0-9a-f]{32}$")

#: The metadata key Relium sets on a checkout, and therefore on the resulting
#: subscription and customer. A second, independent path from a webhook back to
#: the workspace.
TENANT_METADATA_KEY = "relium_tenant_id"

#: Bound on identifiers taken out of a Polar payload before they are stored.
MAX_POLAR_ID = 255


class BillingError(Exception):
    """A billing operation could not be completed. ``code`` is the contract."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


#: The plan asked for is not one that can be bought.
CODE_UNKNOWN_PLAN = "unknown_plan"
#: Billing is not configured on this deployment.
CODE_NOT_CONFIGURED = "billing_not_configured"
#: Polar could not be reached or refused.
CODE_PROVIDER_UNAVAILABLE = "billing_provider_unavailable"
#: The workspace has no Polar customer yet, so there is no portal to open.
CODE_NO_BILLING_ACCOUNT = "no_billing_account"
#: The workspace already has a live subscription. Changing plan is an update to
#: that subscription, not a second purchase.
CODE_SUBSCRIPTION_EXISTS = "subscription_exists"


class BillingService:
    def __init__(self, settings, client, *, app_url="", clock=None):
        self._settings = settings
        self._client = client
        self._app_url = (app_url or "").rstrip("/")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def settings(self):
        return self._settings

    def now(self):
        """The service's clock, as an aware datetime.

        One clock for the whole integration: the webhook's replay window and the
        payment-recovery grace period are both measured with it, so a test can
        move time without the two disagreeing about when "now" is.
        """
        return self._clock()

    # -- checkout ---------------------------------------------------------

    def create_checkout(self, store, tenant_id, plan):
        """A Polar checkout URL for one workspace and one configured plan.

        ``plan`` is the ONLY caller-supplied input, and it is checked against a
        fixed list of two names before it is used. The product id is then looked
        up in this deployment's configuration — a caller cannot name a product,
        an amount, or a price, so there is no request that can buy something
        Relium did not put on sale.
        """
        if plan not in PURCHASABLE_PLANS:
            raise BillingError(CODE_UNKNOWN_PLAN)

        # ###########################################################
        # # A SECOND CHECKOUT IS A SECOND SUBSCRIPTION.             #
        # ###########################################################
        #
        # Polar's checkout creates a NEW subscription; it does not move an
        # existing one to a different product. A workspace already paying for
        # Starter that "upgrades" through checkout would end up paying for
        # Starter AND Pro, with two renewals, and Relium's one-row-per-workspace
        # model could only ever record the second — leaving a live subscription
        # nothing in Relium knows about, still charging.
        #
        # Changing plan is an UPDATE to the existing subscription, with
        # proration, and Polar's hosted portal does exactly that when
        # "Enable subscription plan changes" is on. So this refuses, with a code
        # the frontend turns into "Change in billing".
        #
        # The test is whether POLAR still has a live subscription, NOT whether
        # this workspace is currently entitled to a plan. The two differ exactly
        # where it matters: a `past_due` subscription grants no access under the
        # default zero grace, and a `paused` one grants none at all, yet both can
        # start charging again by themselves -- past_due when a retry in Polar's
        # 21-day schedule succeeds, paused when it resumes. Asking the
        # entitlement question here would sell a second subscription to a
        # customer who already has one that is merely not paying out right now.
        record = store.billing_for_tenant(tenant_id)
        if record and has_live_subscription(record.get("subscription_status")):
            raise BillingError(CODE_SUBSCRIPTION_EXISTS)

        product_id = product_for_plan(
            plan,
            starter_product_id=self._settings.starter_product_id,
            pro_product_id=self._settings.pro_product_id)
        if not product_id:
            raise BillingError(CODE_NOT_CONFIGURED)

        try:
            session = self._client.create_checkout_session(
                product_id=product_id,
                # The workspace, and the whole basis of the association.
                external_customer_id=tenant_id,
                success_url=self.success_url(),
                metadata={TENANT_METADATA_KEY: tenant_id},
                customer_metadata={TENANT_METADATA_KEY: tenant_id},
            )
        except PolarAPIError as error:
            logger.error("billing_checkout_failed",
                         extra={"error_category": "billing_provider",
                                "operation": error.operation,
                                "http_status": error.status_code})
            raise BillingError(CODE_PROVIDER_UNAVAILABLE) from None

        url = session.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            logger.error("billing_checkout_url_missing",
                         extra={"error_category": "billing_provider"})
            raise BillingError(CODE_PROVIDER_UNAVAILABLE)
        checkout_id = session.get("id")
        return {
            "checkout_url": url,
            # Returned for support and correlation only. It is not a credential
            # and it grants nothing: the plan is decided by the webhook.
            "checkout_id": checkout_id if isinstance(checkout_id, str) else None,
            "plan": plan,
        }

    def success_url(self) -> str:
        """Where Polar returns the customer.

        ###################################################################
        # THIS URL GRANTS NOTHING. IT IS A PLACE TO WAIT.                 #
        ###################################################################

        The page it lands on re-reads GET /api/billing/subscription, which is
        served from the database the webhook writes. Arriving here with any
        query string at all changes no stored state.
        """
        base = self._app_url or ""
        return f"{base}/settings?section=billing&billing=success"

    # -- customer portal ---------------------------------------------------

    def create_portal_session(self, store, tenant_id):
        """A Polar-hosted customer portal URL for this workspace's customer.

        The customer is addressed by the workspace's own external id. A caller
        cannot pass a customer id, and the record is read for THIS tenant, so
        there is no request shape that opens another workspace's portal.
        """
        record = store.billing_for_tenant(tenant_id)
        if not record or not record.get("polar_customer_id"):
            # Nothing has been bought yet, so Polar has no customer for this
            # workspace and a portal session would be minted against nothing.
            raise BillingError(CODE_NO_BILLING_ACCOUNT)

        try:
            session = self._client.create_customer_session(
                external_customer_id=tenant_id,
                return_url=self.portal_return_url())
        except PolarAPIError as error:
            logger.error("billing_portal_failed",
                         extra={"error_category": "billing_provider",
                                "operation": error.operation,
                                "http_status": error.status_code})
            raise BillingError(CODE_PROVIDER_UNAVAILABLE) from None

        url = session.get("customer_portal_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            logger.error("billing_portal_url_missing",
                         extra={"error_category": "billing_provider"})
            raise BillingError(CODE_PROVIDER_UNAVAILABLE)
        # `token` is in the response and is a customer credential. It is not
        # read, not logged, and not returned: the portal URL already carries
        # what the browser needs.
        return {"portal_url": url}

    def portal_return_url(self) -> str:
        base = self._app_url or ""
        return f"{base}/settings?section=billing"

    # -- status ------------------------------------------------------------

    def subscription_view(self, store, tenant_id):
        """What the dashboard is told about this workspace's billing."""
        return self.view_for_record(store.billing_for_tenant(tenant_id))

    def view_for_record(self, record):
        if not record:
            return {
                "plan": PLAN_FREE,
                "status": None,
                "is_active": False,
                "cancel_at_period_end": False,
                "current_period_end": None,
                "has_billing_account": False,
            }
        effective_plan, is_active = access_state(
            plan=record.get("plan") or PLAN_FREE,
            status=record.get("subscription_status"),
            past_due_at=record.get("past_due_at"),
            now=self._clock(),
            past_due_grace=self._settings.past_due_grace)
        return {
            # What the workspace currently HAS, not what it once bought. The
            # two differ for a revoked or lapsed subscription, and the one the
            # product must act on is this.
            "plan": effective_plan,
            "status": record.get("subscription_status"),
            "is_active": is_active,
            "cancel_at_period_end": bool(record.get("cancel_at_period_end")),
            "current_period_end": record.get("current_period_end"),
            # Whether Polar has a customer for this workspace, which is what
            # decides whether "Manage billing" can do anything. The Polar
            # customer id itself is deliberately NOT disclosed: the browser has
            # no use for it, and an identifier that appears in a response is one
            # that will eventually be sent back in a request.
            "has_billing_account": bool(record.get("polar_customer_id")),
        }

    # -- webhook -----------------------------------------------------------

    def apply_subscription_event(self, store, event_type, data):
        """Apply one verified subscription event. Returns an outcome string.

        ``ignored``   the event named no workspace this deployment knows
        ``stale``     an older object arrived after a newer one
        ``applied``   the workspace's billing row now reflects this object

        The signature has already been verified and the delivery already
        de-duplicated by the caller. This function is still written to be safe
        under replay on its own: every write is an upsert keyed on the tenant,
        guarded by the object's own modification time.
        """
        if not isinstance(data, dict):
            return "ignored"

        tenant_id = self._tenant_from(data) or resolve_tenant_by_customer(store, data)
        if tenant_id is None:
            # Deliberately not an error. A Polar organization may sell products
            # that have nothing to do with Relium workspaces, and a delivery
            # Relium cannot place is acknowledged rather than retried forever.
            logger.info("billing_event_unattributed",
                        extra={"operation": event_type})
            return "ignored"

        subscription_id = _polar_id(data.get("id"))
        if subscription_id is None:
            return "ignored"

        product_id = _polar_id(data.get("product_id"))
        plan = plan_for_product(
            product_id,
            starter_product_id=self._settings.starter_product_id,
            pro_product_id=self._settings.pro_product_id)
        if plan == PLAN_FREE and product_id is not None:
            # A real subscription, to a product this deployment was not
            # configured with. Recorded so an operator can see it, and granting
            # nothing — this is the path that stops a product created by mistake
            # in the Polar dashboard from handing out a paid plan.
            logger.warning("billing_unconfigured_product",
                           extra={"operation": event_type,
                                  "error_category": "billing_configuration"})

        status = data.get("status")
        status = status if isinstance(status, str) and len(status) <= 64 else None

        customer = data.get("customer")
        customer_id = _polar_id(data.get("customer_id")) or (
            _polar_id(customer.get("id")) if isinstance(customer, dict) else None)

        record = {
            "tenant_id": tenant_id,
            "polar_customer_id": customer_id,
            "polar_subscription_id": subscription_id,
            "polar_product_id": product_id,
            "plan": plan,
            "subscription_status": status,
            "current_period_end": _timestamp(data.get("current_period_end")),
            "cancel_at_period_end": bool(data.get("cancel_at_period_end")),
            "past_due_at": _timestamp(data.get("past_due_at")),
            # Ordering key. `modified_at` is null on a freshly created object,
            # so the creation time stands in — it is never null, and the pair is
            # monotonic for one subscription.
            "subscription_modified_at": (
                _timestamp(data.get("modified_at"))
                or _timestamp(data.get("created_at"))),
        }
        return store.upsert_billing_from_subscription(**record)

    def _tenant_from(self, data):
        """Which workspace this subscription belongs to, or None.

        Three server-established paths, tried in order of directness. Every one
        of them is a value RELIUM put there — the external id and both metadata
        keys are set by ``create_checkout`` — or a mapping Relium already stored.
        An email address is never consulted: it identifies a person, it is
        changeable, and treating it as an authorization input is how one
        customer's mailbox becomes another workspace's subscription.
        """
        customer = data.get("customer")
        if isinstance(customer, dict):
            candidate = _tenant_id(customer.get("external_id"))
            if candidate:
                return candidate

        for source in (data.get("metadata"),
                       customer.get("metadata") if isinstance(customer, dict) else None):
            if isinstance(source, dict):
                candidate = _tenant_id(source.get(TENANT_METADATA_KEY))
                if candidate:
                    return candidate
        return None


def resolve_tenant_by_customer(store, data):
    """Fallback: the workspace already bound to this Polar customer.

    Separate from ``_tenant_from`` because it needs the database and because it
    is a WEAKER claim: it says Relium has seen this customer before, not that
    Polar is telling us whose it is. Used only after the payload's own
    server-set identifiers came up empty.
    """
    customer_id = _polar_id(data.get("customer_id"))
    if customer_id is None:
        customer = data.get("customer")
        if isinstance(customer, dict):
            customer_id = _polar_id(customer.get("id"))
    if customer_id is None:
        return None
    return store.tenant_for_polar_customer(customer_id)


def _tenant_id(value):
    if isinstance(value, str) and _TENANT_ID.match(value):
        return value
    return None


def _polar_id(value):
    if isinstance(value, str) and 0 < len(value) <= MAX_POLAR_ID:
        return value
    return None


def _timestamp(value):
    """Parse an RFC 3339 timestamp from Polar into an aware datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
