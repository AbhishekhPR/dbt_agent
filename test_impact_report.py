"""The canonical impact report: one renderer, deterministic bytes, and no
claim the persisted evidence does not support.
"""
from __future__ import annotations

import unittest

from agent.metadata_evidence.impact_report import (
    impact_report_filename,
    render_review_impact_report,
)


REVIEW = {
    "review_id": "gh-abc123",
    "repository": "AbhishekhPR/relium-e2e-dbt",
    "pull_number": 60,
    "environment": "production",
    "head_sha": "c716ba3af7e6cdfcc31f447e37b80666d38c2f2c",
    "base_sha": "63849d58f6323d31545d39fbbbb7a8d08983f4a3",
    "attempt": 2,
    "lifecycle_state": "DECISION_READY",
    "enforcement_mode": "shadow",
    "policy_hash": "ff85221588efbe12",
    "head_manifest_hash": "7b198c99d8f737e0",
}

ATTEMPT = {
    "attempt": 2, "decision": "WARN", "evidence_coverage": "COMPLETE",
    "health": 100, "trigger": "metadata_snapshot",
    "snapshot_id": "snap-a73fcb086bc84133bb2fa65d",
    "policy_version": "default-v1",
}

FINDINGS = [
    {"code": "relation.not_collected", "severity": "info",
     "relation": "main.int_customer_orders", "column": None,
     "message": "not part of the targeted collection request", "detail": {}},
    {"code": "column.high_null_rate", "severity": "warn",
     "relation": "main.stg_orders", "column": "customer_id",
     "message": "main.stg_orders.customer_id is 35% NULL in production.",
     "detail": {"null_rate": 0.35, "threshold": 0.2}},
]

SEMANTIC = {"status": "evaluated", "change_count": 1, "models": [{
    "model_name": "int_customer_orders", "status": "evaluated", "changes": [{
        "kind": "filter_changed", "scope": "where", "before_sql": None,
        "after_sql": "NOT status IS NULL",
        "model_unique_id": "model.relium_e2e_dbt.int_customer_orders"}]}]}


def _render(**overrides):
    kwargs = dict(review=REVIEW, attempt=ATTEMPT, findings=FINDINGS,
                  semantic=SEMANTIC)
    kwargs.update(overrides)
    return render_review_impact_report(**kwargs)


class Determinism(unittest.TestCase):
    def test_two_renders_are_byte_identical(self):
        self.assertEqual(_render(), _render())

    def test_no_generation_timestamp_leaks_in(self):
        text = _render()
        for marker in ("generated at", "Generated on", "exported"):
            self.assertNotIn(marker.lower(), text.lower())

    def test_ends_with_single_trailing_newline(self):
        text = _render()
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_findings_are_ordered_by_severity_not_input_order(self):
        text = _render()
        self.assertLess(text.index("column.high_null_rate"),
                        text.index("relation.not_collected"))


class Content(unittest.TestCase):
    def test_decision_and_identity_are_present(self):
        text = _render()
        self.assertIn("# Relium impact report — pull request #60", text)
        self.assertIn("**WARN**", text)
        self.assertIn("gh-abc123", text)
        self.assertIn("AbhishekhPR/relium-e2e-dbt", text)

    def test_semantic_change_is_rendered_truthfully(self):
        text = _render()
        self.assertIn("filter_changed", text)
        self.assertIn("NOT status IS NULL", text)
        # A model with no WHERE clause before must not be described as having
        # had one.
        self.assertIn("no filter", text)

    def test_finding_detail_carries_measured_value_and_threshold(self):
        text = _render()
        self.assertIn("0.35", text)
        self.assertIn("0.2", text)

    def test_direct_edges_render_as_directed_pairs(self):
        text = _render(change_plan={
            "changed_models": ["model.a.int_customer_orders"],
            "downstream_models": ["model.a.dim_customers"],
            "direct_edges": [{
                "source_model_unique_id": "model.a.int_customer_orders",
                "target_model_unique_id": "model.a.dim_customers"}]})
        self.assertIn("model.a.int_customer_orders", text)
        self.assertIn("→", text)
        self.assertIn("Direct edges", text)

    def test_legacy_plan_states_edges_were_not_recorded(self):
        text = _render(change_plan={
            "changed_models": ["int_customer_orders"],
            "downstream_models": ["model.a.dim_customers"],
            "direct_edges": None})
        self.assertIn("model.a.dim_customers", text)
        self.assertIn("not recorded at analysis time", text)
        self.assertNotIn("Direct edges", text)

    def test_absent_sections_are_omitted_not_invented(self):
        text = render_review_impact_report(review=REVIEW, attempt=ATTEMPT)
        self.assertNotIn("## Why Relium flagged it", text)
        self.assertNotIn("## What changed", text)
        self.assertNotIn("## Production metadata", text)
        self.assertNotIn("## Blast radius", text)

    def test_no_kpi_or_causal_language(self):
        text = _render(change_plan={
            "changed_models": ["model.a.x"], "downstream_models": [],
            "direct_edges": []}).lower()
        for banned in ("revenue", "kpi", "root cause", "exposure",
                       "dashboard impact"):
            self.assertNotIn(banned, text)
        # The report may SAY it does not traverse transitively; it must not
        # present transitive results.
        self.assertIn("does not traverse the graph transitively", text)


class NonAttribution(unittest.TestCase):
    """A metadata difference is an observation, never a causal claim."""

    COMPARISON = {
        "status": "evaluated",
        "baseline_snapshot_id": "snap-old", "current_snapshot_id": "snap-new",
        "changes": [
            {"kind": "row_count_changed", "signal": "row_count",
             "relation": "main.stg_orders", "column": None,
             "before": 500, "after": 20},
            {"kind": "schema_fingerprint_changed", "signal": "schema_fingerprint",
             "relation": "main.stg_orders", "column": None,
             "before": "fp-integrated-e2e",
             "after": "5daaf50988b870e8809a38ba58d29e9a"},
        ],
    }

    def test_comparison_is_framed_as_observation(self):
        text = _render(comparison=self.COMPARISON)
        self.assertIn("observations, not attributions", text)
        self.assertIn("does not claim the pull request caused them", text)

    def test_comparison_never_becomes_a_finding_heading(self):
        text = _render(comparison=self.COMPARISON)
        # It lives under Production metadata, not under the findings section.
        self.assertLess(text.index("## Why Relium flagged it"),
                        text.index("## Production metadata"))
        self.assertNotIn("### row_count_changed", text)

    def test_long_fingerprints_are_shortened_in_the_table(self):
        text = _render(comparison=self.COMPARISON)
        # Truncated to a recognisable prefix; the full 32-char value never
        # widens the table.
        self.assertIn("5daaf50988b8…", text)
        self.assertNotIn("5daaf50988b870e8809a38ba58d29e9a", text)

    def test_no_baseline_status_is_stated_plainly(self):
        text = _render(comparison={"status": "no_baseline", "changes": []})
        self.assertIn("no_baseline", text)


class Filename(unittest.TestCase):
    def test_filename_is_safe_and_deterministic(self):
        self.assertEqual(impact_report_filename("gh-abc123", 2),
                         "relium-impact-report-gh-abc123-attempt-2.md")

    def test_path_separators_cannot_survive(self):
        name = impact_report_filename("../../etc/passwd", 1)
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        self.assertNotIn("\\", name)


if __name__ == "__main__":
    unittest.main()
