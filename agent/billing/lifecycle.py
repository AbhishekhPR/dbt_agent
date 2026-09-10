"""Fail-closed billing lifecycle classifications and orchestration."""
from __future__ import annotations

from datetime import datetime, timezone

from agent.billing.client import PolarAPIError


POTENTIALLY_BILLABLE_STATUSES = frozenset({
    "incomplete", "trialing", "active", "past_due", "paused",
})
TERMINAL_STATUSES = frozenset({"canceled", "unpaid", "incomplete_expired"})
ACTIONABLE_CHECKOUT_STATUSES = frozenset({"open", "confirmed"})
TERMINAL_CHECKOUT_STATUSES = frozenset({"expired", "succeeded", "failed"})


class BillingLifecycleError(RuntimeError):
    """Safe lifecycle failure. Provider documents and credentials are omitted."""

    def __init__(self, category):
        super().__init__(category.replace("_", " "))
        self.category = category


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
        if subscription.get("cancel_at_period_end") is True:
            return "scheduled_cancel"
        return "potentially_billable"
    return "unknown"


def reconcile_workspace_billing(*, principal, authorizer, store, client,
                                clock=None):
    """Reconcile Polar for the refreshed, authoritative owner workspace.

    This is deliberately not an entitlement write path. The observations are
    evidence for lifecycle safety; only verified subscription webhooks update
    ``tenant_billing``.
    """
    context = authorizer.require_owner(principal)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    operation = store.begin_billing_lifecycle_operation(
        tenant_id=context.tenant_id, operation_kind="reconcile",
        block_checkout=False, now=now)
    try:
        observations = _collect_provider_state(
            tenant_id=context.tenant_id, store=store, client=client)
        store.save_billing_reconciliation(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"],
            subscriptions=observations["subscriptions"],
            checkouts=observations["checkouts"], observed_at=now)
    except BillingLifecycleError as error:
        store.finish_billing_lifecycle_operation(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"], state="ambiguous",
            failure_category=error.category, subscription_count=0,
            actionable_checkout_count=0, now=now)
        raise
    except PolarAPIError as error:
        category = _provider_failure_category(error)
        store.finish_billing_lifecycle_operation(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"], state="failed",
            failure_category=category, subscription_count=0,
            actionable_checkout_count=0, now=now)
        raise BillingLifecycleError(category) from None

    ambiguity = _reconciliation_ambiguity(observations)
    store.finish_billing_lifecycle_operation(
        tenant_id=context.tenant_id,
        operation_id=operation["operation_id"],
        state="ambiguous" if ambiguity else "reconciled",
        failure_category=ambiguity,
        subscription_count=len(observations["subscriptions"]),
        actionable_checkout_count=observations["actionable_checkout_count"],
        now=now)
    if ambiguity:
        raise BillingLifecycleError(ambiguity)

    return {
        "state": "reconciled",
        "operation_id": operation["operation_id"],
        "subscription_count": len(observations["subscriptions"]),
        "multiple_subscriptions": len(observations["subscriptions"]) > 1,
        "potentially_billable_count": sum(
            item["classification"] in {"potentially_billable", "scheduled_cancel"}
            for item in observations["subscriptions"]),
        "unknown_subscription_count": sum(
            item["classification"] == "unknown"
            for item in observations["subscriptions"]),
        "actionable_checkout_count": observations["actionable_checkout_count"],
    }


def _reconciliation_ambiguity(observations):
    if any(item["classification"] == "unknown"
           for item in observations["subscriptions"]):
        return "unknown_subscription_state"
    if observations["actionable_checkout_count"]:
        return "actionable_checkout"
    return None


