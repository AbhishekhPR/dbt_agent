from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent.billing.client import PolarAPIError, PolarClient


MIGRATION = Path("agent/migrations/postgres/0023_billing_lifecycle_safety.sql")


class BillingLifecycleMigrationContractTests(unittest.TestCase):
    def test_schema_persists_lifecycle_operations_observations_and_intents(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_lifecycle_controls", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS billing_lifecycle_operations", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_polar_subscriptions", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_polar_checkouts", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS billing_checkout_intents", sql)
        self.assertGreaterEqual(sql.count("ON DELETE RESTRICT"), 5)
        self.assertIn(
            "WHERE state IN ('claimed', 'creating', 'provider_created', 'ambiguous')",
            sql)
        self.assertNotIn("lifecycle_generation BIGINT NOT NULL", sql)
        self.assertIn("create_lease_id TEXT", sql)
        self.assertIn("'creating'", sql)
        self.assertIn("checkout_generation BIGINT NOT NULL", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS billing_subscription_revocation_results", sql)

    def test_existing_tenants_are_backfilled_active_without_billing_inference(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("INSERT INTO tenant_lifecycle_controls", sql)
        self.assertIn("SELECT tenant_id, 'active', 'active' FROM tenants", sql)
        self.assertNotIn("FROM tenant_billing", sql)

    def test_migration_timeouts_are_transaction_local(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("SET LOCAL lock_timeout", sql)
        self.assertIn("SET LOCAL statement_timeout", sql)
        self.assertNotIn("\nSET lock_timeout", sql)


class PolarSubscriptionClassificationTests(unittest.TestCase):
    def test_provider_states_are_fail_closed(self):
        from agent.billing.lifecycle import classify_subscription

        self.assertEqual(classify_subscription({"status": "active"}),
                         "potentially_billable")
        self.assertEqual(classify_subscription(
            {"status": "active", "cancel_at_period_end": True}),
            "scheduled_cancel")
        self.assertEqual(classify_subscription({"status": "past_due"}),
                         "potentially_billable")
        self.assertEqual(classify_subscription({"status": "paused"}),
                         "potentially_billable")
        self.assertEqual(classify_subscription({"status": "canceled"}), "terminal")
        self.assertEqual(classify_subscription({"status": "unpaid"}), "terminal")
        self.assertEqual(classify_subscription({"status": "future_state"}), "unknown")
        self.assertEqual(classify_subscription({}), "unknown")


class PolarLifecycleClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token="polar_secret")

    def _client(self, responses):
        queue = list(responses)

        def transport(**request):
            self.calls.append(request)
            return queue.pop(0)

        return PolarClient(self.settings, transport=transport)

    def test_subscription_listing_follows_every_page_with_authoritative_filter(self):
        client = self._client([
            (200, json.dumps({"items": [{"id": "sub_one"}],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
            (200, json.dumps({"items": [{"id": "sub_two"}],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
        ])

        items = client.list_subscriptions(external_customer_id="ten_" + "a" * 32)

        self.assertEqual([item["id"] for item in items], ["sub_one", "sub_two"])
        self.assertIn("external_customer_id=ten_", self.calls[0]["url"])
        self.assertIn("page=2", self.calls[1]["url"])
        self.assertNotIn("polar_secret", " ".join(call["url"] for call in self.calls))

    def test_malformed_pagination_fails_closed(self):
        client = self._client([
            (200, json.dumps({"items": [], "pagination": {"max_page": 2}}).encode()),
        ])

        with self.assertRaises(PolarAPIError):
            client.list_subscriptions(external_customer_id="ten_" + "b" * 32)

    def test_duplicate_ids_across_pages_are_not_complete_evidence(self):
        duplicate = {"id": "sub_same"}
        client = self._client([
            (200, json.dumps({"items": [duplicate],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
            (200, json.dumps({"items": [duplicate],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
        ])
        with self.assertRaises(PolarAPIError):
            client.list_subscriptions(external_customer_id="ten_" + "c" * 32)

    def test_pagination_metadata_must_be_stable(self):
        client = self._client([
            (200, json.dumps({"items": [{"id": "sub_one"}],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
            (200, json.dumps({"items": [{"id": "sub_two"}],
                              "pagination": {"total_count": 3,
                                             "max_page": 2}}).encode()),
        ])
        with self.assertRaises(PolarAPIError):
            client.list_subscriptions(external_customer_id="ten_" + "d" * 32)

    def test_immediate_revoke_uses_delete_and_returns_validated_subscription(self):
        client = self._client([
            (200, json.dumps({"id": "sub_live", "status": "canceled"}).encode()),
        ])

        result = client.revoke_subscription("sub_live")

        self.assertEqual(result["status"], "canceled")
        self.assertEqual(self.calls[0]["method"], "DELETE")
        self.assertTrue(self.calls[0]["url"].endswith("/v1/subscriptions/sub_live"))

    def test_identifiers_cannot_escape_the_provider_path(self):
        client = self._client([])

        with self.assertRaises(ValueError):
            client.revoke_subscription("../customers")


class _LifecycleStore:
    def __init__(self, billing=None):
        self.billing = billing
        self.started = []
        self.saved = []
        self.finished = []
        self.checkout_create_in_flight = False
        self.revocation_results = []

    def billing_for_tenant(self, tenant_id):
        return self.billing

    def begin_billing_lifecycle_operation(self, *, tenant_id, operation_kind,
                                          block_checkout=False, now=None):
        self.started.append((tenant_id, operation_kind, block_checkout))
        return {"operation_id": "blo_test", "generation": 1}

    def save_billing_reconciliation(self, **values):
        self.saved.append(values)

    def finish_billing_lifecycle_operation(self, **values):
        self.finished.append(values)

    def billing_checkout_create_in_flight(self, tenant_id):
        return self.checkout_create_in_flight

    def record_billing_subscription_revocation_result(self, **values):
        self.revocation_results.append(values)


class _OwnerAuthorizer:
    def __init__(self, tenant_id="ten_" + "a" * 32, error=None):
        self.tenant_id = tenant_id
        self.error = error

    def require_owner(self, principal):
        if self.error:
            raise self.error
        return SimpleNamespace(tenant_id=self.tenant_id)


class _LifecycleClient:
    def __init__(self, *, external_subscriptions=(), customer_subscriptions=(),
                 external_checkouts=(), customer_checkouts=()):
        self.external_subscriptions = list(external_subscriptions)
        self.customer_subscriptions = list(customer_subscriptions)
        self.external_checkouts = list(external_checkouts)
        self.customer_checkouts = list(customer_checkouts)
        self.calls = []

    def list_subscriptions(self, **identity):
        self.calls.append(("subscriptions", identity))
        return (self.external_subscriptions if "external_customer_id" in identity
                else self.customer_subscriptions)

    def list_checkouts(self, **identity):
        self.calls.append(("checkouts", identity))
        return (self.external_checkouts if "external_customer_id" in identity
                else self.customer_checkouts)

    def revoke_subscription(self, subscription_id):
        raise AssertionError(f"unexpected revoke {subscription_id}")


def _subscription(identifier, tenant_id, customer_id="cus_live", status="active"):
    return {
        "id": identifier,
        "status": status,
        "customer_id": customer_id,
        "customer": {"id": customer_id, "external_id": tenant_id},
        "product_id": "prod_live",
        "cancel_at_period_end": False,
    }


class BillingReconciliationTests(unittest.TestCase):
    tenant_id = "ten_" + "a" * 32

    def test_unions_external_and_persisted_customer_results_without_duplicates(self):
        from agent.billing.lifecycle import reconcile_workspace_billing

        first = _subscription("sub_one", self.tenant_id)
        second = _subscription("sub_two", self.tenant_id)
        client = _LifecycleClient(
            external_subscriptions=[first], customer_subscriptions=[first, second])
        store = _LifecycleStore({"polar_customer_id": "cus_live"})

        result = reconcile_workspace_billing(
            principal=object(), authorizer=_OwnerAuthorizer(), store=store,
            client=client, clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))

        self.assertEqual(result["subscription_count"], 2)
        self.assertTrue(result["multiple_subscriptions"])
        self.assertEqual(result["state"], "reconciled")
        self.assertEqual(len(store.saved[0]["subscriptions"]), 2)
        self.assertEqual(store.finished[0]["state"], "reconciled")
        self.assertEqual(client.calls, [
            ("subscriptions", {"external_customer_id": self.tenant_id}),
            ("checkouts", {"external_customer_id": self.tenant_id}),
            ("subscriptions", {"customer_id": "cus_live"}),
            ("checkouts", {"customer_id": "cus_live"}),
        ])

    def test_cross_tenant_provider_identity_fails_closed_and_is_durable(self):
        from agent.billing.lifecycle import (
            BillingLifecycleError, reconcile_workspace_billing,
        )

        client = _LifecycleClient(external_subscriptions=[
            _subscription("sub_other", "ten_" + "b" * 32)])
        store = _LifecycleStore()

        with self.assertRaisesRegex(BillingLifecycleError, "identity"):
            reconcile_workspace_billing(
                principal=object(), authorizer=_OwnerAuthorizer(), store=store,
                client=client)

        self.assertEqual(store.finished[0]["state"], "ambiguous")
        self.assertEqual(store.finished[0]["failure_category"], "identity_conflict")
        self.assertEqual(store.saved, [])

    def test_owner_authorization_happens_before_provider_or_store_work(self):
        from agent.billing.lifecycle import reconcile_workspace_billing

        client = _LifecycleClient()
        store = _LifecycleStore()
        denied = RuntimeError("not owner")

        with self.assertRaisesRegex(RuntimeError, "not owner"):
            reconcile_workspace_billing(
                principal=object(), authorizer=_OwnerAuthorizer(error=denied),
                store=store, client=client)

        self.assertEqual(client.calls, [])
        self.assertEqual(store.started, [])

    def test_reconciliation_records_unknown_and_actionable_states_but_no_entitlement(self):
        from agent.billing.lifecycle import (
            BillingLifecycleError, reconcile_workspace_billing,
        )

        client = _LifecycleClient(
            external_subscriptions=[_subscription(
                "sub_unknown", self.tenant_id, status="future")],
            external_checkouts=[{
                "id": "checkout_open", "status": "open",
                "customer": {"id": "cus_live", "external_id": self.tenant_id},
                "expires_at": "2026-09-09T00:00:00Z", "metadata": {},
            }])
        store = _LifecycleStore()

        with self.assertRaises(BillingLifecycleError):
            reconcile_workspace_billing(
                principal=object(), authorizer=_OwnerAuthorizer(), store=store,
                client=client)

        self.assertEqual(store.finished[-1]["state"], "ambiguous")
        self.assertEqual(store.finished[-1]["failure_category"],
                         "unknown_subscription_state")
        self.assertFalse(hasattr(store, "upsert_billing_from_subscription"))

    def test_malformed_checkout_expiry_is_durably_ambiguous(self):
        from agent.billing.lifecycle import (
            BillingLifecycleError, reconcile_workspace_billing,
        )
        client = _LifecycleClient(external_checkouts=[{
            "id": "checkout_bad_time", "status": "open",
            "customer": {"id": "cus_live", "external_id": self.tenant_id},
            "expires_at": "not-a-time", "metadata": {},
        }])
        store = _LifecycleStore()

        with self.assertRaises(BillingLifecycleError):
            reconcile_workspace_billing(
                principal=object(), authorizer=_OwnerAuthorizer(), store=store,
                client=client)

        self.assertEqual(store.finished[-1]["failure_category"],
                         "malformed_provider_state")


class _RevocationClient(_LifecycleClient):
    def __init__(self, subscription_pages, revoke_results=None, checkouts=()):
        super().__init__(external_checkouts=checkouts)
        self.subscription_pages = list(subscription_pages)
        self.revoke_results = dict(revoke_results or {})
        self.revoked = []

    def list_subscriptions(self, **identity):
        self.calls.append(("subscriptions", identity))
        if "customer_id" in identity:
            return []
        return self.subscription_pages.pop(0)

    def revoke_subscription(self, subscription_id):
        self.revoked.append(subscription_id)
        outcome = self.revoke_results.get(subscription_id, {
            "id": subscription_id, "status": "canceled"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BillingRevocationTests(unittest.TestCase):
    tenant_id = "ten_" + "a" * 32

    def test_revoke_blocks_checkout_revokes_all_live_and_proves_terminal(self):
        from agent.billing.lifecycle import revoke_workspace_subscriptions

        active = [_subscription("sub_one", self.tenant_id),
                  _subscription("sub_two", self.tenant_id, status="past_due")]
        terminal = [_subscription("sub_one", self.tenant_id, status="canceled"),
                    _subscription("sub_two", self.tenant_id, status="canceled")]
        client = _RevocationClient([active, terminal])
        store = _LifecycleStore()

        result = revoke_workspace_subscriptions(
            principal=object(), authorizer=_OwnerAuthorizer(), store=store,
            client=client)

        self.assertEqual(result["state"], "verified_safe")
        self.assertEqual(client.revoked, ["sub_one", "sub_two"])
        self.assertEqual(store.started, [(self.tenant_id, "revoke", True)])
        self.assertEqual(store.finished[-1]["state"], "verified_safe")

    def test_delete_404_counts_only_after_complete_followup_proves_absence(self):
        from agent.billing.lifecycle import revoke_workspace_subscriptions

        client = _RevocationClient(
            [[_subscription("sub_gone", self.tenant_id)], []],
            {"sub_gone": PolarAPIError(
                "absent", status_code=404, operation="revoke_subscription")})

        result = revoke_workspace_subscriptions(
            principal=object(), authorizer=_OwnerAuthorizer(),
            store=_LifecycleStore(), client=client)

        self.assertEqual(result["state"], "verified_safe")

    def test_one_provider_failure_does_not_skip_other_subscriptions_or_claim_safety(self):
        from agent.billing.lifecycle import (
            BillingLifecycleError, revoke_workspace_subscriptions,
        )

        active = [_subscription("sub_bad", self.tenant_id),
                  _subscription("sub_good", self.tenant_id)]
        client = _RevocationClient(
            [active], {"sub_bad": PolarAPIError(
                "timeout", operation="revoke_subscription")})
        store = _LifecycleStore()

        with self.assertRaises(BillingLifecycleError):
            revoke_workspace_subscriptions(
                principal=object(), authorizer=_OwnerAuthorizer(),
                store=store, client=client)

        self.assertEqual(client.revoked, ["sub_bad", "sub_good"])
        self.assertEqual(store.finished[-1]["state"], "failed")
        self.assertEqual(
            {row["polar_subscription_id"]: row["outcome"]
             for row in store.revocation_results},
            {"sub_bad": "provider_failure", "sub_good": "provider_accepted"})

    def test_actionable_checkout_prevents_verified_safe(self):
        from agent.billing.lifecycle import (
            BillingLifecycleError, revoke_workspace_subscriptions,
        )

        checkout = {
            "id": "checkout_open", "status": "open",
            "customer": {"id": "cus_live", "external_id": self.tenant_id},
            "metadata": {},
        }
        store = _LifecycleStore()
        client = _RevocationClient([[], []], checkouts=[checkout])

        with self.assertRaises(BillingLifecycleError):
            revoke_workspace_subscriptions(
                principal=object(), authorizer=_OwnerAuthorizer(),
                store=store, client=client)

        self.assertEqual(store.finished[-1]["state"], "ambiguous")
        self.assertEqual(store.finished[-1]["failure_category"],
                         "actionable_checkout")

    def test_inflight_checkout_provider_call_prevents_verified_safe(self):
        from agent.billing.lifecycle import (
            BillingLifecycleError, revoke_workspace_subscriptions,
        )
        store = _LifecycleStore()
        store.checkout_create_in_flight = True

        with self.assertRaises(BillingLifecycleError):
            revoke_workspace_subscriptions(
                principal=object(), authorizer=_OwnerAuthorizer(), store=store,
                client=_RevocationClient([[], []]))

        self.assertEqual(store.finished[-1]["state"], "ambiguous")
        self.assertEqual(store.finished[-1]["failure_category"],
                         "checkout_create_in_flight")


@unittest.skipUnless(os.environ.get("RELIUM_TEST_POSTGRES_DSN"),
                     "PostgreSQL lifecycle tests require RELIUM_TEST_POSTGRES_DSN")
class BillingLifecyclePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.dsn = os.environ["RELIUM_TEST_POSTGRES_DSN"]
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        self.store = PostgresLifecycleStore(self.dsn)
        self.first = self.store.upsert_tenant_for_clerk_organization(
            "org_bill_a", organization_name="Billing A")
        self.second = self.store.upsert_tenant_for_clerk_organization(
            "org_bill_b", organization_name="Billing B")

    def tearDown(self):
        self.store.close()

    def test_new_tenant_operation_lazily_creates_active_control_and_preserves_entitlement(self):
        tenant_id = self.first["tenant_id"]
        self.store.upsert_billing_from_subscription(
            tenant_id=tenant_id, polar_customer_id="cus_a",
            polar_subscription_id="sub_entitlement", polar_product_id="prod_a",
            plan="starter", subscription_status="active",
            current_period_end=None, cancel_at_period_end=False,
            past_due_at=None, subscription_modified_at=None)

        operation = self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="reconcile")
        self.store.save_billing_reconciliation(
            tenant_id=tenant_id, operation_id=operation["operation_id"],
            subscriptions=({
                "polar_subscription_id": "sub_seen",
                "polar_customer_id": "cus_a", "polar_product_id": "prod_a",
                "provider_status": "canceled", "cancel_at_period_end": False,
                "classification": "terminal",
            },), checkouts=(), observed_at=datetime.now(timezone.utc))

        entitlement = self.store.billing_for_tenant(tenant_id)
        self.assertEqual(entitlement["polar_subscription_id"], "sub_entitlement")
        self.assertEqual(entitlement["plan"], "starter")

    def test_provider_subscription_cannot_be_observed_for_two_tenants(self):
        now = datetime.now(timezone.utc)
        first_op = self.store.begin_billing_lifecycle_operation(
            tenant_id=self.first["tenant_id"], operation_kind="reconcile")
        second_op = self.store.begin_billing_lifecycle_operation(
            tenant_id=self.second["tenant_id"], operation_kind="reconcile")
        item = ({
            "polar_subscription_id": "sub_shared", "polar_customer_id": "cus_a",
            "polar_product_id": "prod_a", "provider_status": "active",
            "cancel_at_period_end": False,
            "classification": "potentially_billable",
        },)
        self.store.save_billing_reconciliation(
            tenant_id=self.first["tenant_id"],
            operation_id=first_op["operation_id"], subscriptions=item,
            checkouts=(), observed_at=now)

        with self.assertRaises(Exception):
            self.store.save_billing_reconciliation(
                tenant_id=self.second["tenant_id"],
                operation_id=second_op["operation_id"], subscriptions=item,
                checkouts=(), observed_at=now)

    def test_superseded_operation_cannot_save_or_finish(self):
        tenant_id = self.first["tenant_id"]
        old = self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="reconcile")
        self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="reconcile")

        with self.assertRaises(Exception):
            self.store.save_billing_reconciliation(
                tenant_id=tenant_id, operation_id=old["operation_id"],
                subscriptions=(), checkouts=(),
                observed_at=datetime.now(timezone.utc))
        with self.assertRaises(Exception):
            self.store.finish_billing_lifecycle_operation(
                tenant_id=tenant_id, operation_id=old["operation_id"],
                state="reconciled", failure_category=None,
                subscription_count=0, actionable_checkout_count=0)

    def test_checkout_provider_create_is_exclusive_and_revocation_sees_inflight(self):
        tenant_id = self.first["tenant_id"]
        intent = self.store.claim_billing_checkout_intent(
            tenant_id=tenant_id, requested_plan="starter",
            polar_product_id="prod_starter", now=datetime.now(timezone.utc))

        first = self.store.begin_billing_checkout_provider_create(
            tenant_id=tenant_id,
            checkout_intent_id=intent["checkout_intent_id"],
            now=datetime.now(timezone.utc))
        second = self.store.begin_billing_checkout_provider_create(
            tenant_id=tenant_id,
            checkout_intent_id=intent["checkout_intent_id"],
            now=datetime.now(timezone.utc))
        self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="revoke", block_checkout=True)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertTrue(self.store.billing_checkout_create_in_flight(tenant_id))

    def test_revocation_generation_fences_a_claimed_checkout_before_provider_call(self):
        tenant_id = self.first["tenant_id"]
        intent = self.store.claim_billing_checkout_intent(
            tenant_id=tenant_id, requested_plan="starter",
            polar_product_id="prod_starter", now=datetime.now(timezone.utc))
        self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="revoke", block_checkout=True)

        claim = self.store.begin_billing_checkout_provider_create(
            tenant_id=tenant_id,
            checkout_intent_id=intent["checkout_intent_id"],
            now=datetime.now(timezone.utc))

        self.assertIsNone(claim)

    def test_normal_reconciliation_does_not_invalidate_checkout_generation(self):
        tenant_id = self.first["tenant_id"]
        intent = self.store.claim_billing_checkout_intent(
            tenant_id=tenant_id, requested_plan="starter",
            polar_product_id="prod_starter", now=datetime.now(timezone.utc))
        self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="reconcile", block_checkout=False)

        claim = self.store.begin_billing_checkout_provider_create(
            tenant_id=tenant_id,
            checkout_intent_id=intent["checkout_intent_id"],
            now=datetime.now(timezone.utc))

        self.assertIsNotNone(claim)

    def test_provider_completion_after_revocation_fence_is_rejected(self):
        tenant_id = self.first["tenant_id"]
        now = datetime.now(timezone.utc)
        intent = self.store.claim_billing_checkout_intent(
            tenant_id=tenant_id, requested_plan="starter",
            polar_product_id="prod_starter", now=now)
        lease = self.store.begin_billing_checkout_provider_create(
            tenant_id=tenant_id,
            checkout_intent_id=intent["checkout_intent_id"], now=now)
        self.store.begin_billing_lifecycle_operation(
            tenant_id=tenant_id, operation_kind="revoke", block_checkout=True)

        with self.assertRaises(Exception):
            self.store.finish_billing_checkout_intent(
                tenant_id=tenant_id,
                checkout_intent_id=intent["checkout_intent_id"],
                state="provider_created", polar_checkout_id="checkout_late",
                failure_category=None, now=now,
                create_lease_id=lease["create_lease_id"])

    def test_completed_checkout_intent_is_idempotent_for_later_signed_webhooks(self):
        from agent.billing.service import BillingService

        tenant_id = self.first["tenant_id"]
        now = datetime.now(timezone.utc)
        intent = self.store.claim_billing_checkout_intent(
            tenant_id=tenant_id, requested_plan="starter",
            polar_product_id="prod_starter", now=now)
        self.store.finish_billing_checkout_intent(
            tenant_id=tenant_id,
            checkout_intent_id=intent["checkout_intent_id"], state="completed",
            polar_checkout_id=None, failure_category=None, now=now)
        settings = SimpleNamespace(
            starter_product_id="prod_starter", pro_product_id="prod_pro",
            past_due_grace=None)
        service = BillingService(settings, client=object(), clock=lambda: now)
        payload = _subscription("sub_webhook", tenant_id, status="active")
        payload["product_id"] = "prod_starter"
        payload["metadata"] = {
            "relium_tenant_id": tenant_id,
            "relium_checkout_intent_id": intent["checkout_intent_id"],
        }

        self.assertEqual(service.apply_subscription_event(
            self.store, "subscription.created", payload), "applied")
        payload["status"] = "canceled"
        self.assertEqual(service.apply_subscription_event(
            self.store, "subscription.canceled", payload), "applied")
        self.assertEqual(
            self.store.billing_for_tenant(tenant_id)["subscription_status"],
            "canceled")


class _CheckoutIntentStore(_LifecycleStore):
    def __init__(self, billing=None, *, blocked=False):
        super().__init__(billing)
        self.blocked = blocked
        self.intent = None
        self.intent_finishes = []
        self.create_claims = []

    def claim_billing_checkout_intent(self, *, tenant_id, requested_plan,
                                      polar_product_id, now):
        if self.blocked:
            from agent.billing.lifecycle import BillingLifecycleError
            raise BillingLifecycleError("checkout_blocked")
        if self.intent is None:
            self.intent = {
                "checkout_intent_id": "bci_fixed", "requested_plan": requested_plan,
                "polar_product_id": polar_product_id, "state": "claimed",
                "polar_checkout_id": None, "created": True,
                "lifecycle_generation": 0,
            }
        else:
            self.intent = dict(self.intent, created=False)
        return self.intent

    def begin_billing_checkout_provider_create(self, *, tenant_id,
                                               checkout_intent_id, now):
        self.create_claims.append((tenant_id, checkout_intent_id))
        return {"create_lease_id": "bcl_fixed"}

    def finish_billing_checkout_intent(self, **values):
        self.intent_finishes.append(values)
        if self.intent:
            self.intent = dict(self.intent, state=values["state"],
                               polar_checkout_id=values.get("polar_checkout_id"))


class _CheckoutClient(_LifecycleClient):
    def __init__(self, *, subscriptions=(), checkout_rounds=(), create_result=None):
        super().__init__(external_subscriptions=subscriptions)
        self.checkout_rounds = list(checkout_rounds)
        self.create_result = create_result or {
            "id": "checkout_new", "url": "https://polar.sh/checkout/new"}
        self.created_payloads = []

    def list_checkouts(self, **identity):
        self.calls.append(("checkouts", identity))
        if "customer_id" in identity:
            return []
        return self.checkout_rounds.pop(0) if self.checkout_rounds else []

    def create_checkout_session(self, **payload):
        self.created_payloads.append(payload)
        if isinstance(self.create_result, Exception):
            raise self.create_result
        return self.create_result


class BillingCheckoutIntentTests(unittest.TestCase):
    tenant_id = "ten_" + "a" * 32

    @staticmethod
    def _service(client):
        from agent.billing.service import BillingService

        settings = SimpleNamespace(
            starter_product_id="prod_starter", pro_product_id="prod_pro",
            webhook_secret="unused", past_due_grace=None)
        return BillingService(settings, client, app_url="https://app.relium.ai",
                              clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))

    def test_provider_subscription_blocks_checkout_even_without_local_entitlement(self):
        from agent.billing.service import BillingError

        client = _CheckoutClient(subscriptions=[
            _subscription("sub_live", self.tenant_id)])
        store = _CheckoutIntentStore()

        with self.assertRaisesRegex(BillingError, "subscription_exists"):
            self._service(client).create_checkout(store, self.tenant_id, "starter")

        self.assertEqual(client.created_payloads, [])

    def test_intent_id_is_provider_metadata_and_success_is_persisted(self):
        client = _CheckoutClient()
        store = _CheckoutIntentStore()

        result = self._service(client).create_checkout(
            store, self.tenant_id, "starter")

        self.assertEqual(result["checkout_id"], "checkout_new")
        self.assertEqual(client.created_payloads[0]["metadata"]
                         ["relium_checkout_intent_id"], "bci_fixed")
        self.assertEqual(store.intent_finishes[-1]["state"], "provider_created")

    def test_retry_recovers_one_matching_provider_checkout_without_new_create(self):
        matching = {
            "id": "checkout_recovered", "status": "open",
            "url": "https://polar.sh/checkout/recovered",
            "customer": {"id": "cus_live", "external_id": self.tenant_id},
            "metadata": {"relium_checkout_intent_id": "bci_fixed"},
        }
        client = _CheckoutClient(checkout_rounds=[[matching]])
        store = _CheckoutIntentStore()
        store.intent = {
            "checkout_intent_id": "bci_fixed", "requested_plan": "starter",
            "polar_product_id": "prod_starter", "state": "claimed",
            "polar_checkout_id": None, "created": False,
            "lifecycle_generation": 0,
        }

        result = self._service(client).create_checkout(
            store, self.tenant_id, "starter")

        self.assertEqual(result["checkout_id"], "checkout_recovered")
        self.assertEqual(client.created_payloads, [])

    def test_multiple_matching_checkouts_fail_closed(self):
        from agent.billing.service import BillingError

        match = lambda identifier: {
            "id": identifier, "status": "open", "url": "https://polar.sh/x",
            "customer": {"id": "cus_live", "external_id": self.tenant_id},
            "metadata": {"relium_checkout_intent_id": "bci_fixed"},
        }
        client = _CheckoutClient(checkout_rounds=[[
            match("checkout_one"), match("checkout_two")]])
        store = _CheckoutIntentStore()
        store.intent = {
            "checkout_intent_id": "bci_fixed", "requested_plan": "starter",
            "polar_product_id": "prod_starter", "state": "claimed",
            "polar_checkout_id": None, "created": False,
            "lifecycle_generation": 0,
        }

        with self.assertRaisesRegex(BillingError, "billing_provider_unavailable"):
            self._service(client).create_checkout(store, self.tenant_id, "starter")

        self.assertEqual(store.intent_finishes[-1]["state"], "ambiguous")

    def test_known_provider_checkout_missing_from_reconciliation_never_creates_second(self):
        from agent.billing.service import BillingError

        client = _CheckoutClient(checkout_rounds=[[]])
        store = _CheckoutIntentStore()
        store.intent = {
            "checkout_intent_id": "bci_fixed", "requested_plan": "starter",
            "polar_product_id": "prod_starter", "state": "provider_created",
            "polar_checkout_id": "checkout_known", "created": False,
            "lifecycle_generation": 0,
        }

        with self.assertRaisesRegex(BillingError, "billing_provider_unavailable"):
            self._service(client).create_checkout(store, self.tenant_id, "starter")

        self.assertEqual(client.created_payloads, [])

    def test_lifecycle_block_refuses_before_provider_call(self):
        from agent.billing.service import BillingError

        client = _CheckoutClient()
        store = _CheckoutIntentStore(blocked=True)

        with self.assertRaisesRegex(BillingError, "billing_lifecycle_blocked"):
            self._service(client).create_checkout(store, self.tenant_id, "starter")

        self.assertEqual(client.calls, [])

    def test_non_claimant_never_calls_provider_create(self):
        from agent.billing.service import BillingError

        client = _CheckoutClient()
        store = _CheckoutIntentStore()
        store.begin_billing_checkout_provider_create = lambda **_values: None

        with self.assertRaisesRegex(BillingError, "billing_provider_unavailable"):
            self._service(client).create_checkout(store, self.tenant_id, "starter")

        self.assertEqual(client.created_payloads, [])


if __name__ == "__main__":
    unittest.main()
