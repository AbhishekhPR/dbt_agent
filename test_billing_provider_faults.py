"""Diagnosis of Polar provider faults in the workspace-deletion billing phase.

``provider_timeout`` used to be the category for EVERY Polar error that carried
no HTTP status. In production that made a workspace deletion report a network
timeout for a fault that need not have involved the network at all, and nothing
recorded WHICH of the provider calls in the billing phase had failed.

Workspace deletion is the most pagination-heavy path in the system -- it lists
checkouts and subscriptions by two identities, twice, around a mutation -- so a
listing that cannot prove itself complete is exactly the fault most likely to
appear there, and it is not a timeout.

Every case in this file must still FAIL CLOSED. None of them may let the
billing gate conclude that billing has stopped.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

from agent.billing.client import PolarAPIError, PolarClient
from agent.billing.lifecycle import (
    BillingLifecycleError, _provider_failure_category,
    revoke_workspace_subscriptions,
)


TENANT_ID = "ten_" + "a" * 32
TOKEN = "polar_secret_value"


def _page(items, total_count, max_page):
    return (200, json.dumps({
        "items": items,
        "pagination": {"total_count": total_count, "max_page": max_page},
    }).encode())


def _subscription(identifier, status="active", customer_id="cus_live"):
    return {
        "id": identifier,
        "status": status,
        "customer_id": customer_id,
        "customer": {"id": customer_id, "external_id": TENANT_ID},
        "product_id": "prod_live",
        "cancel_at_period_end": False,
    }


class _RecordingTransport:
    """Answers, stalls or fails, and remembers how it was called."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def __call__(self, **request):
        self.calls.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


class ProviderFaultClassificationTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token=TOKEN)

    def _fault(self, outcomes):
        client = PolarClient(self.settings,
                             transport=_RecordingTransport(outcomes), timeout=7.5)
        with self.assertRaises(PolarAPIError) as raised:
            client.list_subscriptions(external_customer_id=TENANT_ID)
        return _provider_failure_category(raised.exception), raised.exception

    def test_socket_deadline_is_reported_as_a_timeout(self):
        stalls = [
            socket.timeout("timed out"),
            urllib.error.URLError(socket.timeout("timed out")),
            TimeoutError("timed out"),
        ]
        for stall in stalls:
            with self.subTest(stall=type(stall).__name__):
                category, error = self._fault([stall])
                self.assertEqual(category, "provider_timeout")
                self.assertEqual(error.failure_kind, "timeout")

    def test_an_unreachable_host_is_not_reported_as_a_timeout(self):
        failures = [
            urllib.error.URLError(socket.gaierror(-2, "no such host")),
            ConnectionRefusedError(111, "connection refused"),
            urllib.error.URLError("certificate verify failed"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                category, error = self._fault([failure])
                self.assertEqual(category, "provider_unreachable")
                self.assertEqual(error.failure_kind, "unreachable")

    def test_an_unprovable_listing_is_not_reported_as_a_timeout(self):
        # Polar ANSWERED in every one of these. The listing simply could not be
        # proved complete, which is a consistency fault, not a network one.
        listings = {
            "unstable": [_page([{"id": "s1"}], 2, 2), _page([{"id": "s2"}], 3, 2)],
            "duplicated": [_page([{"id": "s1"}], 2, 2), _page([{"id": "s1"}], 2, 2)],
            "short": [_page([{"id": "s1"}], 2, 1)],
            "malformed": [(200, json.dumps(
                {"items": [], "pagination": {"max_page": 2}}).encode())],
        }
        for name, outcomes in listings.items():
            with self.subTest(listing=name):
                category, error = self._fault(outcomes)
                self.assertEqual(category, "provider_inconsistent")
                self.assertEqual(error.failure_kind, "inconsistent")

    def test_every_statusless_fault_stays_retryable(self):
        # Retryable means "nothing was decided, ask again" -- never "proceed".
        for outcome in (socket.timeout("timed out"),
                        ConnectionRefusedError(111, "refused"),
                        _page([{"id": "s1"}], 2, 1)):
            with self.subTest(outcome=type(outcome).__name__):
                _, error = self._fault([outcome])
                self.assertIsNone(error.status_code)
                self.assertTrue(error.retryable)

    def test_a_refusal_carrying_a_status_is_still_classified_by_status(self):
        cases = [(401, "provider_auth"), (403, "provider_auth"),
                 (429, "provider_rate_limit"), (503, "provider_5xx"),
                 (400, "provider_refused")]
        for status, expected in cases:
            with self.subTest(status=status):
                client = PolarClient(
                    self.settings,
                    transport=_RecordingTransport([(status, b'{"error":"nope"}')]))
                with self.assertRaises(PolarAPIError) as raised:
                    client.list_subscriptions(external_customer_id=TENANT_ID)
                self.assertEqual(
                    _provider_failure_category(raised.exception), expected)
                self.assertIsNone(raised.exception.failure_kind)


class ProviderDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token=TOKEN)

    def test_the_configured_deadline_reaches_the_transport_on_every_call(self):
        transport = _RecordingTransport([_page([], 0, 1)])
        client = PolarClient(self.settings, transport=transport, timeout=3.25)

        client.list_subscriptions(external_customer_id=TENANT_ID)

        self.assertEqual([call["timeout"] for call in transport.calls], [3.25])

    def test_the_deadline_is_per_request_not_per_listing(self):
        # A three-page listing gets the full deadline three times. This is worth
        # pinning: the ceiling on one lifecycle advance is the deadline times
        # the number of provider calls, not the deadline.
        transport = _RecordingTransport([
            _page([{"id": "s1"}], 3, 3),
            _page([{"id": "s2"}], 3, 3),
            _page([{"id": "s3"}], 3, 3),
        ])
        client = PolarClient(self.settings, transport=transport, timeout=9.0)

        client.list_subscriptions(external_customer_id=TENANT_ID)

        self.assertEqual([call["timeout"] for call in transport.calls],
                         [9.0, 9.0, 9.0])

    def test_a_delayed_but_answered_call_succeeds_and_records_its_latency(self):
        def slow_page():
            time.sleep(0.02)
            return _page([{"id": "sub_one"}], 1, 1)

        client = PolarClient(self.settings,
                             transport=_RecordingTransport([slow_page]), timeout=30)

        with self.assertLogs("agent.billing.client", level="INFO") as logs:
            items = client.list_subscriptions(external_customer_id=TENANT_ID)

        self.assertEqual([item["id"] for item in items], ["sub_one"])
        self.assertEqual(logs.records[0].outcome, "ok")
        self.assertGreaterEqual(logs.records[0].latency_ms, 10)


class ProviderLoggingTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token=TOKEN)

    def test_a_failed_call_is_logged_with_operation_latency_and_retryability(self):
        client = PolarClient(
            self.settings,
            transport=_RecordingTransport([socket.timeout("timed out")]),
            timeout=7.5)

        with self.assertLogs("agent.billing.client", level="INFO") as logs:
            with self.assertRaises(PolarAPIError):
                client.list_checkouts(external_customer_id=TENANT_ID)

        record = logs.records[0]
        self.assertEqual(record.operation, "list_checkouts")
        self.assertEqual(record.http_method, "GET")
        self.assertEqual(record.route_template, "/v1/checkouts/")
        self.assertEqual(record.outcome, "timeout")
        self.assertIs(record.retryable, True)
        self.assertIsNone(record.http_status)
        self.assertEqual(record.timeout_seconds, 7.5)
        self.assertIsInstance(record.latency_ms, int)

    def test_every_billing_phase_call_names_itself(self):
        # "Which provider call failed?" must be answerable from the log alone.
        calls = {
            "list_subscriptions":
                lambda c: c.list_subscriptions(external_customer_id=TENANT_ID),
            "list_checkouts":
                lambda c: c.list_checkouts(external_customer_id=TENANT_ID),
            "get_subscription": lambda c: c.get_subscription("sub_live"),
            "revoke_subscription": lambda c: c.revoke_subscription("sub_live"),
        }
        for operation, call in calls.items():
            with self.subTest(operation=operation):
                client = PolarClient(
                    self.settings,
                    transport=_RecordingTransport([socket.timeout("timed out")]))
                with self.assertLogs("agent.billing.client", level="INFO") as logs:
                    with self.assertRaises(PolarAPIError):
                        call(client)
                self.assertEqual(logs.records[0].operation, operation)

    def test_an_unprovable_listing_is_logged_as_inconsistent(self):
        client = PolarClient(
            self.settings,
            transport=_RecordingTransport([_page([{"id": "s1"}], 2, 1)]))

        with self.assertLogs("agent.billing.client", level="INFO") as logs:
            with self.assertRaises(PolarAPIError):
                client.list_subscriptions(external_customer_id=TENANT_ID)

        outcomes = [getattr(record, "outcome", None) for record in logs.records]
        self.assertIn("inconsistent", outcomes)

    def test_a_refusal_is_never_logged_as_a_successful_call(self):
        # Found in review: the success record was emitted before the status was
        # checked, so a 402 was logged `outcome: ok` and then raised. That is
        # precisely the log an operator would be reading to find this fault.
        for status in (402, 429, 500):
            with self.subTest(status=status):
                client = PolarClient(
                    self.settings,
                    transport=_RecordingTransport([(status, b'{"error":"nope"}')]))
                with self.assertLogs("agent.billing.client", level="INFO") as logs:
                    with self.assertRaises(PolarAPIError):
                        client.list_subscriptions(external_customer_id=TENANT_ID)
                outcomes = [record.outcome for record in logs.records]
                self.assertNotIn("ok", outcomes)
                self.assertIn("refused", outcomes)
                self.assertIn(status, [r.http_status for r in logs.records])

    def test_an_unreadable_body_is_logged_as_a_refusal_too(self):
        client = PolarClient(self.settings,
                             transport=_RecordingTransport([(200, b"not json")]))

        with self.assertLogs("agent.billing.client", level="INFO") as logs:
            with self.assertRaises(PolarAPIError):
                client.list_subscriptions(external_customer_id=TENANT_ID)

        self.assertEqual([record.outcome for record in logs.records], ["refused"])

    def test_an_id_addressed_call_logs_a_template_not_the_provider_object_id(self):
        # Found in review: the concrete path was logged, which put a customer's
        # Polar subscription id in every record and made the route unaggregatable.
        for call in (lambda c: c.get_subscription("sub_live_secret"),
                     lambda c: c.revoke_subscription("sub_live_secret")):
            with self.subTest(call=call):
                client = PolarClient(
                    self.settings,
                    transport=_RecordingTransport([socket.timeout("timed out")]))
                with self.assertLogs("agent.billing.client", level="INFO") as logs:
                    with self.assertRaises(PolarAPIError):
                        call(client)
                record = logs.records[0]
                self.assertEqual(record.route_template, "/v1/subscriptions/{id}")
                self.assertNotIn("sub_live_secret", str(vars(record)))

    def test_logs_never_carry_the_token_the_url_a_tenant_id_or_a_body(self):
        cases = [
            [socket.timeout("timed out")],
            [(402, b'{"error":"card_declined","secret":"leak"}')],
            [_page([{"id": "sub_one"}], 2, 1)],
        ]
        for index, outcomes in enumerate(cases):
            with self.subTest(case=index):
                client = PolarClient(self.settings,
                                     transport=_RecordingTransport(outcomes))
                with self.assertLogs("agent.billing.client", level="INFO") as logs:
                    with self.assertRaises(PolarAPIError):
                        client.list_subscriptions(external_customer_id=TENANT_ID)
                for record in logs.records:
                    rendered = " ".join(
                        str(value) for value in vars(record).values())
                    self.assertNotIn(TOKEN, rendered)
                    self.assertNotIn("api.polar.sh", rendered)
                    self.assertNotIn(TENANT_ID, rendered)
                    self.assertNotIn("card_declined", rendered)
                    self.assertNotIn("leak", rendered)


