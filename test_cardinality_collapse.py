import unittest


def observation(**overrides):
    value = {
        "model_identity": "mart.customers",
        "declared_grain": ["customer_id"],
        "key_columns": ["customer_id"],
        "current_distinct_key_count": 100,
        "previous_distinct_key_count": 1000,
        "current_row_count": 1000,
        "previous_row_count": 1000,
        "historical_baseline_window": [1000, 980, 1020, 1010],
        "deployment_id": "dep-1",
        "pr_number": 42,
        "commit_sha": "abc",
        "downstream_models": ["mart.revenue"],
        "affected_kpis": ["customer_count"],
        "null_key_count": 0,
        "sample_size": 1000,
    }
    value.update(overrides)
    return value


class CardinalityCollapseTests(unittest.TestCase):
    def test_severe_key_collapse_is_critical_with_metrics_and_attribution(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation())
        self.assertEqual(result["status"], "CRITICAL")
        self.assertEqual(result["deployment_id"], "dep-1")
        self.assertEqual(result["metrics"]["distinct_key_ratio"], 0.1)
        self.assertEqual(result["affected_kpis"], ["customer_count"])

    def test_gradual_collapse_warns_not_critical(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(current_distinct_key_count=800))
        self.assertEqual(result["status"], "WARN")

    def test_row_drop_without_key_collapse_is_healthy(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(current_distinct_key_count=1000, current_row_count=100))
        self.assertEqual(result["status"], "HEALTHY")

    def test_stable_rows_with_key_collapse_is_detected(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(current_row_count=1000))
        self.assertIn(result["status"], {"CRITICAL", "WARN"})
        self.assertGreater(result["metrics"]["rows_per_key_ratio"], 1)

    def test_intentional_filter_contract_suppresses_alert(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(intentional_change={"type": "filtered_model"}))
        self.assertEqual(result["status"], "HEALTHY")
        self.assertTrue(result["intentional_change"])

    def test_declared_grain_change_is_not_collapse(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(declared_grain_changed=True))
        self.assertEqual(result["status"], "NOT EVALUATED")

    def test_insufficient_history_and_small_sample_are_not_evaluated(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        self.assertEqual(evaluate_cardinality_collapse(observation(historical_baseline_window=[]))["status"], "NOT EVALUATED")
        self.assertEqual(evaluate_cardinality_collapse(observation(sample_size=2))["status"], "NOT EVALUATED")

    def test_null_key_spike_is_reported(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(null_key_count=300))
        self.assertGreater(result["metrics"]["null_key_ratio"], 0.2)
        self.assertIn(result["status"], {"WARN", "CRITICAL"})

    def test_recovery_after_rollback_is_healthy(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(current_distinct_key_count=1000, rollback_observed=True))
        self.assertEqual(result["status"], "HEALTHY")
        self.assertTrue(result["rollback_observed"])

    def test_repeated_deployment_correlation_is_preserved(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse

        result = evaluate_cardinality_collapse(observation(repeated_deployment_ids=["dep-0", "dep-1"]))
        self.assertEqual(result["repeated_deployment_ids"], ["dep-0", "dep-1"])


if __name__ == "__main__":
    unittest.main()
