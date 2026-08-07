"""Tests for the customer-side Relium Collector.

The collector runs inside a customer's environment with read access to their
warehouse and a token for Relium's API. What it must never do matters as much
as what it does, so the safety boundaries are tested as first-class behaviour,
not as documentation.

Warehouse-backed tests use a real PostgreSQL server when RELIUM_TEST_POSTGRES_DSN
names one, following the convention of the rest of the PostgreSQL suite. The
boundary tests need no server at all.
"""
from __future__ import annotations

import json
import logging
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from agent.collector.client import ReliumApiError, ReliumClient
from agent.collector.config import CollectorConfig
from agent.collector.runner import (
    CollectionError,
    collect_snapshot,
    idempotency_key_for,
    run_collection,
    validate_request,
)
from agent.collector.signals import (
    UnknownSignalError,
    UnsafeIdentifierError,
    classify_signals,
    profile_query,
    split_relation,
)
from agent.collector.warehouse import PostgresMetadataReader, RelationMissing

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")
WAREHOUSE_DSN = os.environ.get("RELIUM_TEST_WAREHOUSE_DSN")

PROFILE_SIGNALS = ["relation_exists", "column_exists", "data_type", "is_nullable",
                   "schema_fingerprint", "row_count", "null_rate", "freshness"]


def _config(**overrides):
    defaults = dict(api_url="https://relium.test", api_token="rlm_abc.secret-value",
                    warehouse_dsn="postgresql://u:p@warehouse/db",
                    environment="production", collector_id="test-collector")
    defaults.update(overrides)
    return CollectorConfig(**defaults)


def _request(**overrides):
    base = {
        "request_id": "req-1",
        "review_id": "gh-review-1",
        "environment": "production",
        "attempt": 1,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "base_manifest_hash": "c" * 64,
        "head_manifest_hash": "d" * 64,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "targets": [{
            "relation_name": "raw.orders",
            "dependency_kind": "external",
            "columns": ["order_id", "discount_amount"],
            "required_signals": list(PROFILE_SIGNALS),
            "criticality": "standard",
            "model_unique_id": "source.a.raw.orders",
        }],
    }
    base.update(overrides)
    return base


class _RecordingReader:
    """Records exactly what was asked of the warehouse."""

    def __init__(self, columns=("order_id", "discount_amount"), row_count=100):
        self.calls = []
        self._columns = columns
        self._row_count = row_count

    def collect_relation(self, *, relation_name, columns, signals,
                         relation_schema=None):
        self.calls.append({"relation": relation_name, "columns": list(columns),
                           "signals": sorted(signals),
                           "schema": relation_schema})
        return {
            "relation_name": relation_name,
            "exists_in_production": True,
            "collection_status": "COLLECTED",
            "row_count": self._row_count,
            "columns": [{"column_name": c, "data_type": "numeric",
                         "null_rate": 0.01, "collection_status": "COLLECTED"}
                        for c in columns],
        }

    @staticmethod
    def missing_relation(relation_name):
        return PostgresMetadataReader.missing_relation(relation_name)


class _FakeClient:
    def __init__(self, submit_status=202, requests=None):
        self.submitted = []
        self.failures = []
        self.acknowledged = []
        self._submit_status = submit_status
        self.registered = False
        self._requests = requests if requests is not None else [_request()]

    def register(self):
        self.registered = True
        return {"collector_id": "test-collector"}

    def pending_requests(self, limit=1):
        return self._requests[:limit]

    def get_request(self, request_id):
        for r in self._requests:
            if r["request_id"] == request_id:
                return r
        return None

    def acknowledge(self, request_id):
        self.acknowledged.append(request_id)
        return {"status": "acknowledged"}

    def report_failure(self, request_id, reason):
        self.failures.append((request_id, reason))
        return {"status": "recorded"}

    def submit_snapshot(self, snapshot, idempotency_key):
        self.submitted.append({"snapshot": snapshot, "key": idempotency_key})
        if self._submit_status == 409:
            return 409, {"status": "rejected", "reason": "payload differs"}
        return self._submit_status, {"status": "accepted",
                                     "snapshot_id": "snap-test-1"}


# ------------------------------------------------------------ targeting