# ---------------------------------------------------------------------------
# The billing gate itself, driven through the real client and transport.
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self, billing=None):
        self.billing = billing
        self.started = []
        self.saved = []
        self.finished = []
        self.revocation_results = []

    def billing_for_tenant(self, tenant_id):
        return self.billing

    def begin_billing_lifecycle_operation(self, *, tenant_id, operation_kind,
                                          block_checkout=False, now=None):
        self.started.append((tenant_id, operation_kind, block_checkout))
        return {"operation_id": "blo_test", "generation": len(self.started)}

    def save_billing_reconciliation(self, **values):
        self.saved.append(values)

    def finish_billing_lifecycle_operation(self, **values):
        self.finished.append(values)

    def billing_checkout_create_in_flight(self, tenant_id):
        return False

    def record_billing_subscription_revocation_result(self, **values):
        self.revocation_results.append(values)


class _Owner:
    def require_owner(self, principal):
        return SimpleNamespace(tenant_id=TENANT_ID)


class _CountingClient:
    """Wraps the real classification while counting mutations."""

    def __init__(self, *, listings, revoke_status="canceled"):
        self._listings = list(listings)
        self._revoke_status = revoke_status
        self.revoked = []

    def _next(self):
        outcome = self._listings.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def list_checkouts(self, **identity):
        return []

    def list_subscriptions(self, **identity):
        if "customer_id" in identity:
            return []
        return self._next()

    def revoke_subscription(self, subscription_id):
        self.revoked.append(subscription_id)
        return {"id": subscription_id, "status": self._revoke_status}


