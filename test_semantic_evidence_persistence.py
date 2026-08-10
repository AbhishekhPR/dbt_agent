"""SQL semantic evidence, bound to the attempt that produced it.

Requires RELIUM_TEST_POSTGRES_DSN; skipped without it, like the other
PostgreSQL suites.

The rule these enforce is the one the dashboard already applies to findings
and coverage: evidence belongs to an exact attempt, and each attempt is
self-contained. The store never copies evidence between attempts on its own -
a caller that records an attempt without semantic evidence gets NULL.

Carrying evidence FORWARD is a decision the recompute path makes explicitly,
because a metadata recomputation describes the same base/head pair (a review
is bound to one pull_number/head_sha and attempts carry no SHA of their own).
That carry-forward is covered in test_metadata_review_integration.py.
"""
from __future__ import annotations

import copy
import json
import os
import unittest

from agent.api.routes import _semantic_evidence_view
from agent.deployment_review_service import review_manifest_change
from agent.github_app.runner import _semantic_evidence
from agent.metadata_evidence.review_lifecycle import begin_review

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


def manifest(sql, columns, depends_on=()):
    return {"metadata": {"project_name": "p"}, "nodes": {"model.p.fct_orders": {
        "unique_id": "model.p.fct_orders", "resource_type": "model",
        "name": "fct_orders", "schema": "a", "alias": "fct_orders", "database": "w",
        "path": "models/fct_orders.sql", "original_file_path": "models/fct_orders.sql",
        "raw_code": sql, "compiled_code": sql, "depends_on": {"nodes": list(depends_on)},
        "columns": {c: {"name": c} for c in columns},
        "config": {"materialized": "table"}, "description": ""}},
        "sources": {}, "exposures": {}, "macros": {}, "child_map": {}, "parent_map": {}}


REFUND_BASE = manifest(
    "select o.order_id, o.amount - coalesce(r.refund_amount, 0) as net_order_amount "
    "from {{ ref('stg_orders') }} o "
    "left join {{ ref('stg_refunds') }} r on r.order_id = o.order_id",
    ["order_id", "net_order_amount"], ["model.p.stg_orders", "model.p.stg_refunds"])
REFUND_HEAD = manifest(
    "select o.order_id, o.amount as net_order_amount from {{ ref('stg_orders') }} o",
    ["order_id", "net_order_amount"], ["model.p.stg_orders"])
UNCHANGED = manifest("select a from {{ ref('t') }} t", ["a"], ["model.p.t"])