class TargetingTests(unittest.TestCase):
    def test_only_requested_tables_are_queried(self):
        reader = _RecordingReader()
        request = _request(targets=[
            {"relation_name": "raw.orders", "dependency_kind": "external",
             "columns": ["order_id"], "required_signals": PROFILE_SIGNALS},
        ])
        collect_snapshot(request, reader, config=_config())
        self.assertEqual([c["relation"] for c in reader.calls], ["raw.orders"])

    def test_only_requested_columns_are_queried(self):
        reader = _RecordingReader()
        request = _request(targets=[
            {"relation_name": "raw.orders", "dependency_kind": "external",
             "columns": ["discount_amount"], "required_signals": PROFILE_SIGNALS},
        ])
        collect_snapshot(request, reader, config=_config())
        self.assertEqual(reader.calls[0]["columns"], ["discount_amount"])

    def test_only_requested_signals_are_computed(self):
        reader = _RecordingReader()
        request = _request(targets=[
            {"relation_name": "raw.orders", "dependency_kind": "external",
             "columns": ["order_id"],
             "required_signals": ["relation_exists", "row_count"]},
        ])
        collect_snapshot(request, reader, config=_config())
        self.assertEqual(reader.calls[0]["signals"], ["relation_exists", "row_count"])

    def test_head_derived_targets_are_never_queried(self):
        """A relation produced by a model in the pull request is not expected
        in production. Querying for it would manufacture a false finding."""
        reader = _RecordingReader()
        request = _request(targets=[
            {"relation_name": "raw.orders", "dependency_kind": "external",
             "columns": ["order_id"], "required_signals": PROFILE_SIGNALS},
            {"relation_name": "analytics.stg_new", "dependency_kind": "head_derived",
             "columns": ["order_id"], "required_signals": PROFILE_SIGNALS},
        ])
        collect_snapshot(request, reader, config=_config())
        self.assertEqual([c["relation"] for c in reader.calls], ["raw.orders"])

    def test_generated_sql_touches_only_the_named_relation(self):
        sql, _ = profile_query("raw", "orders", ["order_id"], {"row_count", "null_rate"})
        self.assertIn('FROM "raw"."orders"', sql)
        self.assertNotIn("*,", sql.replace("count(*)", ""))
        self.assertNotIn("JOIN", sql.upper())
        self.assertNotIn("UNION", sql.upper())


# ------------------------------------------------------- signal vocabulary

class SignalVocabularyTests(unittest.TestCase):
    def test_unknown_signal_type_fails_closed(self):
        with self.assertRaises(UnknownSignalError):
            classify_signals(["row_count", "exfiltrate_everything"])

    def test_unknown_signal_in_a_target_rejects_the_whole_request(self):
        request = _request(targets=[
            {"relation_name": "raw.orders", "dependency_kind": "external",
             "columns": ["order_id"], "required_signals": ["not_a_signal"]},
        ])
        with self.assertRaises(UnknownSignalError):
            validate_request(request)

    def test_known_but_unimplemented_signal_is_reported_not_silently_passed(self):
        supported, unimplemented = classify_signals(["row_count", "min_max"])
        self.assertEqual(supported, ["row_count"])
        self.assertEqual(unimplemented, ["min_max"])

    def test_unimplemented_signal_downgrades_completeness_to_partial(self):
        reader = _RecordingReader()
        request = _request(targets=[
            {"relation_name": "raw.orders", "dependency_kind": "external",
             "columns": ["order_id"], "required_signals": ["row_count", "min_max"]},
        ])
        snapshot, summary = collect_snapshot(request, reader, config=_config())
        self.assertEqual(snapshot["completeness"], "PARTIAL")
        self.assertEqual(summary["signals_unsupported"], ["min_max"])

    def test_unsafe_identifiers_are_refused(self):
        for bad in ("orders; drop table orders", 'orders" --', "a.b.c", ""):
            with self.subTest(name=bad):
                with self.assertRaises(UnsafeIdentifierError):
                    split_relation(bad)

    def test_bare_relation_defaults_to_public_schema(self):
        self.assertEqual(split_relation("orders"), ("public", "orders"))


# ---------------------------------------------------------- payload safety