def _clock():
    return datetime(2026, 9, 10, tzinfo=timezone.utc)


class BillingGateUnderProviderFaultTests(unittest.TestCase):
    def _revoke(self, client, store):
        return revoke_workspace_subscriptions(
            principal=object(), authorizer=_Owner(), store=store,
            client=client, clock=_clock)

    def test_a_timeout_while_listing_fails_closed_before_any_mutation(self):
        client = _CountingClient(
            listings=[PolarAPIError("Polar could not be reached.",
                                    operation="list_subscriptions",
                                    failure_kind="timeout")])
        store = _Store()

        with self.assertRaises(BillingLifecycleError) as raised:
            self._revoke(client, store)

        self.assertEqual(raised.exception.category, "provider_timeout")
        self.assertEqual(client.revoked, [])
        self.assertEqual(store.finished[0]["state"], "failed")
        self.assertEqual(store.finished[0]["failure_category"], "provider_timeout")

    def test_an_unprovable_listing_fails_closed_under_its_own_name(self):
        client = _CountingClient(
            listings=[PolarAPIError("Polar returned incomplete pagination.",
                                    operation="list_subscriptions",
                                    failure_kind="inconsistent")])
        store = _Store()

        with self.assertRaises(BillingLifecycleError) as raised:
            self._revoke(client, store)

        self.assertEqual(raised.exception.category, "provider_inconsistent")
        self.assertEqual(store.finished[0]["failure_category"],
                         "provider_inconsistent")

    def test_a_timeout_after_revocation_fails_closed_without_declaring_safety(self):
        # The dangerous shape: the mutation landed, the PROOF did not. The gate
        # must not conclude billing is stopped.
        client = _CountingClient(listings=[
            [_subscription("sub_live")],
            PolarAPIError("Polar could not be reached.",
                          operation="list_subscriptions", failure_kind="timeout"),
        ])
        store = _Store()

        with self.assertRaises(BillingLifecycleError) as raised:
            self._revoke(client, store)

        self.assertEqual(raised.exception.category, "provider_timeout")
        self.assertEqual(client.revoked, ["sub_live"])
        self.assertNotIn("verified_safe",
                         [entry["state"] for entry in store.finished])

    def test_resuming_after_a_timeout_succeeds_and_never_revokes_twice(self):
        # Attempt one revokes, then loses the provider before it can prove the
        # result. Attempt two observes the terminal subscription and completes.
        first = _CountingClient(listings=[
            [_subscription("sub_live")],
            PolarAPIError("Polar could not be reached.",
                          operation="list_subscriptions", failure_kind="timeout"),
        ])
        store = _Store()
        with self.assertRaises(BillingLifecycleError):
            self._revoke(first, store)
        self.assertEqual(first.revoked, ["sub_live"])

        second = _CountingClient(listings=[
            [_subscription("sub_live", status="canceled")],
            [_subscription("sub_live", status="canceled")],
        ])
        result = self._revoke(second, store)

        self.assertEqual(result["state"], "verified_safe")
        # The resume issued NO further mutation: the subscription was already
        # terminal, so it was skipped rather than revoked a second time.
        self.assertEqual(second.revoked, [])
        self.assertEqual(store.finished[-1]["state"], "verified_safe")

    def test_a_clean_workspace_still_reaches_verified_safe(self):
        client = _CountingClient(listings=[[], []])
        store = _Store()

        result = self._revoke(client, store)

        self.assertEqual(result["state"], "verified_safe")
        self.assertEqual(result["subscription_count"], 0)
        self.assertEqual(client.revoked, [])