def analyse(before, after):
    """Run the real review path and return (incident, evidence)."""
    result = review_manifest_change(
        manifest=copy.deepcopy(after), changed_files=["models/fct_orders.sql"],
        changed_models=["fct_orders"], deployment_id="d",
        previous_manifest=copy.deepcopy(before) if before is not None else None,
        manifest_source={"head": "gh"}, base_sha="a" * 40, head_sha="b" * 40)
    incident = result["incident"]
    return incident, _semantic_evidence(incident)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class SemanticEvidencePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        cls.store = PostgresLifecycleStore(DSN)
        cls.store.ensure_tenant("acme", "analytics", "production")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    _pull = iter(range(600, 900))

    def _begin(self, before, after, *, pull_number=None, semantic=True):
        incident, evidence = analyse(before, after)
        outcome = begin_review(
            self.store, organization_id="acme", repository_id="analytics",
            environment="production", pull_number=pull_number or next(self._pull),
            base_sha="a" * 40, head_sha=f"{(pull_number or 0):040d}" or "b" * 40,
            base_manifest=before, head_manifest=after, changed_models=["fct_orders"],
            enforcement_mode="enforce", code_health=incident["health"],
            semantic_evidence=evidence if semantic else None)
        return incident, outcome

    def _stored(self, review_id, attempt):
        row = self.store.connection.execute(
            "SELECT semantic_evidence FROM review_attempts "
            "WHERE review_id=%s AND attempt=%s", (review_id, attempt)).fetchone()
        return row["semantic_evidence"] if row else None

    # -- migration ---------------------------------------------------------

    def test_migration_0010_applied_after_0009(self):
        # The claim is about ORDER, not about 0010 being the newest migration.
        # Asserting on the tail made every later migration break this test for
        # a reason that has nothing to do with semantic evidence.
        versions = [r["version"] for r in self.store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertIn(9, versions)
        self.assertIn(10, versions)
        self.assertLess(versions.index(9), versions.index(10))

    def test_the_column_is_nullable(self):
        row = self.store.connection.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='review_attempts' AND column_name='semantic_evidence'"
        ).fetchone()
        self.assertEqual(row["is_nullable"], "YES")

    # -- states ------------------------------------------------------------

    def test_evaluated_with_changes_is_persisted_on_the_attempt(self):
        pull = next(self._pull)
        _incident, outcome = self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        stored = self._stored(outcome.review_id, outcome.attempt)
        self.assertEqual(stored["status"], "evaluated")
        kinds = {c["kind"] for m in stored["models"] for c in m["changes"]}
        self.assertIn("projection_expression_changed", kinds)
        self.assertIn("join_removed", kinds)

    def test_evaluated_with_zero_changes_is_not_null(self):
        """Ran and found nothing is a fact, and must be stored as one."""
        pull = next(self._pull)
        _incident, outcome = self._begin(UNCHANGED, UNCHANGED, pull_number=pull)
        stored = self._stored(outcome.review_id, outcome.attempt)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "evaluated")
        self.assertEqual(sum(len(m["changes"]) for m in stored["models"]), 0)

    def test_no_comparison_is_stored_as_null(self):
        """No base manifest: nothing ran, so nothing is claimed."""
        pull = next(self._pull)
        _incident, outcome = self._begin(None, REFUND_HEAD, pull_number=pull)
        self.assertIsNone(self._stored(outcome.review_id, outcome.attempt))

    def test_unavailable_and_zero_changes_are_distinguishable(self):
        zero = self._stored(*self._ids(self._begin(UNCHANGED, UNCHANGED,
                                                   pull_number=next(self._pull))))
        absent = self._stored(*self._ids(self._begin(None, REFUND_HEAD,
                                                     pull_number=next(self._pull))))
        self.assertIsNotNone(zero)
        self.assertIsNone(absent)
        self.assertIsNone(_semantic_evidence_view(absent))
        self.assertEqual(_semantic_evidence_view(zero)["change_count"], 0)

    @staticmethod
    def _ids(begun):
        _incident, outcome = begun
        return outcome.review_id, outcome.attempt

    # -- attempt binding ---------------------------------------------------

    def test_the_store_never_copies_evidence_between_attempts_by_itself(self):
        """Carry-forward is the caller's explicit decision, not the store's.

        The recompute path does pass the previous attempt's evidence forward;
        this asserts the layer beneath it stays dumb, so an attempt recorded
        without evidence is NULL rather than silently inheriting.
        """
        pull = next(self._pull)
        _incident, outcome = self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        first = self._stored(outcome.review_id, outcome.attempt)
        self.assertTrue(first["models"][0]["changes"])

        # A later attempt on the same review that compared nothing.
        self.store.record_review_decision(
            "acme", "analytics", outcome.review_id, decision="ALLOW",
            evidence_coverage="COMPLETE", health=100, attempt=outcome.attempt + 1,
            trigger="metadata_snapshot", payload={"findings": []})
        second = self._stored(outcome.review_id, outcome.attempt + 1)
        self.assertIsNone(second, "the store copied evidence on its own")
        self.assertIsNotNone(self._stored(outcome.review_id, outcome.attempt))

    def test_provenance_comes_from_the_review_not_the_evidence(self):
        pull = next(self._pull)
        _incident, outcome = self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        review = self.store.get_review("acme", "analytics", outcome.review_id)
        self.assertTrue(review["base_sha"])
        self.assertTrue(review["head_sha"])
        stored = json.dumps(self._stored(outcome.review_id, outcome.attempt))
        # Not duplicated into every change.
        self.assertNotIn("base_sha", stored)
        self.assertNotIn("head_sha", stored)

    def test_recomputation_is_idempotent_for_the_same_attempt(self):
        pull = next(self._pull)
        _incident, outcome = self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        first = self._stored(outcome.review_id, outcome.attempt)
        self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        self.assertEqual(self._stored(outcome.review_id, outcome.attempt), first)

    # -- disclosure --------------------------------------------------------

    def test_no_raw_manifest_or_parser_internals_are_stored(self):
        pull = next(self._pull)
        _incident, outcome = self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        stored = json.dumps(self._stored(outcome.review_id, outcome.attempt))
        for banned in ("raw_code", "compiled_code", "original_file_path",
                       "Traceback", "sqlglot", "depends_on"):
            self.assertNotIn(banned, stored)

    def test_the_api_view_exposes_only_allowlisted_fields(self):
        pull = next(self._pull)
        _incident, outcome = self._begin(REFUND_BASE, REFUND_HEAD, pull_number=pull)
        view = _semantic_evidence_view(self._stored(outcome.review_id, outcome.attempt))
        allowed_top = {"status", "changes", "change_count", "unavailable_models"}
        self.assertEqual(set(view) - allowed_top, set())
        for change in view["changes"]:
            self.assertIn("kind", change)
            self.assertNotIn("finding_owner", change)
            self.assertNotIn("detector", change)

    def test_an_unknown_change_kind_is_dropped_rather_than_passed_through(self):
        view = _semantic_evidence_view({
            "status": "evaluated",
            "models": [{"model_name": "m", "status": "evaluated",
                        "changes": [{"kind": "something_new", "secret": "x"}]}]})
        self.assertEqual(view["changes"], [])

    def test_a_partial_comparison_names_the_unreadable_model(self):
        view = _semantic_evidence_view({
            "status": "partial",
            "models": [
                {"model_name": "ok", "status": "evaluated",
                 "changes": [{"kind": "grouping_changed", "before_sql": "a",
                              "after_sql": "b"}]},
                {"model_name": "bad", "status": "unavailable",
                 "unavailable_reason": "SQL could not be parsed", "changes": []}]})
        self.assertEqual(view["status"], "partial")
        self.assertEqual(view["change_count"], 1)
        self.assertEqual([m["model_name"] for m in view["unavailable_models"]], ["bad"])


class RegexFallbackDoesNotFabricateEvidenceTests(unittest.TestCase):
    """A regex-proved finding is policy evidence, not AST evidence."""

    def test_unparseable_sql_yields_a_finding_but_no_before_after_cards(self):
        before = manifest("select total_sales - refund_total as v from t", ["v"])
        after = manifest("!!! not sql !!!", ["v"])
        incident, evidence = analyse(before, after)
        comparison = incident["metadata"]["manifest_comparison"]

        view = _semantic_evidence_view(evidence)
        if view is not None:
            self.assertEqual(view["changes"], [],
                             "regex fallback fabricated AST Before/After evidence")
            self.assertIn(view["status"], {"unavailable", "partial"})
        for change in comparison["material_sql_changes"]:
            self.assertEqual(change["detector"], "regex_fallback_unparsed_sql")
            self.assertNotIn("before_sql", change)


if __name__ == "__main__":
    unittest.main()
