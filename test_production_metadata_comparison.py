"""The production metadata comparison engine and its API projection.

These run without PostgreSQL: ``build_comparison`` is pure, and the projection
is a pure function of a stored document. Baseline SELECTION, persistence and
lifecycle binding need a real database and live in
test_production_metadata_comparison_postgres.py.

What these are really defending is the difference between four sentences:

    "we did not measure that"
    "we measured it and it is gone"
    "we measured it and nothing changed"
    "we never ran the comparison"

Every one of them has a distinct representation, and none of them is zero.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agent.api.routes import _metadata_comparison_view
from agent.metadata_evidence.production_comparison import (
    STATUS_EVALUATED,
    STATUS_NO_BASELINE,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    build_comparison,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def column(name, **fields):
    base = {"column_name": name, "collection_status": "COLLECTED",
            "exists_in_production": True}
    base.update(fields)
    return base


def relation(name="orders", *, index=0, model="model.p.orders", columns=(), **fields):
    base = {"relation_index": index, "model_unique_id": model,
            "relation_database": "warehouse", "relation_schema": "analytics",
            "relation_name": name, "collection_status": "COLLECTED",
            "exists_in_production": True, "columns": list(columns)}
    base.update(fields)
    return base


def snapshot(snapshot_id, relations, *, observed_at=T0, completeness="COMPLETE"):
    return {"snapshot_id": snapshot_id, "environment": "production",
            "completeness": completeness, "observed_at": observed_at,
            "relations": list(relations)}


def kinds(result):
    return [c["kind"] for c in result["changes"]]


def only(result, kind):
    matches = [c for c in result["changes"] if c["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {kinds(result)}"
    return matches[0]


class NoBaselineTests(unittest.TestCase):
    def test_absent_baseline_is_no_baseline_not_zero(self):
        result = build_comparison(None, snapshot("s2", [relation(row_count=800)]))
        self.assertEqual(result["status"], STATUS_NO_BASELINE)
        self.assertEqual(result["changes"], [])
        self.assertIsNone(result["baseline_snapshot_id"])
        self.assertEqual(result["current_snapshot_id"], "s2")

    def test_no_baseline_invents_no_before_values(self):
        """The failure mode this replaces: comparing 800 against an imagined 0."""
        result = build_comparison(None, snapshot("s2", [relation(row_count=800)]))
        self.assertNotIn("coverage", result)
        self.assertEqual([], [c for c in result["changes"]])

    def test_absent_current_is_unavailable(self):
        result = build_comparison(None, None)
        self.assertEqual(result["status"], STATUS_UNAVAILABLE)
        self.assertEqual(result["changes"], [])


class StructuralChangeTests(unittest.TestCase):
    def test_relation_present_then_absent(self):
        before = snapshot("s1", [relation(row_count=1000)])
        after = snapshot("s2", [relation(exists_in_production=False, columns=[])],
                         observed_at=T1)
        change = only(build_comparison(before, after), "relation_availability_changed")
        self.assertEqual(change["signal"], "relation_exists")
        self.assertIs(change["before"], True)
        self.assertIs(change["after"], False)
        self.assertEqual(change["relation"], "orders")
        self.assertIsNone(change["column"])

    def test_a_vanished_relation_reports_no_behavioural_deltas(self):
        """Its metrics are unmeasurable, not zero."""
        before = snapshot("s1", [relation(row_count=1000, schema_fingerprint="f1")])
        after = snapshot("s2", [relation(exists_in_production=False)], observed_at=T1)
        self.assertEqual(kinds(build_comparison(before, after)),
                         ["relation_availability_changed"])

    def test_column_present_then_absent(self):
        before = snapshot("s1", [relation(columns=[column("customer_id")])])
        after = snapshot("s2", [relation(columns=[
            column("customer_id", exists_in_production=False)])], observed_at=T1)
        change = only(build_comparison(before, after), "column_availability_changed")
        self.assertEqual(change["signal"], "column_exists")
        self.assertEqual(change["column"], "customer_id")
        self.assertIs(change["before"], True)
        self.assertIs(change["after"], False)

    def test_column_type_changed(self):
        before = snapshot("s1", [relation(columns=[
            column("customer_id", data_type="BIGINT")])])
        after = snapshot("s2", [relation(columns=[
            column("customer_id", data_type="VARCHAR")])], observed_at=T1)
        change = only(build_comparison(before, after), "column_type_changed")
        self.assertEqual(change["signal"], "data_type")
        self.assertEqual(change["before"], "BIGINT")
        self.assertEqual(change["after"], "VARCHAR")

    def test_nullable_changed(self):
        before = snapshot("s1", [relation(columns=[
            column("customer_id", is_nullable=False)])])
        after = snapshot("s2", [relation(columns=[
            column("customer_id", is_nullable=True)])], observed_at=T1)
        change = only(build_comparison(before, after), "column_nullability_changed")
        self.assertEqual(change["signal"], "nullable")
        self.assertIs(change["before"], False)
        self.assertIs(change["after"], True)

    def test_schema_fingerprint_changed(self):
        before = snapshot("s1", [relation(schema_fingerprint="abc")])
        after = snapshot("s2", [relation(schema_fingerprint="def")], observed_at=T1)
        change = only(build_comparison(before, after), "schema_fingerprint_changed")
        self.assertEqual(change["signal"], "schema_fingerprint")
        self.assertEqual((change["before"], change["after"]), ("abc", "def"))


class NumericSemanticsTests(unittest.TestCase):
    def test_row_count_decrease_carries_absolute_and_relative_delta(self):
        before = snapshot("s1", [relation(row_count=51230)])
        after = snapshot("s2", [relation(row_count=48901)], observed_at=T1)
        change = only(build_comparison(before, after), "row_count_changed")
        self.assertEqual(change["before"], 51230)
        self.assertEqual(change["after"], 48901)
        self.assertEqual(change["absolute_delta"], -2329)
        self.assertEqual(change["relative_delta"], -0.04546)

    def test_row_count_increase(self):
        before = snapshot("s1", [relation(row_count=100)])
        after = snapshot("s2", [relation(row_count=150)], observed_at=T1)
        change = only(build_comparison(before, after), "row_count_changed")
        self.assertEqual(change["absolute_delta"], 50)
        self.assertEqual(change["relative_delta"], 0.5)

    def test_zero_baseline_omits_relative_delta_rather_than_inventing_one(self):
        before = snapshot("s1", [relation(row_count=0)])
        after = snapshot("s2", [relation(row_count=500)], observed_at=T1)
        change = only(build_comparison(before, after), "row_count_changed")
        self.assertEqual(change["absolute_delta"], 500)
        self.assertNotIn("relative_delta", change)

    def test_null_rate_change_is_percentage_points_not_percent(self):
        before = snapshot("s1", [relation(columns=[column("email", null_rate=0.012)])])
        after = snapshot("s2", [relation(columns=[column("email", null_rate=0.148)])],
                         observed_at=T1)
        change = only(build_comparison(before, after), "null_rate_changed")
        self.assertEqual(change["before"], 0.012)
        self.assertEqual(change["after"], 0.148)
        # 1.2% -> 14.8% is +13.6 POINTS. As percent it would be +1133%, which
        # is a different statement and not the one this field makes.
        self.assertEqual(change["percentage_point_delta"], 13.6)
        self.assertNotIn("relative_delta", change)
        self.assertNotIn("absolute_delta", change)

    def test_duplicate_rate_change_is_percentage_points(self):
        before = snapshot("s1", [relation(columns=[
            column("order_id", duplicate_rate=0.0)])])
        after = snapshot("s2", [relation(columns=[
            column("order_id", duplicate_rate=0.25)])], observed_at=T1)
        change = only(build_comparison(before, after), "duplicate_rate_changed")
        self.assertEqual(change["percentage_point_delta"], 25.0)

    def test_distinct_count_change(self):
        before = snapshot("s1", [relation(columns=[
            column("customer_id", distinct_count=4000)])])
        after = snapshot("s2", [relation(columns=[
            column("customer_id", distinct_count=3000)])], observed_at=T1)
        change = only(build_comparison(before, after), "distinct_count_changed")
        self.assertEqual(change["absolute_delta"], -1000)
        self.assertEqual(change["relative_delta"], -0.25)

    def test_cardinality_is_a_ratio_compared_in_percentage_points(self):
        """distinct_count / row_count, so it is a rate, not a count."""
        before = snapshot("s1", [relation(columns=[column("id", cardinality=0.37)])])
        after = snapshot("s2", [relation(columns=[column("id", cardinality=0.42)])],
                         observed_at=T1)
        change = only(build_comparison(before, after), "cardinality_changed")
        self.assertEqual(change["signal"], "cardinality")
        self.assertEqual(change["before"], 0.37)
        self.assertEqual(change["after"], 0.42)
        self.assertEqual(change["percentage_point_delta"], 5.0)
        self.assertNotIn("absolute_delta", change)
        self.assertNotIn("relative_delta", change)

    def test_a_fractional_cardinality_is_never_truncated_to_zero(self):
        """The defect 0012 closes: int(0.37) is 0, and 0 is a lie."""
        before = snapshot("s1", [relation(columns=[column("id", cardinality=0.37)])])
        after = snapshot("s2", [relation(columns=[column("id", cardinality=0.37)])],
                         observed_at=T1)
        # Equal fractional values are equal, not two zeros that happen to match.
        self.assertEqual(build_comparison(before, after)["changes"], [])

        moved = snapshot("s3", [relation(columns=[column("id", cardinality=0.44)])],
                         observed_at=T1)
        change = only(build_comparison(before, moved), "cardinality_changed")
        self.assertNotEqual(change["before"], 0)
        self.assertEqual(change["before"], 0.37)
        self.assertEqual(change["percentage_point_delta"], 7.0)

    def test_freshness_change_uses_the_persisted_lag(self):
        before = snapshot("s1", [relation(freshness_lag_seconds=300)])
        after = snapshot("s2", [relation(freshness_lag_seconds=7200)], observed_at=T1)
        change = only(build_comparison(before, after), "freshness_changed")
        self.assertEqual(change["signal"], "freshness")
        self.assertEqual((change["before"], change["after"]), (300, 7200))
        self.assertEqual(change["absolute_delta"], 6900)

    def test_freshness_timestamp_alone_is_not_reported_as_a_change(self):
        """Otherwise every collection would look like a freshness change."""
        before = snapshot("s1", [relation(freshness_timestamp=T0)])
        after = snapshot("s2", [relation(freshness_timestamp=T1)], observed_at=T1)
        self.assertEqual(kinds(build_comparison(before, after)), [])


class UnchangedTests(unittest.TestCase):
    def test_identical_observations_are_evaluated_with_no_changes(self):
        rows = [relation(row_count=1000, schema_fingerprint="f",
                         freshness_lag_seconds=60,
                         columns=[column("id", data_type="BIGINT", is_nullable=False,
                                         null_rate=0.01, duplicate_rate=0.0,
                                         distinct_count=1000, cardinality=1.0)])]
        result = build_comparison(snapshot("s1", rows), snapshot("s2", rows,
                                                                 observed_at=T1))
        self.assertEqual(result["status"], STATUS_EVALUATED)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["coverage"]["relations_compared"], 1)
        self.assertEqual(result["coverage"]["columns_compared"], 1)


class NotObservedTests(unittest.TestCase):
    """Not observed is never absent, false or zero."""

    def test_relation_only_in_baseline_is_not_reported_as_removed(self):
        before = snapshot("s1", [relation("orders", model="model.p.orders"),
                                 relation("payments", index=1,
                                          model="model.p.payments")])
        after = snapshot("s2", [relation("orders", model="model.p.orders")],
                         observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["status"], STATUS_EVALUATED)
        # It also does not count as a coverage gap: the gap concept describes
        # what THIS observation could not be compared against.
        self.assertEqual(result["coverage"]["relations_observed"], 1)

    def test_column_only_in_baseline_is_not_reported_as_dropped(self):
        before = snapshot("s1", [relation(columns=[column("id"), column("email")])])
        after = snapshot("s2", [relation(columns=[column("id")])], observed_at=T1)
        self.assertEqual(build_comparison(before, after)["changes"], [])

    def test_unobserved_relation_status_is_skipped_entirely(self):
        before = snapshot("s1", [relation(row_count=1000)])
        after = snapshot("s2", [relation(row_count=None,
                                         collection_status="SKIPPED")], observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["coverage"]["relations_observed"], 0)

    def test_null_metric_is_not_compared_as_zero(self):
        before = snapshot("s1", [relation(row_count=1000)])
        after = snapshot("s2", [relation(row_count=None)], observed_at=T1)
        self.assertEqual(build_comparison(before, after)["changes"], [])

    def test_not_observed_and_explicitly_absent_are_different_outcomes(self):
        before = snapshot("s1", [relation(columns=[column("customer_id")])])

        unobserved = snapshot("s2", [relation(columns=[
            column("customer_id", collection_status="SKIPPED")])], observed_at=T1)
        absent = snapshot("s3", [relation(columns=[
            column("customer_id", exists_in_production=False)])], observed_at=T1)

        self.assertEqual(build_comparison(before, unobserved)["changes"], [])
        self.assertEqual(kinds(build_comparison(before, absent)),
                         ["column_availability_changed"])

    def test_baseline_side_not_observed_yields_no_change(self):
        before = snapshot("s1", [relation(row_count=1000,
                                          collection_status="FAILED")])
        after = snapshot("s2", [relation(row_count=800)], observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["status"], STATUS_PARTIAL)


class PartialCoverageTests(unittest.TestCase):
    def test_relation_without_baseline_makes_the_result_partial(self):
        before = snapshot("s1", [relation("orders", model="model.p.orders")])
        after = snapshot("s2", [relation("orders", model="model.p.orders"),
                                relation("payments", index=1,
                                         model="model.p.payments")],
                         observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["status"], STATUS_PARTIAL)
        self.assertEqual(result["coverage"]["relations_observed"], 2)
        self.assertEqual(result["coverage"]["relations_compared"], 1)
        self.assertEqual(result["coverage"]["relations_without_baseline"],
                         [{"model": "model.p.payments", "relation": "payments"}])

    def test_column_without_baseline_makes_the_result_partial(self):
        before = snapshot("s1", [relation(columns=[column("id")])])
        after = snapshot("s2", [relation(columns=[column("id"), column("email")])],
                         observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["status"], STATUS_PARTIAL)
        self.assertEqual(result["coverage"]["columns_without_baseline"],
                         [{"model": "model.p.orders", "relation": "orders",
                           "column": "email"}])

    def test_a_partial_snapshot_makes_the_comparison_partial(self):
        rows = [relation(row_count=10)]
        result = build_comparison(snapshot("s1", rows),
                                  snapshot("s2", rows, observed_at=T1,
                                           completeness="PARTIAL"))
        self.assertEqual(result["status"], STATUS_PARTIAL)
        self.assertEqual(result["coverage"]["current_completeness"], "PARTIAL")

    def test_partial_still_reports_the_changes_it_could_compare(self):
        before = snapshot("s1", [relation("orders", model="model.p.orders",
                                          row_count=100)])
        after = snapshot("s2", [relation("orders", model="model.p.orders",
                                         row_count=90),
                                relation("payments", index=1,
                                         model="model.p.payments", row_count=5)],
                         observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["status"], STATUS_PARTIAL)
        change = only(result, "row_count_changed")
        self.assertEqual(change["relation"], "orders")


class IdentityMatchingTests(unittest.TestCase):
    def test_relations_match_on_model_identity_across_a_rename(self):
        before = snapshot("s1", [relation("orders_v1", model="model.p.orders",
                                          row_count=100)])
        after = snapshot("s2", [relation("orders_v2", model="model.p.orders",
                                         row_count=90)], observed_at=T1)
        change = only(build_comparison(before, after), "row_count_changed")
        self.assertEqual(change["relation"], "orders_v2")
        self.assertEqual(change["model"], "model.p.orders")

    def test_relations_match_on_warehouse_coordinates_without_a_model_id(self):
        before = snapshot("s1", [relation(model=None, row_count=100)])
        after = snapshot("s2", [relation(model=None, row_count=90)], observed_at=T1)
        change = only(build_comparison(before, after), "row_count_changed")
        self.assertIsNone(change["model"])

    def test_positional_index_is_not_identity(self):
        """Reordered relations still match; index is positional, name is not."""
        before = snapshot("s1", [relation("orders", index=0, model="model.p.orders",
                                          row_count=100),
                                 relation("payments", index=1,
                                          model="model.p.payments", row_count=7)])
        after = snapshot("s2", [relation("payments", index=0,
                                         model="model.p.payments", row_count=7),
                                relation("orders", index=1, model="model.p.orders",
                                         row_count=90)], observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual(result["status"], STATUS_EVALUATED)
        change = only(result, "row_count_changed")
        self.assertEqual(change["relation"], "orders")

    def test_output_ordering_is_deterministic(self):
        before = snapshot("s1", [relation("b_rel", model="m.b", row_count=10),
                                 relation("a_rel", index=1, model="m.a", row_count=10)])
        after = snapshot("s2", [relation("b_rel", model="m.b", row_count=11),
                                relation("a_rel", index=1, model="m.a", row_count=11)],
                         observed_at=T1)
        result = build_comparison(before, after)
        self.assertEqual([c["relation"] for c in result["changes"]],
                         ["a_rel", "b_rel"])


class ApiProjectionTests(unittest.TestCase):
    def test_null_stays_null(self):
        self.assertIsNone(_metadata_comparison_view(None))

    def test_all_four_states_survive_projection(self):
        for status in ("evaluated", "partial", "no_baseline", "unavailable"):
            with self.subTest(status=status):
                view = _metadata_comparison_view({"status": status, "changes": []})
                self.assertEqual(view["status"], status)

    def test_an_unrecognised_status_is_not_passed_through(self):
        self.assertIsNone(_metadata_comparison_view({"status": "clean"}))

    def test_snapshot_identities_and_timestamps_are_exposed(self):
        view = _metadata_comparison_view({
            "status": "evaluated", "baseline_snapshot_id": "snap-a",
            "current_snapshot_id": "snap-b", "baseline_observed_at": "2026-08-01",
            "current_observed_at": "2026-08-02", "changes": []})
        self.assertEqual(view["baseline_snapshot_id"], "snap-a")
        self.assertEqual(view["current_snapshot_id"], "snap-b")
        self.assertEqual(view["current_observed_at"], "2026-08-02")

    def test_unknown_change_kinds_are_dropped(self):
        view = _metadata_comparison_view({
            "status": "evaluated",
            "changes": [{"kind": "row_count_changed", "before": 1, "after": 2},
                        {"kind": "sample_rows_leaked", "rows": [["secret"]]}]})
        self.assertEqual(view["change_count"], 1)
        self.assertEqual(view["changes"][0]["kind"], "row_count_changed")

    def test_fields_outside_the_allowlist_never_reach_the_client(self):
        """A future engine field is invisible until someone publishes it."""
        view = _metadata_comparison_view({
            "status": "evaluated",
            "changes": [{"kind": "row_count_changed", "model": "m", "relation": "r",
                         "column": None, "signal": "row_count",
                         "before": 10, "after": 5, "absolute_delta": -5,
                         "min_value": "alice@example.com",
                         "collector_id": "col-1",
                         "sql": "select * from orders"}]})
        change = view["changes"][0]
        self.assertEqual(set(change), {"kind", "model", "relation", "column",
                                       "signal", "before", "after",
                                       "absolute_delta"})

    def test_no_raw_snapshot_internals_leak(self):
        view = _metadata_comparison_view({
            "status": "evaluated", "changes": [],
            "evidence_hash": "h", "provenance": {"dsn": "postgres://secret"},
            "collector_version": "1.2.3", "idempotency_key": "k",
            "relations": [{"min_value": "alice@example.com"}]})
        self.assertEqual(set(view), {"status", "baseline_snapshot_id",
                                     "current_snapshot_id", "baseline_observed_at",
                                     "current_observed_at", "changes",
                                     "change_count", "coverage"})

    def test_cardinality_projects_percentage_points_not_count_deltas(self):
        view = _metadata_comparison_view({
            "status": "evaluated",
            "changes": [{"kind": "cardinality_changed", "model": "m",
                         "relation": "r", "column": "id", "signal": "cardinality",
                         "before": 0.37, "after": 0.42,
                         "percentage_point_delta": 5.0}]})
        change = view["changes"][0]
        self.assertEqual(change["before"], 0.37)
        self.assertEqual(change["percentage_point_delta"], 5.0)
        self.assertNotIn("absolute_delta", change)
        self.assertNotIn("relative_delta", change)

    def test_percentage_point_delta_is_projected_and_relative_delta_is_not(self):
        view = _metadata_comparison_view({
            "status": "evaluated",
            "changes": [{"kind": "null_rate_changed", "before": 0.012,
                         "after": 0.148, "percentage_point_delta": 13.6,
                         "relative_delta": 11.33}]})
        change = view["changes"][0]
        self.assertEqual(change["percentage_point_delta"], 13.6)
        self.assertNotIn("relative_delta", change)

    def test_coverage_is_projected_for_partial(self):
        view = _metadata_comparison_view({
            "status": "partial", "changes": [],
            "coverage": {"relations_observed": 2, "relations_compared": 1,
                         "relations_without_baseline": [
                             {"model": "m.p", "relation": "payments",
                              "evidence_hash": "leak"}],
                         "columns_observed": 0, "columns_compared": 0,
                         "columns_without_baseline": [],
                         "baseline_completeness": "COMPLETE",
                         "current_completeness": "COMPLETE"}})
        self.assertEqual(view["coverage"]["relations_compared"], 1)
        self.assertEqual(view["coverage"]["relations_without_baseline"],
                         [{"model": "m.p", "relation": "payments"}])

    def test_every_engine_kind_is_projectable(self):
        """A kind the engine can emit but the API silently drops would make a
        real change invisible, which is worse than an ugly failure."""
        from agent.api.routes import _METADATA_CHANGE_FIELDS
        emitted = {
            "relation_availability_changed", "column_availability_changed",
            "column_type_changed", "column_nullability_changed",
            "schema_fingerprint_changed", "row_count_changed",
            "null_rate_changed", "duplicate_rate_changed",
            "distinct_count_changed", "cardinality_changed", "freshness_changed",
        }
        self.assertEqual(emitted, set(_METADATA_CHANGE_FIELDS))


class PolicySeparationTests(unittest.TestCase):
    def test_the_evidence_carries_no_severity_verdict_or_threshold(self):
        before = snapshot("s1", [relation(row_count=1000, columns=[
            column("email", null_rate=0.01)])])
        after = snapshot("s2", [relation(row_count=10, columns=[
            column("email", null_rate=0.99)])], observed_at=T1)
        result = build_comparison(before, after)
        forbidden = {"severity", "decision", "verdict", "finding", "threshold",
                     "health", "risk", "blocking"}
        for change in result["changes"]:
            self.assertEqual(forbidden & set(change), set())
        self.assertEqual(forbidden & set(result), set())


if __name__ == "__main__":
    unittest.main()
