"""Baseline selection, persistence and lifecycle binding, against real PostgreSQL.

Requires RELIUM_TEST_POSTGRES_DSN; skipped without it, like the other
PostgreSQL suites. Nothing here substitutes SQLite, an in-memory store or a
mock: baseline selection is a SQL total ordering and a row comparison, and a
fake would prove nothing about either.

The engine itself is unit-tested in test_production_metadata_comparison.py.
What these add is everything that only a database can decide:

  - which snapshot is "the previous one", and that the answer is the same
    every time it is asked
  - that the answer can never come from another tenant, repository or
    environment
  - that four outcomes stay four outcomes on the way to disk and back
  - that an attempt keeps naming the two snapshots it actually compared, after
    newer observations arrive
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from agent.api.routes import _metadata_comparison_view
from agent.metadata_evidence.production_comparison import compute_comparison
from agent.metadata_evidence.recompute import recompute_review
from agent.metadata_evidence.review_lifecycle import (
    validate_and_bind_snapshot,
)

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "acme"
REPO = "analytics"
ENV = "production"

T = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def at(minutes):
    return T + timedelta(minutes=minutes)


def orders(*, row_count=1000, null_rate=0.01, customer_id_exists=True,
           data_type="BIGINT", fingerprint="fp-a", lag=300):
    """One observation of `orders`, shaped exactly as the collector reports it."""
    return {
        "relation_name": "orders",
        "relation_database": "warehouse",
        "relation_schema": "analytics",
        "model_unique_id": "model.jaffle.orders",
        "relation_type": "table",
        "exists_in_production": True,
        "collection_status": "COLLECTED",
        "schema_fingerprint": fingerprint,
        "row_count": row_count,
        "freshness_lag_seconds": lag,
        "columns": [{
            "column_name": "customer_id",
            "exists_in_production": customer_id_exists,
            "collection_status": "COLLECTED",
            "data_type": data_type,
            "is_nullable": True,
            "null_rate": null_rate,
            "distinct_count": 400,
        }],
    }


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ProductionComparisonPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        cls.store = PostgresLifecycleStore(DSN)
        for org, repo, env in (
            (ORG, REPO, ENV),
            (ORG, REPO, "staging"),
            (ORG, "warehouse-repo", ENV),
            ("globex", REPO, ENV),
        ):
            cls.store.ensure_tenant(org, repo, env)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    # -- helpers -----------------------------------------------------------

    def submit(self, *, observed_at, relations=(), organization_id=ORG,
               repository_id=REPO, environment=ENV, completeness="COMPLETE",
               review_id=None, snapshot_id=None):
        """Persist a snapshot through the real storage path."""
        snapshot_id = snapshot_id or f"snap-{uuid.uuid4().hex[:16]}"
        snapshot, created = self.store.submit_metadata_snapshot(
            organization_id, repository_id, environment,
            snapshot_id=snapshot_id,
            idempotency_key=f"idem-{snapshot_id}",
            payload_hash=f"ph-{snapshot_id}",
            evidence_hash=f"eh-{snapshot_id}",
            observed_at=observed_at, collected_at=observed_at,
            completeness=completeness, review_id=review_id,
            relations=list(relations),
        )
        self.assertTrue(created)
        # The insert returns the snapshot header only. Everything downstream
        # needs the expanded snapshot, exactly as the lifecycle reads it.
        return self.store.get_snapshot(organization_id, repository_id, snapshot_id)

    def submit_with_received_at(self, *, observed_at, received_at, snapshot_id):
        """A snapshot with an explicit receipt time, for tie-break tests.

        Written with raw SQL because the storage path stamps received_at with
        now(), and the immutability trigger correctly refuses to let a test
        rewrite it afterwards.
        """
        self.store.connection.execute(
            "INSERT INTO metadata_snapshots (organization_id, repository_id, "
            "snapshot_id, environment, completeness, freshness_state, provenance, "
            "evidence_hash, idempotency_key, payload_hash, observed_at, "
            "collected_at, received_at) VALUES (%s,%s,%s,%s,'COMPLETE','CURRENT',"
            "'{}'::jsonb,%s,%s,%s,%s,%s,%s)",
            (ORG, REPO, snapshot_id, ENV, f"eh-{snapshot_id}",
             f"idem-{snapshot_id}", f"ph-{snapshot_id}", observed_at,
             observed_at, received_at))
        return self.store.get_snapshot(ORG, REPO, snapshot_id)

    def baseline_of(self, snapshot):
        return self.store.previous_production_snapshot(
            snapshot["organization_id"], snapshot["repository_id"],
            snapshot["environment"], snapshot_id=snapshot["snapshot_id"],
            observed_at=snapshot["observed_at"],
            received_at=snapshot["received_at"])

    def compare(self, snapshot):
        return compute_comparison(
            self.store, organization_id=snapshot["organization_id"],
            repository_id=snapshot["repository_id"],
            environment=snapshot["environment"], current_snapshot=snapshot)

    # -- migration ---------------------------------------------------------

    def test_migration_0011_applied_after_0010(self):
        # Ordering, not tail position: migration 0012 must not break this.
        versions = [r["version"] for r in self.store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertIn(11, versions)
        self.assertLess(versions.index(10), versions.index(11))

    def test_the_comparison_column_is_nullable(self):
        row = self.store.connection.execute(
            "SELECT is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name='review_attempts' AND column_name='metadata_comparison'"
        ).fetchone()
        self.assertEqual(row["is_nullable"], "YES")
        self.assertEqual(row["data_type"], "jsonb")

    # -- baseline selection ------------------------------------------------

    def test_no_prior_snapshot_yields_no_baseline(self):
        current = self.submit(observed_at=at(0), relations=[orders()])
        self.assertIsNone(self.baseline_of(current))
        result = self.compare(current)
        self.assertEqual(result["status"], "no_baseline")
        self.assertIsNone(result["baseline_snapshot_id"])
        self.assertEqual(result["changes"], [])

    def test_the_most_recent_prior_snapshot_is_chosen(self):
        oldest = self.submit(observed_at=at(10), relations=[orders()])
        previous = self.submit(observed_at=at(20), relations=[orders()])
        current = self.submit(observed_at=at(30), relations=[orders()])
        self.assertEqual(self.baseline_of(current)["snapshot_id"],
                         previous["snapshot_id"])
        self.assertNotEqual(self.baseline_of(current)["snapshot_id"],
                            oldest["snapshot_id"])

    def test_a_later_snapshot_is_never_the_baseline(self):
        previous = self.submit(observed_at=at(40), relations=[orders()])
        current = self.submit(observed_at=at(50), relations=[orders()])
        self.submit(observed_at=at(60), relations=[orders()])  # future
        self.assertEqual(self.baseline_of(current)["snapshot_id"],
                         previous["snapshot_id"])

    def test_a_snapshot_is_never_its_own_baseline(self):
        current = self.submit(observed_at=at(70), relations=[orders()])
        baseline = self.baseline_of(current)
        self.assertNotEqual(baseline["snapshot_id"], current["snapshot_id"])

    def test_another_repository_is_never_the_baseline(self):
        foreign = self.submit(observed_at=at(80), relations=[orders()],
                              repository_id="warehouse-repo")
        current = self.submit(observed_at=at(81), relations=[orders()],
                              repository_id="warehouse-repo")
        # The only prior snapshot in THIS repository is the one just submitted
        # for it; every `analytics` snapshot above is invisible here.
        self.assertEqual(self.baseline_of(current)["snapshot_id"],
                         foreign["snapshot_id"])

        analytics_current = self.submit(observed_at=at(82), relations=[orders()])
        self.assertNotEqual(self.baseline_of(analytics_current)["repository_id"],
                            "warehouse-repo")

    def test_another_environment_is_never_the_baseline(self):
        staging = self.submit(observed_at=at(90), relations=[orders()],
                              environment="staging")
        current = self.submit(observed_at=at(91), relations=[orders()])
        baseline = self.baseline_of(current)
        self.assertEqual(baseline["environment"], ENV)
        self.assertNotEqual(baseline["snapshot_id"], staging["snapshot_id"])

    def test_another_organization_is_never_the_baseline(self):
        self.submit(observed_at=at(100), relations=[orders()],
                    organization_id="globex")
        current = self.submit(observed_at=at(101), relations=[orders()],
                              organization_id="globex")
        earlier = self.submit(observed_at=at(99), relations=[orders()],
                              organization_id="globex")
        baseline = self.baseline_of(current)
        self.assertEqual(baseline["organization_id"], "globex")
        self.assertNotEqual(baseline["snapshot_id"], earlier["snapshot_id"])

    def test_a_failed_snapshot_is_not_an_eligible_baseline(self):
        eligible = self.submit(observed_at=at(110), relations=[orders()])
        self.submit(observed_at=at(111), relations=[], completeness="FAILED")
        current = self.submit(observed_at=at(112), relations=[orders()])
        self.assertEqual(self.baseline_of(current)["snapshot_id"],
                         eligible["snapshot_id"])

    def test_a_tie_on_observed_at_is_broken_deterministically(self):
        moment = at(120)
        receipt = at(121)
        a = self.submit_with_received_at(observed_at=moment, received_at=receipt,
                                         snapshot_id="snap-tie-aaa")
        b = self.submit_with_received_at(observed_at=moment, received_at=receipt,
                                         snapshot_id="snap-tie-bbb")
        current = self.submit_with_received_at(observed_at=moment,
                                               received_at=receipt,
                                               snapshot_id="snap-tie-ccc")
        chosen = {self.baseline_of(current)["snapshot_id"] for _ in range(5)}
        self.assertEqual(chosen, {b["snapshot_id"]})
        self.assertNotIn(a["snapshot_id"], chosen)

    # -- comparison over stored snapshots ---------------------------------

    def test_stored_snapshots_produce_the_expected_evidence(self):
        before = self.submit(observed_at=at(200), relations=[
            orders(row_count=1000, null_rate=0.01, customer_id_exists=True)])
        current = self.submit(observed_at=at(210), relations=[
            orders(row_count=800, null_rate=0.12, customer_id_exists=False,
                   fingerprint="fp-b")])
        result = self.compare(current)

        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["baseline_snapshot_id"], before["snapshot_id"])
        self.assertEqual(result["current_snapshot_id"], current["snapshot_id"])

        by_kind = {c["kind"]: c for c in result["changes"]}
        self.assertEqual(by_kind["row_count_changed"]["absolute_delta"], -200)
        self.assertEqual(by_kind["row_count_changed"]["relative_delta"], -0.2)
        self.assertEqual(by_kind["column_availability_changed"]["after"], False)
        self.assertIn("schema_fingerprint_changed", by_kind)

    def test_identical_stored_snapshots_are_evaluated_with_no_changes(self):
        self.submit(observed_at=at(220), relations=[orders()])
        current = self.submit(observed_at=at(230), relations=[orders()])
        result = self.compare(current)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["coverage"]["relations_compared"], 1)

    def test_a_targeted_scope_without_history_is_partial(self):
        self.submit(observed_at=at(240), relations=[orders()])
        payments = dict(orders())
        payments.update({"relation_name": "payments",
                         "model_unique_id": "model.jaffle.payments"})
        current = self.submit(observed_at=at(250), relations=[orders(), payments])
        result = self.compare(current)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["relations_observed"], 2)
        self.assertEqual(result["coverage"]["relations_compared"], 1)
        self.assertEqual([r["relation"] for r
                          in result["coverage"]["relations_without_baseline"]],
                         ["payments"])

    def test_the_baseline_snapshot_is_never_mutated_by_comparison(self):
        before = self.submit(observed_at=at(260), relations=[
            orders(row_count=1000)])
        frozen = self.store.get_snapshot(ORG, REPO, before["snapshot_id"])
        current = self.submit(observed_at=at(270), relations=[orders(row_count=1)])
        self.compare(current)
        after = self.store.get_snapshot(ORG, REPO, before["snapshot_id"])
        self.assertEqual(after["evidence_hash"], frozen["evidence_hash"])
        self.assertEqual(after["relations"][0]["row_count"], 1000)

    def test_snapshots_remain_immutable(self):
        snapshot = self.submit(observed_at=at(280), relations=[orders()])
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE metadata_snapshots SET completeness='FAILED' "
                "WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s",
                (ORG, REPO, snapshot["snapshot_id"]))
        self.store.connection.rollback()
        self.assertEqual(
            self.store.get_snapshot(ORG, REPO, snapshot["snapshot_id"],
                                    expand=False)["completeness"], "COMPLETE")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ObservationImmutabilityTests(unittest.TestCase):
    """The whole observation is immutable, not just its header.

    Before migration 0012 only `metadata_snapshots` was protected. Every
    measured value - row counts, rates, data types, existence flags - lives in
    the child tables, and all of them could be rewritten in place while the
    header stayed byte-identical. An attempt that promises "baseline X against
    current Y" is only as good as X and Y being unable to change.
    """

    @classmethod
    def setUpClass(cls):
        import psycopg

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        cls.store = PostgresLifecycleStore(DSN)
        cls.store.ensure_tenant(ORG, REPO, ENV)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        self.snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, snapshot_id=self.snapshot_id,
            idempotency_key=f"idem-{self.snapshot_id}",
            payload_hash=f"ph-{self.snapshot_id}",
            evidence_hash=f"eh-{self.snapshot_id}",
            observed_at=at(500), collected_at=at(500),
            relations=[orders(row_count=1000)],
            metrics=[{"metric_name": "orders_total", "metric_value": 1000.0,
                      "relation_name": "orders"}])

    def _refused(self, sql):
        """Run a statement that must be refused, and report why it was."""
        try:
            self.store.connection.execute(sql, (ORG, REPO, self.snapshot_id))
        except Exception as exc:
            self.store.connection.rollback()
            return str(exc)
        self.store.connection.rollback()
        self.fail(f"the database accepted a mutation it must refuse: {sql}")

    _WHERE = ("WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s")

    # -- parent ------------------------------------------------------------

    def test_parent_snapshot_update_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"UPDATE metadata_snapshots SET completeness='FAILED' {self._WHERE}"))

    def test_parent_snapshot_delete_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"DELETE FROM metadata_snapshots {self._WHERE}"))

    # -- relations ---------------------------------------------------------

    def test_relation_update_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"UPDATE snapshot_relations SET row_count=1 {self._WHERE}"))

    def test_relation_delete_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"DELETE FROM snapshot_relations {self._WHERE}"))

    # -- columns -----------------------------------------------------------

    def test_column_update_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"UPDATE snapshot_columns SET null_rate=0.99 {self._WHERE}"))

    def test_column_delete_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"DELETE FROM snapshot_columns {self._WHERE}"))

    # -- metrics -----------------------------------------------------------

    def test_metric_update_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"UPDATE snapshot_metrics SET metric_value=0 {self._WHERE}"))

    def test_metric_delete_is_rejected(self):
        self.assertIn("immutable", self._refused(
            f"DELETE FROM snapshot_metrics {self._WHERE}"))

    # -- the observation survives every refusal ----------------------------

    def test_the_observation_is_unchanged_after_every_refused_mutation(self):
        before = self.store.get_snapshot(ORG, REPO, self.snapshot_id)
        for sql in (
            f"UPDATE metadata_snapshots SET completeness='FAILED' {self._WHERE}",
            f"UPDATE snapshot_relations SET row_count=1 {self._WHERE}",
            f"UPDATE snapshot_columns SET null_rate=0.99 {self._WHERE}",
            f"UPDATE snapshot_metrics SET metric_value=0 {self._WHERE}",
            f"DELETE FROM snapshot_columns {self._WHERE}",
            f"DELETE FROM snapshot_relations {self._WHERE}",
            f"DELETE FROM snapshot_metrics {self._WHERE}",
            f"DELETE FROM metadata_snapshots {self._WHERE}",
        ):
            self._refused(sql)
        self.assertEqual(self.store.get_snapshot(ORG, REPO, self.snapshot_id),
                         before)

    # -- ingest is untouched -----------------------------------------------

    def test_normal_ingest_still_succeeds(self):
        """The triggers fire on UPDATE and DELETE only; INSERT is the whole
        point of the table and must be unaffected."""
        snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        snapshot, created = self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, snapshot_id=snapshot_id,
            idempotency_key=f"idem-{snapshot_id}", payload_hash="ph",
            evidence_hash="eh", observed_at=at(510), collected_at=at(510),
            relations=[orders(row_count=1234)],
            metrics=[{"metric_name": "m", "metric_value": 1.0}])
        self.assertTrue(created)
        stored = self.store.get_snapshot(ORG, REPO, snapshot_id)
        self.assertEqual(stored["relations"][0]["row_count"], 1234)
        self.assertEqual(len(stored["relations"][0]["columns"]), 1)
        self.assertEqual(len(stored["metrics"]), 1)

    def test_idempotent_replay_still_works(self):
        snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        kwargs = dict(
            snapshot_id=snapshot_id, idempotency_key=f"idem-{snapshot_id}",
            payload_hash="same-payload", evidence_hash="eh",
            observed_at=at(520), collected_at=at(520),
            relations=[orders(row_count=42)])
        first, created_first = self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, **kwargs)
        second, created_second = self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, **dict(kwargs, snapshot_id=f"snap-{uuid.uuid4().hex[:16]}"))
        self.assertTrue(created_first)
        # A replay of the same key with the same payload returns the ORIGINAL
        # snapshot and writes nothing - it must not be turned into a mutation
        # attempt by the new triggers.
        self.assertFalse(created_second)
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(
            self.store.get_snapshot(ORG, REPO, snapshot_id)["relations"][0]["row_count"],
            42)

    def test_a_conflicting_replay_is_still_a_conflict_not_a_mutation(self):
        from agent.postgres_lifecycle_store import SnapshotConflict

        snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        kwargs = dict(
            idempotency_key=f"idem-{snapshot_id}", evidence_hash="eh",
            observed_at=at(530), collected_at=at(530))
        self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, snapshot_id=snapshot_id, payload_hash="first",
            relations=[orders(row_count=7)], **kwargs)
        with self.assertRaises(SnapshotConflict):
            self.store.submit_metadata_snapshot(
                ORG, REPO, ENV, snapshot_id=f"snap-{uuid.uuid4().hex[:16]}",
                payload_hash="second", relations=[orders(row_count=8)], **kwargs)
        self.assertEqual(
            self.store.get_snapshot(ORG, REPO, snapshot_id)["relations"][0]["row_count"],
            7)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class CardinalityContractTests(unittest.TestCase):
    """Cardinality is distinct_count / row_count, and must survive the round trip.

    Stored as BIGINT it did not: PostgreSQL rounds on the way in, so a column
    that is 37% distinct was persisted as 0 - not an imprecise 0.37, but a
    positive claim that the column had no distinct values.
    """

    @classmethod
    def setUpClass(cls):
        import psycopg

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        cls.store = PostgresLifecycleStore(DSN)
        cls.store.ensure_tenant(ORG, REPO, ENV)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def submit(self, *, observed_at, cardinality, distinct_count, row_count):
        snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        relation = orders(row_count=row_count)
        relation["columns"][0].update({"cardinality": cardinality,
                                       "distinct_count": distinct_count})
        self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, snapshot_id=snapshot_id,
            idempotency_key=f"idem-{snapshot_id}", payload_hash=f"ph-{snapshot_id}",
            evidence_hash=f"eh-{snapshot_id}", observed_at=observed_at,
            collected_at=observed_at, relations=[relation])
        return self.store.get_snapshot(ORG, REPO, snapshot_id)

    def test_the_column_is_a_floating_point_type(self):
        row = self.store.connection.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='snapshot_columns' AND column_name='cardinality'"
        ).fetchone()
        self.assertEqual(row["data_type"], "double precision")

    def test_row_count_100_distinct_37_persists_as_0_37(self):
        """The named regression: 37/100 must not read back as 0."""
        stored = self.submit(observed_at=at(600), row_count=100,
                             distinct_count=37, cardinality=37 / 100)
        value = stored["relations"][0]["columns"][0]["cardinality"]
        self.assertAlmostEqual(value, 0.37, places=9)
        self.assertNotEqual(value, 0)

    def test_a_fractional_cardinality_survives_the_comparison(self):
        self.submit(observed_at=at(610), row_count=100, distinct_count=37,
                    cardinality=0.37)
        current = self.submit(observed_at=at(620), row_count=100,
                              distinct_count=44, cardinality=0.44)
        result = compute_comparison(
            self.store, organization_id=ORG, repository_id=REPO,
            environment=ENV, current_snapshot=current)
        by_kind = {c["kind"]: c for c in result["changes"]}

        cardinality = by_kind["cardinality_changed"]
        self.assertAlmostEqual(cardinality["before"], 0.37, places=9)
        self.assertAlmostEqual(cardinality["after"], 0.44, places=9)
        self.assertAlmostEqual(cardinality["percentage_point_delta"], 7.0, places=6)
        # Not a count: a percentage-point delta, like the other rates.
        self.assertNotIn("absolute_delta", cardinality)

        # distinct_count keeps count semantics alongside it.
        distinct = by_kind["distinct_count_changed"]
        self.assertEqual(distinct["absolute_delta"], 7)

    def test_the_api_projects_a_truthful_fractional_cardinality(self):
        self.submit(observed_at=at(630), row_count=100, distinct_count=37,
                    cardinality=0.37)
        current = self.submit(observed_at=at(640), row_count=100,
                              distinct_count=44, cardinality=0.44)
        view = _metadata_comparison_view(compute_comparison(
            self.store, organization_id=ORG, repository_id=REPO,
            environment=ENV, current_snapshot=current))
        cardinality = next(c for c in view["changes"]
                           if c["kind"] == "cardinality_changed")
        self.assertAlmostEqual(cardinality["before"], 0.37, places=9)
        self.assertNotIn("absolute_delta", cardinality)

    def test_a_value_outside_zero_to_one_is_refused(self):
        """The contract is enforced, not merely documented."""
        with self.assertRaises(Exception):
            self.submit(observed_at=at(650), row_count=100, distinct_count=37,
                        cardinality=37.0)
        self.store.connection.rollback()


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ComparisonPersistenceTests(unittest.TestCase):
    """The four states, and the binding that must not drift."""

    @classmethod
    def setUpClass(cls):
        import psycopg

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        cls.store = PostgresLifecycleStore(DSN)
        cls.store.ensure_tenant(ORG, REPO, ENV)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    _pull = iter(range(100, 400))

    def _review(self):
        """A persisted review in an environment of its own.

        The baseline is selected per environment, not per review - that is the
        whole point of the feature. So a test that wants "this snapshot has no
        prior observation" has to isolate the environment; otherwise another
        test's snapshot legitimately becomes its baseline, and the assertion
        would be testing the harness rather than the code.
        """
        pull = next(self._pull)
        environment = f"production-{pull}"
        review_id = f"rev-{pull}"
        self.store.ensure_tenant(ORG, REPO, environment)
        self.store.upsert_pr_review(
            ORG, REPO, environment, review_id=review_id, pull_number=pull,
            base_sha="a" * 40, head_sha=f"{pull:040d}",
            base_manifest_hash="bh", head_manifest_hash="hh",
            enforcement_mode="enforce", policy_version="v1", policy_hash="ph",
            metadata_required=True, payload={"plan": {"targets": []}})
        self.store.record_review_decision(
            ORG, REPO, review_id, decision=None, evidence_coverage="UNKNOWN",
            health=100, attempt=1, trigger="initial")
        return review_id, environment

    def _snapshot(self, review, *, observed_at, relations):
        review_id, environment = review
        snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        self.store.submit_metadata_snapshot(
            ORG, REPO, environment, snapshot_id=snapshot_id,
            idempotency_key=f"idem-{snapshot_id}", payload_hash=f"ph-{snapshot_id}",
            evidence_hash=f"eh-{snapshot_id}", observed_at=observed_at,
            collected_at=observed_at, review_id=review_id,
            relations=list(relations))
        validate_and_bind_snapshot(
            self.store, organization_id=ORG, repository_id=REPO,
            environment=environment, review_id=review_id,
            snapshot=self.store.get_snapshot(ORG, REPO, snapshot_id))
        return self.store.get_snapshot(ORG, REPO, snapshot_id)

    def _stored(self, review, attempt):
        review_id = review[0] if isinstance(review, tuple) else review
        row = self.store.connection.execute(
            "SELECT metadata_comparison FROM review_attempts "
            "WHERE organization_id=%s AND repository_id=%s AND review_id=%s "
            "AND attempt=%s", (ORG, REPO, review_id, attempt)).fetchone()
        return row["metadata_comparison"] if row else None

    def _recompute(self, review):
        review_id, environment = review
        return recompute_review(self.store, organization_id=ORG,
                                repository_id=REPO, environment=environment,
                                review_id=review_id)

    # -- the four states ---------------------------------------------------

    def test_an_attempt_that_never_compared_stores_sql_null(self):
        """A review still waiting for metadata makes no claim at all."""
        review = self._review()
        self.assertIsNone(self._stored(review, 1))

    def test_no_baseline_is_stored_as_a_document_not_as_null(self):
        review = self._review()
        self._snapshot(review, observed_at=at(300), relations=[orders()])
        outcome = self._recompute(review)
        stored = self._stored(review, outcome["attempt"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "no_baseline")
        self.assertIsNone(stored["baseline_snapshot_id"])

    def test_evaluated_with_zero_changes_is_stored_as_a_document(self):
        review = self._review()
        self._snapshot(review, observed_at=at(310), relations=[orders()])
        self._recompute(review)
        self._snapshot(review, observed_at=at(320), relations=[orders()])
        outcome = self._recompute(review)
        stored = self._stored(review, outcome["attempt"])
        self.assertEqual(stored["status"], "evaluated")
        self.assertEqual(stored["changes"], [])

    def test_the_four_states_are_not_collapsed(self):
        never = self._review()

        no_baseline_review = self._review()
        self._snapshot(no_baseline_review, observed_at=at(330),
                       relations=[orders()])
        no_baseline = self._recompute(no_baseline_review)

        zero_review = self._review()
        self._snapshot(zero_review, observed_at=at(340), relations=[orders()])
        self._recompute(zero_review)
        self._snapshot(zero_review, observed_at=at(350), relations=[orders()])
        zero = self._recompute(zero_review)

        partial_review = self._review()
        self._snapshot(partial_review, observed_at=at(360), relations=[orders()])
        self._recompute(partial_review)
        payments = dict(orders())
        payments.update({"relation_name": "payments",
                         "model_unique_id": "model.jaffle.payments"})
        self._snapshot(partial_review, observed_at=at(370),
                       relations=[orders(), payments])
        partial = self._recompute(partial_review)

        self.assertIsNone(self._stored(never, 1))
        self.assertEqual(
            self._stored(no_baseline_review, no_baseline["attempt"])["status"],
            "no_baseline")
        self.assertEqual(self._stored(zero_review, zero["attempt"])["status"],
                         "evaluated")
        self.assertEqual(self._stored(partial_review, partial["attempt"])["status"],
                         "partial")

    # -- binding -----------------------------------------------------------

    def test_an_attempt_keeps_naming_the_snapshots_it_compared(self):
        review = self._review()
        a = self._snapshot(review, observed_at=at(400),
                           relations=[orders(row_count=1000)])
        self._recompute(review)
        b = self._snapshot(review, observed_at=at(410),
                           relations=[orders(row_count=800)])
        second = self._recompute(review)
        recorded = self._stored(review, second["attempt"])
        self.assertEqual(recorded["baseline_snapshot_id"], a["snapshot_id"])
        self.assertEqual(recorded["current_snapshot_id"], b["snapshot_id"])

        # A third observation arrives. The earlier attempt must not start
        # describing a comparison it never made.
        c = self._snapshot(review, observed_at=at(420),
                           relations=[orders(row_count=100)])
        third = self._recompute(review)
        unchanged = self._stored(review, second["attempt"])
        self.assertEqual(unchanged["baseline_snapshot_id"], a["snapshot_id"])
        self.assertEqual(unchanged["current_snapshot_id"], b["snapshot_id"])
        self.assertEqual(unchanged["changes"][0]["absolute_delta"], -200)

        latest = self._stored(review, third["attempt"])
        self.assertEqual(latest["baseline_snapshot_id"], b["snapshot_id"])
        self.assertEqual(latest["current_snapshot_id"], c["snapshot_id"])
        self.assertEqual(latest["changes"][0]["absolute_delta"], -700)

    def test_recomputation_is_idempotent_and_does_not_move_the_baseline(self):
        review = self._review()
        self._snapshot(review, observed_at=at(430), relations=[
            orders(row_count=1000)])
        self._recompute(review)
        self._snapshot(review, observed_at=at(440), relations=[
            orders(row_count=800)])
        first = self._recompute(review)
        before = self._stored(review, first["attempt"])

        repeat = self._recompute(review)
        self.assertEqual(repeat["status"], "already_recomputed")
        self.assertEqual(repeat["attempt"], first["attempt"])
        self.assertEqual(self._stored(review, first["attempt"]), before)
        self.assertEqual(
            len(self.store.review_attempts(ORG, REPO, review[0])),
            first["attempt"])

    # -- api projection over real rows ------------------------------------

    def test_the_api_projection_of_a_stored_document_leaks_nothing(self):
        review = self._review()
        self._snapshot(review, observed_at=at(450), relations=[
            orders(row_count=1000, null_rate=0.01)])
        self._recompute(review)
        self._snapshot(review, observed_at=at(460), relations=[
            orders(row_count=800, null_rate=0.12, data_type="VARCHAR")])
        outcome = self._recompute(review)

        view = _metadata_comparison_view(self._stored(review, outcome["attempt"]))
        self.assertEqual(view["status"], "evaluated")
        self.assertEqual(set(view), {"status", "baseline_snapshot_id",
                                     "current_snapshot_id", "baseline_observed_at",
                                     "current_observed_at", "changes",
                                     "change_count", "coverage"})
        allowed_per_change = {"kind", "model", "relation", "column", "signal",
                              "before", "after", "absolute_delta",
                              "relative_delta", "percentage_point_delta"}
        for change in view["changes"]:
            self.assertLessEqual(set(change), allowed_per_change)

        by_kind = {c["kind"]: c for c in view["changes"]}
        self.assertEqual(by_kind["null_rate_changed"]["percentage_point_delta"], 11.0)
        self.assertEqual(by_kind["column_type_changed"]["after"], "VARCHAR")

    def test_no_snapshot_internals_appear_anywhere_in_the_projection(self):
        review = self._review()
        self._snapshot(review, observed_at=at(470), relations=[orders()])
        self._recompute(review)
        self._snapshot(review, observed_at=at(480), relations=[
            orders(row_count=2)])
        outcome = self._recompute(review)
        import json

        blob = json.dumps(_metadata_comparison_view(
            self._stored(review, outcome["attempt"])))
        for forbidden in ("evidence_hash", "idempotency_key", "payload_hash",
                          "provenance", "collector_id", "collector_version",
                          "min_value", "max_value", "select ", "adapter_type"):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