def revoke_workspace_subscriptions(*, principal, authorizer, store, client,
                                   clock=None):
    """Immediately revoke every discovered subscription, then prove safety.

    Checkout is blocked before the first provider read. Provider calls are not
    held inside a database transaction; the durable operation makes partial
    completion explicit and retryable.
    """
    context = authorizer.require_owner(principal)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    operation = store.begin_billing_lifecycle_operation(
        tenant_id=context.tenant_id, operation_kind="revoke",
        block_checkout=True, now=now)
    try:
        initial = _collect_provider_state(
            tenant_id=context.tenant_id, store=store, client=client)
        store.save_billing_reconciliation(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"],
            subscriptions=initial["subscriptions"],
            checkouts=initial["checkouts"], observed_at=now)
    except BillingLifecycleError as error:
        _finish_failure(store, context.tenant_id, operation["operation_id"],
                        "ambiguous", error.category, now)
        raise
    except PolarAPIError as error:
        category = _provider_failure_category(error)
        _finish_failure(store, context.tenant_id, operation["operation_id"],
                        "failed", category, now)
        raise BillingLifecycleError(category) from None

    failures = []
    for subscription in initial["subscriptions"]:
        if subscription["classification"] == "terminal":
            continue
        subscription_id = subscription["polar_subscription_id"]
        try:
            client.revoke_subscription(subscription_id)
            store.record_billing_subscription_revocation_result(
                tenant_id=context.tenant_id,
                operation_id=operation["operation_id"],
                polar_subscription_id=subscription_id,
                outcome="provider_accepted", failure_category=None,
                recorded_at=(clock or (lambda: datetime.now(timezone.utc)))())
        except PolarAPIError as error:
            # A 404 is only a provisional outcome. The complete final listing
            # below must independently prove the object is absent.
            category = _provider_failure_category(error)
            if error.status_code == 404:
                store.record_billing_subscription_revocation_result(
                    tenant_id=context.tenant_id,
                    operation_id=operation["operation_id"],
                    polar_subscription_id=subscription_id,
                    outcome="absent_unconfirmed", failure_category=None,
                    recorded_at=(clock or (lambda: datetime.now(timezone.utc)))())
            else:
                failures.append(category)
                store.record_billing_subscription_revocation_result(
                    tenant_id=context.tenant_id,
                    operation_id=operation["operation_id"],
                    polar_subscription_id=subscription_id,
                    outcome="provider_failure", failure_category=category,
                    recorded_at=(clock or (lambda: datetime.now(timezone.utc)))())
    if failures:
        category = failures[0] if len(set(failures)) == 1 else "provider_partial_failure"
        _finish_failure(
            store, context.tenant_id, operation["operation_id"], "failed",
            category, now, subscription_count=len(initial["subscriptions"]),
            actionable_checkout_count=initial["actionable_checkout_count"])
        raise BillingLifecycleError(category)

    try:
        final = _collect_provider_state(
            tenant_id=context.tenant_id, store=store, client=client)
        observed_at = (clock or (lambda: datetime.now(timezone.utc)))()
        store.save_billing_reconciliation(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"],
            subscriptions=final["subscriptions"], checkouts=final["checkouts"],
            observed_at=observed_at)
    except BillingLifecycleError as error:
        _finish_failure(store, context.tenant_id, operation["operation_id"],
                        "ambiguous", error.category, now)
        raise
    except PolarAPIError as error:
        category = _provider_failure_category(error)
        _finish_failure(store, context.tenant_id, operation["operation_id"],
                        "failed", category, now)
        raise BillingLifecycleError(category) from None

    unknown = any(item["classification"] == "unknown"
                  for item in final["subscriptions"])
    billable = any(item["classification"] != "terminal"
                   for item in final["subscriptions"])
    blocker_reader = getattr(store, "billing_checkout_revocation_blocker", None)
    if blocker_reader:
        category = blocker_reader(context.tenant_id, now=observed_at)
    else:
        in_flight_reader = getattr(store, "billing_checkout_create_in_flight", None)
        category = ("checkout_create_in_flight"
                    if in_flight_reader and in_flight_reader(context.tenant_id)
                    else None)
    if not category:
        if final["actionable_checkout_count"]:
            category = "actionable_checkout"
        elif unknown:
            category = "unknown_subscription_state"
        elif billable:
            category = "subscription_still_billable"
    if category:
        _finish_failure(
            store, context.tenant_id, operation["operation_id"], "ambiguous",
            category, observed_at,
            subscription_count=len(final["subscriptions"]),
            actionable_checkout_count=final["actionable_checkout_count"])
        raise BillingLifecycleError(category)

    verified_finisher = getattr(
        store, "finish_billing_revocation_verified_safe", None)
    if verified_finisher:
        verified_finisher(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"],
            subscription_count=len(final["subscriptions"]),
            now=observed_at)
    else:
        store.finish_billing_lifecycle_operation(
            tenant_id=context.tenant_id,
            operation_id=operation["operation_id"], state="verified_safe",
            failure_category=None,
            subscription_count=len(final["subscriptions"]),
            actionable_checkout_count=0, now=observed_at)
    return {
        "state": "verified_safe",
        "operation_id": operation["operation_id"],
        "subscription_count": len(final["subscriptions"]),
        "multiple_subscriptions": len(initial["subscriptions"]) > 1,
        "actionable_checkout_count": 0,
    }


