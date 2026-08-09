"""Metadata-backed decision logic.

Pure-function tests: no database and no network, so every case is exact and
deterministic. The real-PostgreSQL and live-GitHub coverage lives in
test_metadata_evidence_store.py and the E2E suite.

The property under test throughout is that missing, stale, partial or
unsupported production evidence NEVER produces a final ALLOW, and that a
column created inside the pull request is never blocked merely for being
absent from current production.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.metadata_evidence.collection_plan import build_collection_plan
from agent.metadata_evidence.decision import (
    classify_freshness,
    evaluate_metadata_decision,
)


def _now():
    return datetime.now(timezone.utc)


def _model(name, deps=(), cols=(), schema="analytics"):
    return {"resource_type": "model", "name": name, "schema": schema, "alias": name,
            "database": "warehouse", "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols}}


def _plan(*, changed=("fct_orders",), critical=()):
    base = {
        "nodes": {"model.a.fct_orders": _model("fct_orders", ["source.a.raw.orders"],
                                               ["order_id"])},
        "sources": {"source.a.raw.orders": {
            "schema": "raw", "name": "orders", "database": "warehouse",
            "columns": {"order_id": {}, "discount_amount": {}}}},
    }
    head = {
        "nodes": {"model.a.fct_orders": _model(
            "fct_orders", ["source.a.raw.orders"], ["order_id", "net_revenue"])},
        "sources": base["sources"],
    }
    return build_collection_plan(base_manifest=base, head_manifest=head,
                                 changed_models=list(changed),
                                 critical_models=list(critical))


def _snapshot(relations, *, completeness="COMPLETE", observed_at=None,
              ttl_seconds=3600, freshness_state=None):
    return {
        "completeness": completeness,
        "freshness_state": freshness_state,
        "observed_at": observed_at or _now(),
        "ttl_seconds": ttl_seconds,
        "relations": relations,
    }


def _relation(name, columns=(), **overrides):
    relation = {
        "relation_name": name,
        "exists_in_production": True,
        "collection_status": "COLLECTED",
        "columns": list(columns),
    }
    relation.update(overrides)
    return relation


def _column(name, data_type="numeric", **overrides):
    column = {"column_name": name, "data_type": data_type}
    column.update(overrides)
    return column


def _targets(**overrides):
    """A single external target, shaped like a real plan entry."""
    target = {
        "relation_name": "raw.orders",
        "dependency_kind": "external",
        "columns": ["discount_amount"],
        "criticality": "standard",
    }
    target.update(overrides)
    return {"targets": [target], "metadata_required": True}


class FreshnessTests(unittest.TestCase):
    def test_fresh_snapshot_is_current(self):
        snapshot = _snapshot([], observed_at=_now() - timedelta(minutes=4))
        self.assertEqual(classify_freshness(snapshot), "CURRENT")

    def test_snapshot_older_than_ttl_is_stale(self):
        snapshot = _snapshot([], observed_at=_now() - timedelta(hours=4),
                             ttl_seconds=900)
        self.assertEqual(classify_freshness(snapshot), "STALE")

    def test_critical_models_use_the_shorter_ttl(self):
        snapshot = _snapshot([], observed_at=_now() - timedelta(minutes=30),
                             ttl_seconds=None)
        self.assertEqual(classify_freshness(snapshot, criticality="critical"), "STALE")
        self.assertEqual(classify_freshness(snapshot, criticality="standard"), "CURRENT")

    def test_clock_skew_tolerance_does_not_admit_old_evidence(self):
        just_inside = _snapshot([], observed_at=_now() - timedelta(seconds=930),
                                ttl_seconds=900)
        well_outside = _snapshot([], observed_at=_now() - timedelta(seconds=1800),
                                 ttl_seconds=900)
        self.assertEqual(classify_freshness(just_inside), "CURRENT")
        self.assertEqual(classify_freshness(well_outside), "STALE")

    def test_missing_observed_at_is_unknown_not_current(self):
        self.assertEqual(classify_freshness({"relations": []}), "UNKNOWN")
        self.assertEqual(classify_freshness(None), "UNKNOWN")

    def test_one_stale_relation_makes_the_snapshot_partially_stale(self):
        snapshot = _snapshot(
            [_relation("raw.orders", observed_at=_now() - timedelta(hours=5))],
            observed_at=_now(), ttl_seconds=3600)
        self.assertEqual(classify_freshness(snapshot), "PARTIALLY_STALE")


class MissingEvidenceTests(unittest.TestCase):
    """Required production evidence unavailable must never read as success."""

    def test_shadow_missing_metadata_warns_with_unchanged_health(self):
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=None, enforcement_mode="shadow",
            code_health=100, request_expired=True)
        self.assertEqual(result.decision, "WARN")
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.health, 100)

    def test_enforce_missing_metadata_blocks_with_unchanged_health(self):
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=None, enforcement_mode="enforce",
            code_health=100, request_expired=True)
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.health, 100)

    def test_pending_metadata_yields_no_decision_at_all(self):
        """Waiting is not a verdict. The decision must be absent, not ALLOW."""
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=None, enforcement_mode="enforce",
            code_health=100)
        self.assertIsNone(result.decision)
        self.assertEqual(result.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.health, 100)

    def test_optional_metadata_absent_is_not_evaluated_and_allows(self):
        plan = {"targets": [{"relation_name": "analytics.stg", "columns": [],
                             "dependency_kind": "head_derived"}],
                "metadata_required": False}
        result = evaluate_metadata_decision(
            plan=plan, snapshot=None, enforcement_mode="enforce", code_health=100)
        self.assertEqual(result.lifecycle_state, "METADATA_NOT_REQUIRED")
        self.assertEqual(result.coverage, "COMPLETE")
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(result.evidence["production_metadata"], "NOT EVALUATED")

    def test_health_is_never_changed_by_missing_evidence(self):
        for mode in ("shadow", "enforce"):
            for health in (55, 80, 100):
                with self.subTest(mode=mode, health=health):
                    result = evaluate_metadata_decision(
                        plan=_targets(), snapshot=None, enforcement_mode=mode,
                        code_health=health, request_expired=True)
                    self.assertEqual(result.health, health)


class StaleAndPartialTests(unittest.TestCase):
    def test_stale_metadata_is_not_treated_as_current(self):
        snapshot = _snapshot(
            [_relation("raw.orders", [_column("discount_amount")])],
            observed_at=_now() - timedelta(hours=6), ttl_seconds=900)
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=snapshot, enforcement_mode="enforce",
            code_health=100)
        self.assertEqual(result.evidence["production_metadata"], "STALE")
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.health, 100)
        self.assertEqual(result.lifecycle_state, "METADATA_STALE")

    def test_stale_metadata_in_shadow_warns(self):
        snapshot = _snapshot(
            [_relation("raw.orders", [_column("discount_amount")])],
            observed_at=_now() - timedelta(hours=6), ttl_seconds=900)
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=snapshot, enforcement_mode="shadow",
            code_health=100)
        self.assertEqual(result.decision, "WARN")
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.health, 100)

    def test_partial_snapshot_is_not_complete(self):
        snapshot = _snapshot(
            [_relation("raw.orders", [_column("discount_amount")])],
            completeness="PARTIAL")
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=snapshot, enforcement_mode="enforce",
            code_health=100)
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertNotEqual(result.decision, "ALLOW")
        self.assertEqual(result.lifecycle_state, "METADATA_PARTIAL")

    def test_unsupported_signal_never_passes(self):
        snapshot = _snapshot([_relation("raw.orders", [], collection_status="UNSUPPORTED")])
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=snapshot, enforcement_mode="enforce",
            code_health=100)
        self.assertEqual(result.evidence["production_metadata"], "UNSUPPORTED")
        self.assertNotEqual(result.decision, "ALLOW")


class DecisionCaseTests(unittest.TestCase):
    """The named metadata-backed decision cases."""

    def _decide(self, targets, relations, mode="enforce", **kwargs):
        return evaluate_metadata_decision(
            plan={"targets": targets, "metadata_required": True},
            snapshot=_snapshot(relations), enforcement_mode=mode,
            code_health=100, **kwargs)

    def _codes(self, result):
        return {f.code for f in result.findings}

    # case 1
    def test_external_new_column_present_and_compatible_allows(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"],
              "column_types": {"discount_amount": "numeric"}}],
            [_relation("raw.orders", [_column("discount_amount", "numeric",
                                              null_rate=0.01)])])
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(result.coverage, "COMPLETE")

    # case 2
    def test_external_new_column_missing_blocks(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"]}],
            [_relation("raw.orders", [_column("order_id", "bigint")])])
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("column.missing_in_production", self._codes(result))

    def test_column_reported_absent_by_the_collector_blocks(self):
        """The shape the REAL collector produces, not an omitted column.

        A collector answers for every column the request named, so an absent
        one arrives as a present entry carrying exists_in_production=False -
        never as a gap in the array. Testing only the omitted shape let a
        dropped production column decide ALLOW with no findings at all.
        """
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"]}],
            [_relation("raw.orders", [
                _column("order_id", "bigint"),
                _column("discount_amount", None, exists_in_production=False),
            ])])
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("column.missing_in_production", self._codes(result))

    def test_absent_column_warns_rather_than_blocks_in_shadow_mode(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"]}],
            [_relation("raw.orders", [
                _column("discount_amount", None, exists_in_production=False)])],
            mode="shadow")
        self.assertEqual(result.decision, "WARN")
        self.assertIn("column.missing_in_production", self._codes(result))

    def test_present_column_is_not_reported_as_absent(self):
        """The flag must not manufacture a finding for a column that is there."""
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"],
              "column_types": {"discount_amount": "numeric"}}],
            [_relation("raw.orders", [
                _column("discount_amount", "numeric", exists_in_production=True,
                        null_rate=0.01)])])
        self.assertEqual(result.decision, "ALLOW")
        self.assertNotIn("column.missing_in_production", self._codes(result))

    def test_absent_flag_defaults_to_present_for_older_snapshots(self):
        """A stored snapshot without the flag must not become a BLOCK."""
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"],
              "column_types": {"discount_amount": "numeric"}}],
            [_relation("raw.orders", [_column("discount_amount", "numeric",
                                              null_rate=0.01)])])
        self.assertEqual(result.decision, "ALLOW")
        self.assertNotIn("column.missing_in_production", self._codes(result))

    def test_missing_external_relation_blocks(self):
        result = self._decide(
            [{"relation_name": "raw.missing", "dependency_kind": "external",
              "columns": ["x"]}],
            [])
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("relation.missing_in_production", self._codes(result))

    # case 3
    def test_high_null_rate_is_detected(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"]}],
            [_relation("raw.orders", [_column("discount_amount", "numeric",
                                              null_rate=0.82)])])
        self.assertIn("column.high_null_rate", self._codes(result))
        self.assertEqual(result.decision, "WARN")

    def test_high_null_rate_on_a_critical_model_blocks(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"], "criticality": "critical"}],
            [_relation("raw.orders", [_column("discount_amount", "numeric",
                                              null_rate=0.82)])])
        self.assertEqual(result.decision, "BLOCK")

    # case 4 - the important one
    def test_head_derived_column_absent_from_production_is_not_blocked(self):
        result = self._decide(
            [{"relation_name": "analytics.stg_orders",
              "dependency_kind": "head_derived",
              "columns": ["margin_amount"]}],
            [])  # not in production at all
        self.assertNotEqual(result.decision, "BLOCK")
        self.assertIn("dependency.head_derived_absent_ok", self._codes(result))
        self.assertNotIn("relation.missing_in_production", self._codes(result))
        self.assertNotIn("column.missing_in_production", self._codes(result))

    def test_head_derived_and_external_are_judged_differently(self):
        """Same absence, opposite verdicts, decided solely by dependency kind."""
        head_derived = self._decide(
            [{"relation_name": "analytics.new_model",
              "dependency_kind": "head_derived", "columns": ["x"]}], [])
        external = self._decide(
            [{"relation_name": "analytics.new_model",
              "dependency_kind": "external", "columns": ["x"]}], [])
        self.assertNotEqual(head_derived.decision, "BLOCK")
        self.assertEqual(external.decision, "BLOCK")

    # case 5
    def test_type_mismatch_blocks(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["discount_amount"],
              "column_types": {"discount_amount": "numeric"}}],
            [_relation("raw.orders", [_column("discount_amount", "varchar")])])
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("column.type_mismatch", self._codes(result))

    def test_compatible_type_aliases_do_not_block(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["amount"], "column_types": {"amount": "decimal(10,2)"}}],
            [_relation("raw.orders", [_column("amount", "numeric")])])
        self.assertNotIn("column.type_mismatch", self._codes(result))

    def test_unknown_types_are_not_guessed_at(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["payload"], "column_types": {"payload": "jsonb"}}],
            [_relation("raw.orders", [_column("payload", "some_custom_type")])])
        self.assertNotIn("column.type_mismatch", self._codes(result))

    # case 6
    def test_join_key_type_incompatibility_blocks(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["customer_id"], "join_relation": "raw.customers",
              "join_pairs": [["customer_id", "id"]]},
             {"relation_name": "raw.customers", "dependency_kind": "external",
              "columns": ["id"]}],
            [_relation("raw.orders", [_column("customer_id", "varchar")]),
             _relation("raw.customers", [_column("id", "bigint")])])
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("join.key_type_incompatible", self._codes(result))

    # case 7
    def test_duplicate_amplification_is_detected(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": ["customer_id"], "join_keys": ["customer_id"]}],
            [_relation("raw.orders", [_column("customer_id", "bigint",
                                              duplicate_rate=0.31)])])
        self.assertIn("join.duplicate_amplification", self._codes(result))
        self.assertEqual(result.decision, "WARN")

    # case 8
    def test_missing_watermark_column_blocks(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": [], "watermark_column": "updated_at"}],
            [_relation("raw.orders", [_column("order_id", "bigint")])])
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("incremental.watermark_missing", self._codes(result))

    def test_stale_watermark_warns(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": [], "watermark_column": "updated_at"}],
            [_relation("raw.orders", [_column("updated_at", "timestamptz")],
                       freshness_lag_seconds=60 * 60 * 40)])
        self.assertIn("incremental.watermark_stale", self._codes(result))

    # case 9
    def test_production_drift_is_surfaced(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": [], "base_schema_fingerprint": "fp-base"}],
            [_relation("raw.orders", [], schema_fingerprint="fp-different")])
        self.assertIn("production.schema_drift", self._codes(result))
        self.assertEqual(result.decision, "WARN")

    def test_no_drift_when_fingerprints_match(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": [], "base_schema_fingerprint": "fp-same"}],
            [_relation("raw.orders", [], schema_fingerprint="fp-same")])
        self.assertNotIn("production.schema_drift", self._codes(result))

    # case 10
    def test_removed_column_still_in_production_warns(self):
        result = self._decide(
            [{"relation_name": "raw.orders", "dependency_kind": "external",
              "columns": [], "removed_columns": ["legacy_total"]}],
            [_relation("raw.orders", [_column("legacy_total", "numeric")])])
        self.assertIn("column.removed_still_in_production", self._codes(result))


class ExplanationTests(unittest.TestCase):
    def test_findings_are_separated_by_evidence_category(self):
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=_snapshot(
                [_relation("raw.orders", [_column("discount_amount", "numeric",
                                                  null_rate=0.82)])]),
            enforcement_mode="enforce", code_health=100,
            code_findings=[{"code": "sql.refund_removed", "severity": "warn",
                            "category": "code", "message": "refund subtraction removed"}])
        payload = result.as_dict()
        self.assertTrue(payload["code_findings"])
        self.assertTrue(payload["production_metadata_findings"])
        self.assertEqual(payload["code_findings"][0]["code"], "sql.refund_removed")

    def test_which_evidence_states_were_available_is_recorded(self):
        with_production = evaluate_metadata_decision(
            plan=_targets(),
            snapshot=_snapshot([_relation("raw.orders",
                                          [_column("discount_amount")])]),
            enforcement_mode="enforce", code_health=100)
        without = evaluate_metadata_decision(
            plan=_targets(), snapshot=None, enforcement_mode="enforce",
            code_health=100)
        self.assertTrue(with_production.evidence_states_available["production"])
        self.assertFalse(without.evidence_states_available["production"])
        for result in (with_production, without):
            self.assertTrue(result.evidence_states_available["base_code"])
            self.assertTrue(result.evidence_states_available["head_code"])

    def test_policy_version_and_hash_are_recorded(self):
        result = evaluate_metadata_decision(
            plan=_targets(), snapshot=None, enforcement_mode="enforce",
            code_health=100)
        self.assertTrue(result.policy_version)
        self.assertEqual(len(result.policy_hash), 64)

    def test_evidence_policy_runs_on_a_fully_successful_review(self):
        """Regression for the Phase 0 finding: the policy engine used to run
        only on the missing-manifest path."""
        result = evaluate_metadata_decision(
            plan=_targets(),
            snapshot=_snapshot([_relation("raw.orders",
                                          [_column("discount_amount", "numeric",
                                                   null_rate=0.0)])]),
            enforcement_mode="enforce", code_health=100)
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(result.coverage, "COMPLETE")
        self.assertIn("production_metadata", result.evidence)
        self.assertEqual(result.evidence["production_metadata"], "EVALUATED")
        self.assertTrue(result.policy_hash)


class PlannerIntegrationTests(unittest.TestCase):
    def test_plan_from_real_manifests_drives_the_decision(self):
        plan = _plan()
        result = evaluate_metadata_decision(
            plan=plan, snapshot=None, enforcement_mode="enforce", code_health=100)
        self.assertTrue(plan.as_dict()["metadata_required"])
        self.assertIsNone(result.decision)
        self.assertEqual(result.lifecycle_state, "WAITING_FOR_METADATA")

    def test_plan_is_bounded_to_relevant_relations(self):
        plan = _plan().as_dict()
        names = {t["relation_name"] for t in plan["targets"]}
        self.assertIn("raw.orders", names)
        self.assertLessEqual(len(names), 4)


class CollectorToDecisionChainTests(unittest.TestCase):
    """The real collector output, through the real API parser, to a decision.

    Every layer here was individually 'correct' while the chain was not: the
    collector reported the absent column, the parser dropped the flag, and the
    engine's `column is None` test therefore never fired. Only an end-to-end
    assertion catches that class of loss, so this reconstructs the exact shapes
    rather than hand-writing a snapshot.
    """

    SIGNALS = frozenset({"relation_exists", "column_exists", "data_type",
                         "is_nullable", "row_count", "null_rate",
                         "schema_fingerprint"})

    def _collect(self, catalog, wanted, aggregates):
        from agent.collector.warehouse import PostgresMetadataReader

        reader = PostgresMetadataReader.__new__(PostgresMetadataReader)
        return PostgresMetadataReader._assemble(
            reader, schema="raw", table="orders", relation_name="raw.orders",
            catalog=catalog, present={c["column_name"]: c for c in catalog},
            wanted_columns=wanted, aggregates=aggregates, signals=self.SIGNALS)

    def _decide(self, relation, mode="enforce"):
        from agent.api.collector_routes import _parse_relations

        return evaluate_metadata_decision(
            plan={"metadata_required": True,
                  "targets": [{"relation_name": "raw.orders",
                               "dependency_kind": "external",
                               "columns": ["order_id", "discount_amount"],
                               "criticality": "standard"}]},
            snapshot=_snapshot(_parse_relations({"relations": [relation]})),
            enforcement_mode=mode, code_health=100)

    def test_dropped_production_column_reaches_the_decision_as_a_block(self):
        catalog = [{"column_name": "order_id", "data_type": "bigint",
                    "is_nullable": False, "ordinal_position": 1}]
        relation = self._collect(catalog, ["order_id", "discount_amount"],
                                 {"row_count": 650, "nulls__order_id": 0})

        result = self._decide(relation)
        self.assertEqual(result.decision, "BLOCK")
        codes = {f.code for f in result.findings}
        self.assertIn("column.missing_in_production", codes)

        finding = next(f for f in result.findings
                       if f.code == "column.missing_in_production")
        self.assertEqual(finding.relation, "raw.orders")
        self.assertEqual(finding.column, "discount_amount")

    def test_intact_production_column_still_allows(self):
        catalog = [
            {"column_name": "order_id", "data_type": "bigint",
             "is_nullable": False, "ordinal_position": 1},
            {"column_name": "discount_amount", "data_type": "numeric",
             "is_nullable": True, "ordinal_position": 2},
        ]
        relation = self._collect(
            catalog, ["order_id", "discount_amount"],
            {"row_count": 650, "nulls__order_id": 0, "nulls__discount_amount": 0})

        result = self._decide(relation)
        self.assertEqual(result.decision, "ALLOW")
        self.assertNotIn("column.missing_in_production",
                         {f.code for f in result.findings})

    def test_parser_accepts_both_spellings_of_the_flag(self):
        from agent.api.collector_routes import _parse_relations

        parsed = _parse_relations({"relations": [{
            "relation_name": "raw.orders",
            "columns": [
                {"column_name": "a", "exists": False},
                {"column_name": "b", "exists_in_production": False},
                {"column_name": "c"},
            ],
        }]})
        by_name = {c["column_name"]: c for c in parsed[0]["columns"]}
        self.assertFalse(by_name["a"]["exists_in_production"])
        self.assertFalse(by_name["b"]["exists_in_production"])
        self.assertTrue(by_name["c"]["exists_in_production"])


if __name__ == "__main__":
    unittest.main()