class InconsistencySubtypeTests(unittest.TestCase):
    """One log line must say WHICH integrity check fired, and on which page.

    The first cut of this logging recorded only `outcome: inconsistent`, which
    told production that a listing could not be trusted but not which of the
    four checks had rejected it, on which query, or with what counts. That is
    one deploy cycle per question.
    """

    def setUp(self):
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token=TOKEN)

    def _inconsistency(self, outcomes, *, by_customer=False):
        client = PolarClient(self.settings,
                             transport=_RecordingTransport(outcomes))
        identity = ({"customer_id": "cus_live"} if by_customer
                    else {"external_customer_id": TENANT_ID})
        with self.assertLogs("agent.billing.client", level="INFO") as logs:
            with self.assertRaises(PolarAPIError) as raised:
                client.list_subscriptions(**identity)
        record = next(r for r in logs.records
                      if getattr(r, "outcome", None) == "inconsistent")
        return record, raised.exception

    def test_each_integrity_check_reports_its_own_subtype(self):
        cases = {
            "malformed_envelope": [
                (200, json.dumps({"items": [], "pagination": {"max_page": 2}}).encode())],
            "unstable_pagination": [
                _page([{"id": "s1"}], 2, 2), _page([{"id": "s2"}], 3, 2)],
            "duplicate_object": [
                _page([{"id": "s1"}], 2, 2), _page([{"id": "s1"}], 2, 2)],
            "incomplete_pagination": [_page([{"id": "s1"}], 2, 1)],
        }
        for subtype, outcomes in cases.items():
            with self.subTest(subtype=subtype):
                record, error = self._inconsistency(outcomes)
                self.assertEqual(record.inconsistency_subtype, subtype)
                self.assertEqual(error.inconsistency_subtype, subtype)

    def test_the_page_and_counts_that_made_the_check_fire_are_recorded(self):
        # A single-page listing whose total_count disagrees with what arrived.
        record, _ = self._inconsistency([_page([{"id": "s1"}], 7, 1)])

        self.assertEqual(record.inconsistency_subtype, "incomplete_pagination")
        self.assertEqual(record.page, 1)
        self.assertEqual(record.expected_total, 7)
        self.assertEqual(record.expected_max_page, 1)
        self.assertEqual(record.observed_items, 1)

    def test_instability_is_reported_on_the_page_that_disagreed(self):
        record, _ = self._inconsistency(
            [_page([{"id": "s1"}], 2, 2), _page([{"id": "s2"}], 3, 2)])

        self.assertEqual(record.inconsistency_subtype, "unstable_pagination")
        self.assertEqual(record.page, 2)
        self.assertEqual(record.expected_total, 3)
        self.assertEqual(record.observed_items, 1)

    def test_the_query_is_named_by_kind_and_never_by_value(self):
        # "the external listing disagreed with the customer listing" is a whole
        # class of bug, and used to be invisible.
        external, _ = self._inconsistency([_page([{"id": "s1"}], 2, 1)])
        self.assertEqual(external.identity_kind, "external_customer_id")

        by_customer, _ = self._inconsistency(
            [_page([{"id": "s1"}], 2, 1)], by_customer=True)
        self.assertEqual(by_customer.identity_kind, "customer_id")
        self.assertNotIn("cus_live", str(vars(by_customer)))
        self.assertNotIn(TENANT_ID, str(vars(external)))

    def test_the_new_diagnostics_carry_no_identifier_or_secret(self):
        record, _ = self._inconsistency(
            [_page([{"id": "sub_customer_object"}], 9, 1)])

        rendered = " ".join(str(value) for value in vars(record).values())
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn("api.polar.sh", rendered)
        self.assertNotIn(TENANT_ID, rendered)
        self.assertNotIn("sub_customer_object", rendered)

    def test_an_unknown_subtype_is_refused_rather_than_recorded(self):
        error = PolarAPIError("x", failure_kind="inconsistent",
                              inconsistency_subtype="something_invented")
        self.assertIsNone(error.inconsistency_subtype)


