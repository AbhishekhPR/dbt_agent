import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.decision_engine import DeploymentDecision
from agent.deployment_outcomes import (
    DeploymentOutcome,
    DeploymentOutcomeStore,
    analyze_outcome_history,
)
from agent.signals import Severity


class DeploymentOutcomeTests(unittest.TestCase):
    def test_save_and_load_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deployment_outcomes.json"
            store = DeploymentOutcomeStore(path)
            outcome = _outcome("out-1", "deploy-1")

            store.save_outcome(outcome)

            self.assertTrue(path.exists())
            self.assertEqual(store.list_outcomes(), [outcome])

    def test_missing_outcome_file_is_handled_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentOutcomeStore(Path(tmp) / "missing.json")

            self.assertEqual(store.list_outcomes(), [])
            self.assertEqual(store.list_by_deployment("deploy-1"), [])
            self.assertIsNone(store.latest_for_deployment("deploy-1"))
            self.assertEqual(store.summarize_outcomes()["total_outcomes"], 0)

    def test_malformed_outcome_file_is_handled_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deployment_outcomes.json"
            path.write_text("{not-json", encoding="utf-8")
            store = DeploymentOutcomeStore(path)

            self.assertEqual(store.list_outcomes(), [])
            self.assertIsNone(store.latest_for_deployment("deploy-1"))

    def test_latest_for_deployment_returns_most_recent_saved_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentOutcomeStore(Path(tmp) / "deployment_outcomes.json")
            first = _outcome("out-1", "deploy-1", created_at="2026-07-01T00:00:00+00:00")
            second = _outcome("out-2", "deploy-1", created_at="2026-07-02T00:00:00+00:00")
            other = _outcome("out-3", "deploy-2", created_at="2026-07-03T00:00:00+00:00")

            store.save_outcome(first)
            store.save_outcome(other)
            store.save_outcome(second)

            self.assertEqual(store.latest_for_deployment("deploy-1"), second)

    def test_list_by_deployment_returns_matching_outcomes_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentOutcomeStore(Path(tmp) / "deployment_outcomes.json")
            first = _outcome("out-1", "deploy-1")
            second = _outcome("out-2", "deploy-2")
            third = _outcome("out-3", "deploy-1")

            store.save_outcome(first)
            store.save_outcome(second)
            store.save_outcome(third)

            self.assertEqual(store.list_by_deployment("deploy-1"), [first, third])

    def test_summarize_outcomes_counts_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentOutcomeStore(Path(tmp) / "deployment_outcomes.json")
            outcomes = [
                _outcome("out-1", "deploy-1", decision=DeploymentDecision.BLOCK, outcome="false_positive"),
                _outcome("out-2", "deploy-2", decision=DeploymentDecision.ALLOW, outcome="incident_occurred"),
                _outcome("out-3", "deploy-3", decision=DeploymentDecision.WARN, outcome="incident_occurred"),
                _outcome("out-4", "deploy-4", decision=DeploymentDecision.BLOCK, outcome="incident_occurred"),
                _outcome("out-5", "deploy-5", outcome="accepted_risk"),
                _outcome("out-6", "deploy-6", outcome="reverted"),
                _outcome("out-7", "deploy-7", outcome="blocked"),
                _outcome("out-8", "deploy-8", outcome="manually_approved"),
            ]
            for outcome in outcomes:
                store.save_outcome(outcome)

            summary = store.summarize_outcomes()

            self.assertEqual(summary["total_outcomes"], 8)
            self.assertEqual(summary["false_positives"], 1)
            self.assertEqual(summary["incidents_after_allow"], 1)
            self.assertEqual(summary["incidents_after_warn"], 1)
            self.assertEqual(summary["incidents_after_block"], 1)
            self.assertEqual(summary["accepted_risks"], 1)
            self.assertEqual(summary["reverted_deployments"], 1)
            self.assertEqual(summary["blocked_deployments"], 1)
            self.assertEqual(summary["manually_approved_deployments"], 1)

    def test_json_serialization_works(self):
        outcome = _outcome(
            "out-1",
            "deploy-1",
            decision=DeploymentDecision.WARN,
            metadata={"risk": {"owner": "analytics"}},
        )

        payload = outcome.to_dict()
        restored = DeploymentOutcome.from_dict(json.loads(json.dumps(payload)))

        self.assertEqual(payload["decision"], "WARN")
        self.assertEqual(restored, outcome)

    def test_no_mutation_of_input_metadata(self):
        metadata = {"risk": {"owner": "analytics"}}
        original = copy.deepcopy(metadata)
        outcome = _outcome("out-1", "deploy-1", metadata=metadata)

        outcome.metadata["risk"]["owner"] = "finance"
        serialized = outcome.to_dict()
        serialized["metadata"]["risk"]["owner"] = "product"

        self.assertEqual(metadata, original)

    def test_saved_outcomes_are_not_mutated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentOutcomeStore(Path(tmp) / "deployment_outcomes.json")
            outcome = _outcome("out-1", "deploy-1")
            original = copy.deepcopy(outcome)

            store.save_outcome(outcome)

            self.assertEqual(outcome, original)

    def test_analyze_outcome_history_returns_low_no_op_for_empty_outcomes(self):
        result = analyze_outcome_history([])

        self.assertEqual(result.severity, Severity.LOW)
        self.assertEqual(result.confidence, 0)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.reasons, [])
        self.assertEqual(result.metadata["total_outcomes"], 0)

    def test_false_positive_after_block_creates_context_signal(self):
        result = analyze_outcome_history([
            _outcome("out-1", "deploy-1", decision=DeploymentDecision.BLOCK, outcome="false_positive")
        ])

        self.assertEqual(result.severity, Severity.LOW)
        self.assertEqual(result.score, 0)
        self.assertIn(
            "Previous BLOCK decisions were marked false positive",
            result.reasons,
        )

    def test_incident_after_allow_or_warn_creates_risk_signal(self):
        result = analyze_outcome_history([
            _outcome("out-1", "deploy-1", decision=DeploymentDecision.ALLOW, outcome="incident_occurred"),
            _outcome("out-2", "deploy-2", decision=DeploymentDecision.WARN, outcome="incident_occurred"),
        ])

        self.assertEqual(result.severity, Severity.HIGH)
        self.assertEqual(result.score, -20)
        self.assertGreater(result.confidence, 70)
        self.assertIn(
            "Previous allowed or warned deployments were followed by incidents",
            result.reasons,
        )

    def test_reverted_after_allow_or_warn_creates_risk_signal(self):
        result = analyze_outcome_history([
            _outcome("out-1", "deploy-1", decision=DeploymentDecision.WARN, outcome="reverted")
        ])

        self.assertEqual(result.severity, Severity.MEDIUM)
        self.assertEqual(result.score, -10)
        self.assertIn(
            "Previous allowed or warned deployments were reverted",
            result.reasons,
        )

    def test_accepted_risk_appears_as_context_but_does_not_reduce_health(self):
        result = analyze_outcome_history([
            _outcome("out-1", "deploy-1", decision=DeploymentDecision.WARN, outcome="accepted_risk")
        ])

        self.assertEqual(result.severity, Severity.LOW)
        self.assertEqual(result.score, 0)
        self.assertIn("Similar risk was previously accepted", result.reasons)

    def test_fixed_before_merge_after_block_supports_block(self):
        result = analyze_outcome_history([
            _outcome("out-1", "deploy-1", decision=DeploymentDecision.BLOCK, outcome="fixed_before_merge")
        ])

        self.assertEqual(result.severity, Severity.MEDIUM)
        self.assertEqual(result.score, -10)
        self.assertIn("Previous block led to fix before merge", result.reasons)

    def test_analyze_outcome_history_does_not_mutate_outcomes(self):
        outcomes = [
            _outcome("out-1", "deploy-1", decision=DeploymentDecision.ALLOW, outcome="incident_occurred")
        ]
        original = copy.deepcopy(outcomes)

        analyze_outcome_history(outcomes, deployment_id="deploy-new", changed_models=["stg_orders"])

        self.assertEqual(outcomes, original)


def _outcome(
    outcome_id,
    deployment_id,
    *,
    snapshot_id="snap-1",
    decision=DeploymentDecision.ALLOW,
    outcome="merged",
    created_at="2026-07-02T00:00:00+00:00",
    notes="Reviewed by engineering.",
    metadata=None,
):
    return DeploymentOutcome(
        outcome_id=outcome_id,
        deployment_id=deployment_id,
        snapshot_id=snapshot_id,
        decision=decision,
        outcome=outcome,
        created_at=created_at,
        notes=notes,
        metadata=dict(metadata or {"source": "unit-test"}),
    )


if __name__ == "__main__":
    unittest.main()