class PayloadSafetyTests(unittest.TestCase):
    def test_raw_rows_are_never_included_in_the_submitted_payload(self):
        client = _FakeClient()
        run_collection(_config(), client=client, reader=_RecordingReader())
        payload = json.dumps(client.submitted[0]["snapshot"]).lower()
        for forbidden in ('"rows"', '"sample_rows"', '"records"', '"data"',
                          '"raw"', '"sql"', '"query"', '"statement"'):
            with self.subTest(key=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_submitted_payload_carries_no_credentials(self):
        client = _FakeClient()
        config = _config()
        run_collection(config, client=client, reader=_RecordingReader())
        payload = json.dumps(client.submitted[0]["snapshot"])
        self.assertNotIn(config.api_token, payload)
        self.assertNotIn(config.warehouse_dsn, payload)

    def test_config_repr_never_reveals_secrets(self):
        config = _config()
        for rendering in (repr(config), str(config), f"{config}"):
            self.assertNotIn("secret-value", rendering)
            self.assertNotIn("postgresql://u:p@warehouse/db", rendering)
            self.assertIn("<redacted>", rendering)

    def test_reader_repr_never_reveals_the_dsn(self):
        reader = PostgresMetadataReader("postgresql://user:hunter2@host/db")
        self.assertNotIn("hunter2", repr(reader))
        self.assertNotIn("hunter2", str(reader))

    def test_credentials_never_appear_in_logs(self):
        config = _config()
        client = _FakeClient()
        with self.assertLogs("relium.collector", level="DEBUG") as captured:
            run_collection(config, client=client, reader=_RecordingReader())
        blob = "\n".join(captured.output)
        self.assertNotIn(config.api_token, blob)
        self.assertNotIn("secret-value", blob)
        self.assertNotIn(config.warehouse_dsn, blob)
        self.assertNotIn("hunter2", blob)

    def test_authorization_header_is_never_returned_by_the_client(self):
        seen = {}

        def transport(method, url, body, headers):
            seen.update(headers)
            return 200, {"requests": []}

        client = ReliumClient(_config(), transport=transport)
        client.pending_requests()
        self.assertTrue(seen["Authorization"].startswith("Bearer "))
        # The header exists on the wire but is not surfaced anywhere else.
        self.assertNotIn("Authorization", json.dumps(client.pending_requests()))


# ------------------------------------------------------- request validation

class RequestValidationTests(unittest.TestCase):
    def test_malformed_requests_are_rejected(self):
        cases = {
            "not an object": "nope",
            "missing review_id": _request(review_id=None),
            "missing request_id": _request(request_id=None),
            "no targets": _request(targets=[]),
            "target without relation": _request(targets=[{"columns": []}]),
            "no expiry": _request(expires_at=None),
        }
        for label, request in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(CollectionError):
                    validate_request(request)

    def test_expired_requests_are_rejected_before_any_query(self):
        expired = _request(expires_at=(
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        reader = _RecordingReader()
        client = _FakeClient(requests=[expired])
        outcome = run_collection(_config(), client=client, reader=reader)
        self.assertFalse(outcome.ok)
        self.assertIn("expired", outcome.reason)
        self.assertEqual(reader.calls, [], "the warehouse must not be touched")

    def test_remotely_supplied_sql_is_refused(self):
        """Relium never sends SQL. If it appears, treat the control plane as
        untrusted rather than executing it."""
        for field in ("sql", "query", "statement", "command"):
            with self.subTest(field=field):
                with self.assertRaises(CollectionError):
                    validate_request(_request(**{field: "DROP TABLE orders"}))
                target = {"relation_name": "raw.orders", "dependency_kind": "external",
                          "columns": [], "required_signals": ["row_count"],
                          field: "DROP TABLE orders"}
                with self.assertRaises(CollectionError):
                    validate_request(_request(targets=[target]))


# --------------------------------------------------------- identity + replay

class IdentityAndReplayTests(unittest.TestCase):
    def test_collector_preserves_review_and_attempt_identity(self):
        client = _FakeClient()
        request = _request(attempt=3)
        run_collection(_config(), client=client, reader=_RecordingReader(),
                       request_id=request["request_id"])
        snapshot = client.submitted[0]["snapshot"]
        self.assertEqual(snapshot["review_id"], "gh-review-1")
        self.assertEqual(snapshot["request_id"], "req-1")
        self.assertEqual(snapshot["attempt"], 1)  # from the default request
        for field in ("base_sha", "head_sha", "base_manifest_hash",
                      "head_manifest_hash"):
            with self.subTest(field=field):
                self.assertEqual(snapshot[field], _request()[field])

    def test_idempotency_key_is_stable_for_identical_evidence(self):
        request = _request(attempt=2)
        snapshot = {"review_id": "gh-review-1", "environment": "production",
                    "relations": [{"relation_name": "raw.orders", "row_count": 100}],
                    "completeness": "COMPLETE",
                    "observed_at": "2026-01-01T00:00:00+00:00"}
        self.assertEqual(idempotency_key_for(request, snapshot),
                         idempotency_key_for(request, dict(snapshot)))
        self.assertTrue(idempotency_key_for(request, snapshot).startswith(
            "relium-collector-req-1-2-"))

    def test_a_new_measurement_gets_a_new_key_rather_than_wedging(self):
        """Keying only on (request_id, attempt) wedged the request: a retry
        after a failed submit re-measures, and the API payload hash includes
        observed_at, so the same key would conflict forever."""
        request = _request()
        first = {"review_id": "r", "environment": "production", "relations": [],
                 "completeness": "COMPLETE",
                 "observed_at": "2026-01-01T00:00:00+00:00"}
        later = dict(first, observed_at="2026-01-01T00:05:00+00:00")
        self.assertNotEqual(idempotency_key_for(request, first),
                            idempotency_key_for(request, later))

    def test_different_measured_evidence_gets_a_different_key(self):
        request = _request()
        healthy = {"review_id": "r", "environment": "production",
                   "relations": [{"relation_name": "raw.orders", "row_count": 100}],
                   "completeness": "COMPLETE",
                   "observed_at": "2026-01-01T00:00:00+00:00"}
        changed = dict(healthy,
                       relations=[{"relation_name": "raw.orders", "row_count": 999}])
        self.assertNotEqual(idempotency_key_for(request, healthy),
                            idempotency_key_for(request, changed))

    def test_collector_registers_its_identity_before_submitting(self):
        """The API rejects a snapshot from an unregistered collector, so a
        collector that never registers fails at the last step."""
        client = _FakeClient()
        run_collection(_config(), client=client, reader=_RecordingReader())
        self.assertTrue(client.registered)

    def test_duplicate_submission_remains_idempotent(self):
        """The API answers 200 for an exact replay. That is success, not an
        error, and the collector must report it truthfully."""
        client = _FakeClient(submit_status=200)
        outcome = run_collection(_config(), client=client, reader=_RecordingReader())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status_code, 200)
        self.assertIn("idempotent", outcome.reason)

    def test_conflicting_replay_remains_rejected(self):
        client = _FakeClient(submit_status=409)
        outcome = run_collection(_config(), client=client, reader=_RecordingReader())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status_code, 409)
        self.assertIn("conflicting replay", outcome.reason)


# ------------------------------------------------------------- failure paths

class TruthfulFailureTests(unittest.TestCase):
    def test_api_failure_produces_a_truthful_non_zero_outcome(self):
        class _FailingClient(_FakeClient):
            def submit_snapshot(self, snapshot, idempotency_key):
                raise ReliumApiError("POST /api/metadata-snapshots returned HTTP 503",
                                     status=503)

        outcome = run_collection(_config(), client=_FailingClient(),
                                 reader=_RecordingReader())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status_code, 503)
        self.assertIn("503", outcome.reason)

    def test_warehouse_failure_produces_a_truthful_non_zero_outcome(self):
        from agent.collector.warehouse import WarehouseUnavailable

        class _BrokenReader(_RecordingReader):
            def collect_relation(self, **kwargs):
                raise WarehouseUnavailable("could not connect to the warehouse "
                                           "(OperationalError)")

        client = _FakeClient()
        outcome = run_collection(_config(), client=client, reader=_BrokenReader())
        self.assertFalse(outcome.ok)
        self.assertIn("could not connect", outcome.reason)
        self.assertEqual(client.submitted, [], "no snapshot on a failed collection")
        self.assertEqual(len(client.failures), 1, "the failure must be reported back")

    def test_warehouse_failure_reason_carries_no_dsn(self):
        reader = PostgresMetadataReader("postgresql://user:hunter2@127.0.0.1:1/none",
                                        connect_timeout=1)
        with self.assertRaises(Exception) as caught:
            reader.collect_relation(relation_name="raw.orders", columns=["id"],
                                    signals={"row_count"})
        self.assertNotIn("hunter2", str(caught.exception))

    def test_missing_relation_is_reported_as_absent_not_as_an_error(self):
        class _MissingReader(_RecordingReader):
            def collect_relation(self, **kwargs):
                raise RelationMissing("not there")

        client = _FakeClient()
        outcome = run_collection(_config(), client=client, reader=_MissingReader())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.relations_missing, ["raw.orders"])
        relation = client.submitted[0]["snapshot"]["relations"][0]
        self.assertFalse(relation["exists_in_production"])

    def test_unreadable_relation_fails_rather_than_reporting_absence(self):
        """information_schema is privilege-filtered, so a missing GRANT looks
        exactly like a dropped table - and a dropped table produces a BLOCK.
        A permissions gap must never be laundered into a schema finding."""
        from agent.collector.warehouse import RelationNotReadable

        class _UnreadableReader(_RecordingReader):
            def collect_relation(self, **kwargs):
                raise RelationNotReadable(
                    "relation raw.orders exists but these credentials cannot "
                    "read it; grant SELECT on it to the collector role")

        client = _FakeClient()
        outcome = run_collection(_config(), client=client, reader=_UnreadableReader())
        self.assertFalse(outcome.ok)
        self.assertIn("cannot read it", outcome.reason)
        self.assertIn("grant SELECT", outcome.reason)
        self.assertEqual(client.submitted, [],
                         "misleading evidence must not be submitted")
        self.assertEqual(outcome.relations_missing, [],
                         "an unreadable relation is not an absent relation")

    def test_no_pending_request_is_success_not_failure(self):
        outcome = run_collection(_config(), client=_FakeClient(requests=[]),
                                 reader=_RecordingReader())
        self.assertTrue(outcome.ok)
        self.assertIn("no pending", outcome.reason)


