"""Migration 0020: KPI impact must reach the database and come back.

The in-memory suite in ``test_review_kpi_impact.py`` proves the analysis
produces the document and that both review paths hand it to the store. What it
cannot prove is that PostgreSQL has somewhere to put it — a column that does
not exist fails on INSERT, and a JSONB round trip is a property of the database
rather than of a dictionary that was never written down.

So this suite is database-gated, like the other lifecycle suites, and asserts
the three things only a real server can answer:

  * the migration ran, in an order that puts it after the table it alters;
  * a document survives INSERT and SELECT unchanged;
  * an attempt written without one reads back as SQL NULL, not as an empty
    document — the distinction the column exists for, held by the database
    rather than by the read path remembering to be careful.
"""
from __future__ import annotations

import os
import unittest

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG, REPO, ENV = "kpi-org", "kpi-repo", "production"
REVIEW = "review-kpi-impact"
LEGACY_REVIEW = "review-kpi-legacy"
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40

DOCUMENT = {
    "status": "evaluated",
    "changed_models": ["int_subscription_revenue"],
    "impacted_kpis": ["Recurring Revenue", "Revenue / GMV"],
    "unaffected_kpis": [],
    "impact_paths": [["int_subscription_revenue", "Revenue / GMV"]],
    "confidence": 90,
    "impacted_kpi_details": [{
        "name": "Revenue / GMV",
        "confidence": 90,
        "impacted_by_models": ["int_subscription_revenue"],
        "related_columns": ["revenue"],
        "reasons": ["Direct model match: int_subscription_revenue"],
        "impact_paths": [],
        "column_impact": "fallback",
    }],
    "column_level_evidence": [],
    "fallback_reason": "changed columns unavailable",
    "impacted_count": 2,
    "unaffected_count": 0,
}


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; the column is a database property")
class ReviewKpiImpactMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.store = PostgresLifecycleStore(DSN)
        cls.store.ensure_tenant(ORG, REPO, ENV)
        # Distinct pull numbers: reviews are unique on (pull number, head SHA),
        # so two rows sharing both would collide before any of this is tested.
        for pull_number, review_id in enumerate((REVIEW, LEGACY_REVIEW), start=1):
            cls.store.upsert_pr_review(
                ORG, REPO, ENV, review_id=review_id, pull_number=pull_number,
                base_sha=BASE_SHA, head_sha=HEAD_SHA,
                base_manifest_hash="base", head_manifest_hash="head",
                enforcement_mode="enforce", policy_version="default-v1",
                policy_hash="policy", metadata_required=False,
                payload={"plan": {"targets": []}})

        cls.store.record_review_decision(
            ORG, REPO, REVIEW, decision="ALLOW", evidence_coverage="COMPLETE",
            health=100, attempt=1, trigger="initial",
            payload={"findings": []}, kpi_impact=DOCUMENT)
        # Exactly what every attempt written before this migration looks like:
        # the same call, with nothing to say about KPI impact.
        cls.store.record_review_decision(
            ORG, REPO, LEGACY_REVIEW, decision="ALLOW",
            evidence_coverage="COMPLETE", health=100, attempt=1,
            trigger="initial", payload={"findings": []})

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def _attempt(self, review_id):
        return self.store.review_attempts(ORG, REPO, review_id)[0]

    def test_the_migration_is_applied_after_the_table_it_alters(self):
        versions = [r["version"] for r in self.store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertIn(20, versions)
        # 0010 added semantic_evidence to the same table; if review_attempts
        # were somehow created later, this ALTER could not have run.
        self.assertIn(10, versions)
        self.assertLess(versions.index(10), versions.index(20))

    def test_the_column_exists_on_review_attempts_and_is_jsonb(self):
        row = self.store.connection.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='review_attempts' AND column_name='kpi_impact'"
        ).fetchone()
        self.assertIsNotNone(row, "migration 0020 did not add the column")
        self.assertEqual(row["data_type"], "jsonb")

    def test_the_document_survives_the_round_trip_unchanged(self):
        self.assertEqual(self._attempt(REVIEW)["kpi_impact"], DOCUMENT)

    def test_the_nested_per_kpi_detail_survives_too(self):
        """JSONB must not flatten the structure the dashboard reads."""
        detail = self._attempt(REVIEW)["kpi_impact"]["impacted_kpi_details"][0]
        self.assertEqual(detail["impacted_by_models"],
                         ["int_subscription_revenue"])
        self.assertEqual(detail["column_impact"], "fallback")

    def test_an_attempt_written_without_it_reads_back_as_null(self):
        """The whole reason for a dedicated column.

        Every historical attempt is this row. It must say "never inferred",
        never "inferred, nothing impacted".
        """
        self.assertIsNone(self._attempt(LEGACY_REVIEW)["kpi_impact"])

    def test_the_api_projection_tells_the_two_apart(self):
        from agent.api.routes import _kpi_impact_view

        self.assertIsNone(_kpi_impact_view(self._attempt(LEGACY_REVIEW)["kpi_impact"]))
        view = _kpi_impact_view(self._attempt(REVIEW)["kpi_impact"])
        self.assertEqual(view["status"], "evaluated")
        self.assertEqual(view["impacted_count"], 2)


if __name__ == "__main__":
    unittest.main()
