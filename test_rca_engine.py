import unittest


class RCAEngineTests(unittest.TestCase):
    def test_refund_invariant_removal_is_primary_cause_with_evidence(self):
        from agent.rca_engine import build_rca

        result = build_rca(
            anomaly={"incident_id": "inc-1", "model": "mart_revenue", "kpis": ["revenue"], "detected_at": 20, "type": "revenue_spike"},
            deployments=[{"deployment_id": "dep-1", "merge_time": 10, "models": ["mart_revenue"]}],
            sql_findings=[{"finding_type": "INVARIANT_REMOVED", "description": "refund subtraction removed", "model": "mart_revenue"}],
            lineage={"mart_revenue": {"downstream_models": ["mart_exec"]}},
        )
        self.assertEqual(result["primary_root_cause"], "refund subtraction removed")
        self.assertEqual(result["confidence"]["classification"], "HIGH")
        self.assertTrue(result["evidence"])

    def test_no_relevant_deployment_is_unattributed(self):
        from agent.rca_engine import build_rca

        result = build_rca(anomaly={"incident_id": "inc-2", "model": "m", "detected_at": 20}, deployments=[], sql_findings=[], lineage={})
        self.assertEqual(result["primary_root_cause"], "UNATTRIBUTED")
        self.assertEqual(result["confidence"]["classification"], "LOW")
        self.assertIn("deployment", result["unevaluated_evidence"])

    def test_two_candidates_are_ranked_and_causality_not_overclaimed(self):
        from agent.rca_engine import build_rca

        result = build_rca(anomaly={"incident_id": "inc-3", "model": "m", "detected_at": 20}, deployments=[{"deployment_id": "a", "merge_time": 10, "models": ["m"]}, {"deployment_id": "b", "merge_time": 15, "models": ["m"]}], sql_findings=[{"finding_type": "GENERIC", "description": "implementation changed", "model": "m"}], lineage={})
        self.assertEqual(len(result["alternative_causes"]), 2)
        self.assertFalse(result["causality"]["proven"])

    def test_missing_optional_evidence_reduces_confidence(self):
        from agent.rca_engine import build_rca

        result = build_rca(anomaly={"incident_id": "inc-4", "model": "m", "detected_at": 20}, deployments=[{"deployment_id": "a", "merge_time": 10, "models": ["m"]}], sql_findings=[], lineage={"m": {"completeness": {"column": "incomplete"}}})
        self.assertEqual(result["confidence"]["classification"], "MEDIUM")
        self.assertTrue(result["unevaluated_evidence"])


if __name__ == "__main__":
    unittest.main()