# ------------------------------------------------- real warehouse execution

@unittest.skipUnless(WAREHOUSE_DSN,
                     "RELIUM_TEST_WAREHOUSE_DSN not set; warehouse suite "
                     "requires a real PostgreSQL server")
class RealWarehouseTests(unittest.TestCase):
    """The one honest execution path: PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.schema = f"wh_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{cls.schema}"')
            conn.execute(f'CREATE TABLE "{cls.schema}".orders ('
                         "order_id bigint NOT NULL, discount_amount numeric, "
                         "note text)")
            conn.execute(
                f'INSERT INTO "{cls.schema}".orders (order_id, discount_amount, note) '
                "SELECT g, CASE WHEN mod(g, 5) = 0 THEN NULL ELSE g * 1.5 END, "
                "'row ' || g FROM generate_series(1, 100) g")

    @classmethod
    def tearDownClass(cls):
        import psycopg

        with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')

    def _reader(self):
        return PostgresMetadataReader(WAREHOUSE_DSN)

    def test_collects_the_requested_signals_from_a_real_table(self):
        relation = self._reader().collect_relation(
            relation_name=f"{self.schema}.orders",
            columns=["order_id", "discount_amount"],
            signals={"relation_exists", "column_exists", "data_type",
                     "is_nullable", "schema_fingerprint", "row_count", "null_rate"})
        self.assertTrue(relation["exists_in_production"])
        self.assertEqual(relation["row_count"], 100)
        self.assertTrue(relation["schema_fingerprint"])
        by_name = {c["column_name"]: c for c in relation["columns"]}
        self.assertEqual(sorted(by_name), ["discount_amount", "order_id"])
        self.assertEqual(by_name["order_id"]["null_rate"], 0.0)
        self.assertAlmostEqual(by_name["discount_amount"]["null_rate"], 0.20, places=6)
        self.assertFalse(by_name["order_id"]["is_nullable"])
        self.assertTrue(by_name["discount_amount"]["is_nullable"])

    def test_unrequested_columns_are_absent_from_the_result(self):
        relation = self._reader().collect_relation(
            relation_name=f"{self.schema}.orders", columns=["order_id"],
            signals={"row_count", "null_rate", "data_type"})
        names = {c["column_name"] for c in relation["columns"]}
        self.assertEqual(names, {"order_id"})
        self.assertNotIn("note", json.dumps(relation))

    def test_no_customer_row_value_reaches_the_result(self):
        relation = self._reader().collect_relation(
            relation_name=f"{self.schema}.orders",
            columns=["order_id", "discount_amount"],
            signals={"row_count", "null_rate", "distinct_count", "data_type"})
        blob = json.dumps(relation)
        self.assertNotIn("row 1", blob, "a cell value escaped into the metadata")
        self.assertNotIn("row 42", blob)

    def test_missing_relation_raises_relation_missing(self):
        with self.assertRaises(RelationMissing):
            self._reader().collect_relation(
                relation_name=f"{self.schema}.does_not_exist",
                columns=["order_id"], signals={"row_count"})

    def test_missing_column_is_reported_rather_than_invented(self):
        relation = self._reader().collect_relation(
            relation_name=f"{self.schema}.orders",
            columns=["order_id", "nonexistent_column"],
            signals={"data_type", "row_count", "null_rate"})
        by_name = {c["column_name"]: c for c in relation["columns"]}
        self.assertIn("nonexistent_column", by_name)
        self.assertFalse(by_name["nonexistent_column"]["exists"])
        self.assertIsNone(by_name["nonexistent_column"]["data_type"])

    def test_session_is_read_only(self):
        """A defect in query construction still cannot write."""
        import psycopg

        reader = self._reader()
        with reader._connect() as conn:
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(f'INSERT INTO "{self.schema}".orders (order_id) '
                             "VALUES (999999)")


if __name__ == "__main__":
    unittest.main()
