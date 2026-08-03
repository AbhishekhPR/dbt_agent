import json
import re
import sqlite3
import unittest


class LocalLifecycleE2ETests(unittest.TestCase):
    def run_scenario(self):
        from agent.cardinality_collapse import evaluate_cardinality_collapse
        from agent.deployment_events import DeploymentEventProcessor
        from agent.delivery_journal import DeliveryJournal
        from agent.rca_engine import build_rca
        from agent.sqlite_lifecycle_store import SQLiteLifecycleStore

        connection = sqlite3.connect(":memory:")
        store = SQLiteLifecycleStore(connection); store.ensure_schema(); store.ensure_tenant("org", "repo", "prod")
        processor = DeploymentEventProcessor(store)
        def event(event_id, event_type):
            return processor.process({"event_id": event_id, "event_type": event_type, "organization_id": "org", "repository_id": "repo", "environment": "prod", "deployment_id": "dep", "payload": {"merge_sha": "sha"}})
        for index, state in enumerate(["reviewed", "approved", "deployment_started", "deployment_succeeded", "post_deployment_monitoring"], 1): event(f"e{index}", state)
        observation = evaluate_cardinality_collapse({"model_identity": "mart", "declared_grain": ["id"], "key_columns": ["id"], "current_distinct_key_count": 10, "previous_distinct_key_count": 100, "current_row_count": 100, "previous_row_count": 100, "historical_baseline_window": [100, 100], "sample_size": 100, "deployment_id": "dep"})
        rca = build_rca(anomaly={"incident_id": "inc", "model": "mart", "detected_at": 2, "kpis": ["kpi"]}, deployments=[{"deployment_id": "dep", "merge_time": 1, "models": ["mart"]}], sql_findings=[{"finding_type": "MODEL_GRAIN_CHANGED", "description": "grain changed", "model": "mart"}], lineage={"mart": {"downstream_models": ["downstream"], "completeness": {"column": "complete"}}})
        journal = DeliveryJournal(connection); journal.ensure_schema(); delivery = journal.record("org", "repo", "prod", channel="github", event_key="inc", payload={"rca": rca["primary_root_cause"], "observation": observation["status"]})
        event("e6", "post_deployment_anomaly"); event("e7", "incident_open"); event("e8", "incident_resolved")
        return {"deployment": store.transitions("org", "repo", "prod", "dep"), "observation": observation, "rca": rca, "delivery": delivery}

    def test_lifecycle_runs_twice_with_normalized_evidence_equal(self):
        first = self.run_scenario(); second = self.run_scenario()
        self.assertEqual(self.normalize(first), self.normalize(second))
        self.assertEqual(first["observation"]["status"], "CRITICAL")
        self.assertEqual(first["rca"]["primary_root_cause"], "grain changed")
        self.assertFalse(first["delivery"]["duplicate"])

    @staticmethod
    def normalize(value):
        text = json.dumps(value, sort_keys=True)
        text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "UUID", text)
        text = re.sub(r"\d{4}-\d{2}-\d{2}T[^\"]+", "TIMESTAMP", text)
        return json.loads(text)


if __name__ == "__main__":
    unittest.main()