def _finish_failure(store, tenant_id, operation_id, state, category, now,
                    subscription_count=0, actionable_checkout_count=0):
    store.finish_billing_lifecycle_operation(
        tenant_id=tenant_id, operation_id=operation_id, state=state,
        failure_category=category, subscription_count=subscription_count,
        actionable_checkout_count=actionable_checkout_count, now=now)


def _collect_provider_state(*, tenant_id, store, client):
    billing = store.billing_for_tenant(tenant_id)
    persisted_customer = billing.get("polar_customer_id") if billing else None
    # All checkout views MUST precede all subscription views. A checkout can
    # transition into a subscription while these non-transactional provider
    # reads run. This ordering guarantees that the transition is observed on
    # at least one side: open before it happens, or subscribed afterwards.
    checkouts = _validated_union(
        client.list_checkouts(external_customer_id=tenant_id),
        (), tenant_id=tenant_id, expected_customer_id=persisted_customer,
        kind="checkout")
    if persisted_customer:
        checkouts = _validated_union(
            checkouts, client.list_checkouts(customer_id=persisted_customer),
            tenant_id=tenant_id, expected_customer_id=persisted_customer,
            kind="checkout")
    subscriptions = _validated_union(
        client.list_subscriptions(external_customer_id=tenant_id),
        (), tenant_id=tenant_id, expected_customer_id=persisted_customer,
        kind="subscription")
    if persisted_customer:
        subscriptions = _validated_union(
            subscriptions,
            client.list_subscriptions(customer_id=persisted_customer),
            tenant_id=tenant_id, expected_customer_id=persisted_customer,
            kind="subscription")

    normalized_subscriptions = tuple(
        _normalize_subscription(item) for item in subscriptions)
    normalized_checkouts = tuple(_normalize_checkout(item) for item in checkouts)
    return {
        "subscriptions": normalized_subscriptions,
        "checkouts": normalized_checkouts,
        "actionable_checkout_count": sum(
            item["actionable"] for item in normalized_checkouts),
    }


def _validated_union(first, second, *, tenant_id, expected_customer_id, kind):
    by_id = {}
    for item in tuple(first) + tuple(second):
        if not isinstance(item, dict):
            raise BillingLifecycleError("malformed_provider_state")
        identifier = item.get("id")
        customer = item.get("customer")
        customer = customer if isinstance(customer, dict) else {}
        external_id = customer.get("external_id") or item.get("external_customer_id")
        customer_id = item.get("customer_id") or customer.get("id")
        if (not _bounded(identifier, 255) or external_id != tenant_id
                or not _bounded(customer_id, 255)
                or (expected_customer_id and customer_id != expected_customer_id)):
            raise BillingLifecycleError("identity_conflict")
        existing = by_id.get(identifier)
        if existing is not None and existing != item:
            raise BillingLifecycleError("ambiguous_provider_state")
        by_id[identifier] = item
    return tuple(by_id[key] for key in sorted(by_id))