# ---------------------------------------------------------------------------
# The production log boundary.
#
# Everything above asserts on LogRecord attributes, which `assertLogs` exposes
# directly. Production does not read LogRecord attributes -- it reads whatever
# SafeJsonFormatter chose to serialise, and that formatter renders an ALLOW-LIST.
# Two rounds of provider instrumentation shipped with every new field silently
# dropped at that boundary, because no test ever ran the formatter.
#
# These tests render real records, produced by the real client, through the real
# formatter, and assert on the JSON that would actually reach Railway.
# ---------------------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class ProductionLogBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token=TOKEN)
        self.capture = _Capture()
        self.logger = logging.getLogger("agent.billing.client")
        self.logger.addHandler(self.capture)
        self.addCleanup(self.logger.removeHandler, self.capture)
        self._previous = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.addCleanup(self.logger.setLevel, self._previous)

    def _rendered(self, outcomes, *, by_customer=False):
        from agent.github_app.server import SafeJsonFormatter

        client = PolarClient(self.settings,
                             transport=_RecordingTransport(outcomes), timeout=7.5)
        identity = ({"customer_id": "cus_live_object"} if by_customer
                    else {"external_customer_id": TENANT_ID})
        try:
            client.list_checkouts(**identity)
        except PolarAPIError:
            pass
        formatter = SafeJsonFormatter()
        return [(record.getMessage(), json.loads(formatter.format(record)))
                for record in self.capture.records]

    def test_the_inconsistency_record_reaches_production_with_its_diagnostics(self):
        # The exact production shape: a single-page listing whose total_count
        # disagrees with what arrived.
        rendered = self._rendered([_page([{"id": "co_1"}], 7, 1)])
        _, payload = next(p for p in rendered
                          if p[0] == "polar_provider_state_inconsistent")

        self.assertEqual(payload["operation"], "list_checkouts")
        self.assertEqual(payload["route_template"], "/v1/checkouts/")
        self.assertEqual(payload["outcome"], "inconsistent")
        self.assertEqual(payload["inconsistency_subtype"], "incomplete_pagination")
        self.assertEqual(payload["identity_kind"], "external_customer_id")
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["expected_total"], 7)
        self.assertEqual(payload["expected_max_page"], 1)
        self.assertEqual(payload["observed_items"], 1)
        self.assertIs(payload["retryable"], True)

    def test_the_provider_call_record_reaches_production_with_latency(self):
        rendered = self._rendered([_page([], 0, 1)])
        _, payload = next(p for p in rendered if p[0] == "polar_provider_call")

        self.assertEqual(payload["operation"], "list_checkouts")
        self.assertEqual(payload["http_method"], "GET")
        self.assertEqual(payload["http_status"], 200)
        self.assertEqual(payload["outcome"], "ok")
        self.assertEqual(payload["timeout_seconds"], 7.5)
        self.assertIn("latency_ms", payload)

    def test_a_failed_call_reaches_production_naming_its_fault(self):
        rendered = self._rendered([socket.timeout("timed out")])
        _, payload = next(p for p in rendered
                          if p[0] == "polar_provider_call_failed")

        self.assertEqual(payload["outcome"], "timeout")
        self.assertIs(payload["retryable"], True)
        self.assertEqual(payload["operation"], "list_checkouts")

    def test_every_field_the_client_emits_survives_the_allow_list(self):
        # The class of bug, not just this instance: a field added to a log call
        # and not to _LOG_FIELDS never reaches production, and used to do so
        # silently. Anything genuinely unsafe to render must be removed from the
        # log call -- not left to be dropped by the formatter.
        from agent.github_app.server import _LOG_FIELDS

        standard = set(vars(logging.LogRecord(
            "n", logging.INFO, "p", 1, "m", (), None)))
        standard.update({"taskName", "message", "asctime"})

        for outcomes in ([_page([{"id": "co_1"}], 7, 1)],
                         [_page([], 0, 1)],
                         [socket.timeout("timed out")],
                         [(402, b'{"error":"card_declined"}')]):
            self.capture.records.clear()
            self._rendered(outcomes)
            for record in self.capture.records:
                emitted = set(vars(record)) - standard
                missing = emitted - set(_LOG_FIELDS)
                self.assertEqual(
                    missing, set(),
                    f"{record.getMessage()} emits {sorted(missing)}, which "
                    f"SafeJsonFormatter would silently drop")

    def test_the_rendered_json_carries_no_identifier_or_secret(self):
        # The allow-list is a redaction boundary. Widening it must not widen
        # what escapes, so this asserts on the serialised bytes.
        for outcomes, by_customer in (([_page([{"id": "co_secret_object"}], 9, 1)], False),
                                      ([_page([{"id": "co_secret_object"}], 9, 1)], True),
                                      ([(402, b'{"error":"card_declined"}')], False)):
            with self.subTest(by_customer=by_customer):
                self.capture.records.clear()
                for _, payload in self._rendered(outcomes, by_customer=by_customer):
                    rendered = json.dumps(payload)
                    self.assertNotIn(TOKEN, rendered)
                    self.assertNotIn(TENANT_ID, rendered)
                    self.assertNotIn("cus_live_object", rendered)
                    self.assertNotIn("co_secret_object", rendered)
                    self.assertNotIn("api.polar.sh", rendered)
                    self.assertNotIn("card_declined", rendered)
                    self.assertNotIn("?", rendered)


if __name__ == "__main__":
    unittest.main()