def _normalize_subscription(item):
    customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
    status = item.get("status")
    if not _bounded(status, 64):
        raise BillingLifecycleError("malformed_provider_state")
    product_id = item.get("product_id")
    if product_id is None and isinstance(item.get("product"), dict):
        product_id = item["product"].get("id")
    if product_id is not None and not _bounded(product_id, 255):
        raise BillingLifecycleError("malformed_provider_state")
    return {
        "polar_subscription_id": item["id"],
        "polar_customer_id": item.get("customer_id") or customer.get("id"),
        "polar_product_id": product_id,
        "provider_status": status,
        "cancel_at_period_end": item.get("cancel_at_period_end") is True,
        "classification": classify_subscription(item),
    }


def _normalize_checkout(item):
    customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
    status = item.get("status")
    if not _bounded(status, 64):
        raise BillingLifecycleError("malformed_provider_state")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    from agent.billing.service import CHECKOUT_INTENT_METADATA_KEY
    intent_id = metadata.get(CHECKOUT_INTENT_METADATA_KEY)
    if intent_id is not None and not _bounded(intent_id, 255):
        raise BillingLifecycleError("malformed_provider_state")
    actionable = status in ACTIONABLE_CHECKOUT_STATUSES
    if status not in ACTIONABLE_CHECKOUT_STATUSES | TERMINAL_CHECKOUT_STATUSES:
        actionable = True
    expires_at = _provider_timestamp(item.get("expires_at"))
    return {
        "polar_checkout_id": item["id"],
        "polar_customer_id": item.get("customer_id") or customer.get("id"),
        "provider_status": status,
        "checkout_intent_id": intent_id,
        "expires_at": expires_at,
        "actionable": actionable,
        # Transient recovery material. The PostgreSQL observation writer names
        # each stored field explicitly and deliberately does not persist this
        # bearer-like checkout URL.
        "url": item.get("url"),
    }


def _provider_failure_category(error):
    """Name the fault precisely. Every branch here still fails closed.

    ###################################################################
    # A MISSING STATUS IS NOT AUTOMATICALLY A TIMEOUT.                #
    ###################################################################

    This used to return `provider_timeout` for every error that carried no HTTP
    status, which is nine different faults wearing one name: the socket
    deadline, DNS, connect, TLS, and four distinct ways a paginated listing can
    fail to prove itself complete. Workspace deletion is the most
    pagination-heavy path in the system -- it lists checkouts and subscriptions
    by two identities, twice, around a mutation -- so it is exactly where a
    consistency failure is most likely and where being told "timeout" sends an
    operator hunting a network fault that was never there.

    The distinction is diagnostic only. All of these remain retryable and none
    of them lets a caller conclude billing is stopped.
    """
    status = error.status_code
    if status in {401, 403}:
        return "provider_auth"
    if status == 429:
        return "provider_rate_limit"
    if isinstance(status, int) and 500 <= status <= 599:
        return "provider_5xx"
    if status is None:
        kind = getattr(error, "failure_kind", None)
        if kind == "unreachable":
            return "provider_unreachable"
        if kind == "inconsistent":
            return "provider_inconsistent"
        # Unclassified stays `provider_timeout`: it is the pre-existing name and
        # the conservative reading of "we never got an answer".
        return "provider_timeout"
    return "provider_refused"


def _bounded(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum


def _provider_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise BillingLifecycleError("malformed_provider_state")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BillingLifecycleError("malformed_provider_state") from None
    if parsed.tzinfo is None:
        raise BillingLifecycleError("malformed_provider_state")
    return parsed
