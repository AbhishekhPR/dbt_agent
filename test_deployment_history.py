import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.decision_engine import DeploymentDecision
from agent.deployment_history import DeploymentHistoryStore, record_deployment_snapshot
from agent.deployment_snapshot import DeploymentSnapshot
from agent.incident import Incident
from agent.signals import Severity


class DeploymentHistoryStoreTests(unittest.TestCase):
    def test_saving_snapshot_creates_history_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            store = DeploymentHistoryStore(path)

            store.save_snapshot(_snapshot("snap-1", "deploy-1"))

            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["snapshots"][0]["snapshot_id"], "snap-1")

    def test_load_snapshot_returns_correct_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            first = _snapshot("snap-1", "deploy-1")
            second = _snapshot("snap-2", "deploy-2")
            store.save_snapshot(first)
            store.save_snapshot(second)

            loaded = store.load_snapshot("snap-1")

            self.assertEqual(loaded, first.to_dict())

    def test_load_latest_snapshot_returns_newest_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_snapshot("snap-1", "deploy-1"))
            newest = _snapshot("snap-2", "deploy-2")
            store.save_snapshot(newest)

            loaded = store.load_latest_snapshot()

            self.assertEqual(loaded, newest.to_dict())

    def test_list_snapshots_returns_all_snapshots_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            snapshots = [
                _snapshot("snap-1", "deploy-1"),
                _snapshot("snap-2", "deploy-2"),
                _snapshot("snap-3", "deploy-3"),
            ]
            for snapshot in snapshots:
                store.save_snapshot(snapshot)

            loaded = store.list_snapshots()

            self.assertEqual(
                [snapshot["snapshot_id"] for snapshot in loaded],
                ["snap-1", "snap-2", "snap-3"],
            )

    def test_saving_same_snapshot_id_replaces_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_snapshot("snap-1", "deploy-1", metadata={"version": 1}))
            replacement = _snapshot("snap-1", "deploy-1b", metadata={"version": 2})

            store.save_snapshot(replacement)

            loaded = store.list_snapshots()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["deployment_id"], "deploy-1b")
            self.assertEqual(loaded[0]["metadata"], {"version": 2})

    def test_missing_history_file_returns_empty_list_and_none_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "missing.json")

            self.assertEqual(store.list_snapshots(), [])
            self.assertIsNone(store.load_latest_snapshot())
            self.assertIsNone(store.load_snapshot("missing"))

    def test_empty_or_corrupt_history_file_is_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text("{not-json", encoding="utf-8")
            store = DeploymentHistoryStore(path)

            self.assertEqual(store.list_snapshots(), [])
            self.assertIsNone(store.load_latest_snapshot())

    def test_snapshots_are_not_mutated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            snapshot = _snapshot("snap-1", "deploy-1")
            original = copy.deepcopy(snapshot)

            store.save_snapshot(snapshot)

            self.assertEqual(snapshot, original)

    def test_stored_json_is_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            store = DeploymentHistoryStore(path)
            store.save_snapshot(_snapshot("snap-1", "deploy-1"))

            payload = json.loads(path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload)

            self.assertIsInstance(serialized, str)

    def test_record_deployment_snapshot_saves_current_snapshot_from_incident_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            current_snapshot = _snapshot_dict("snap-current", "deploy-current")
            incident = _incident(DeploymentDecision.ALLOW, current_snapshot=current_snapshot)

            saved_id = record_deployment_snapshot(history_store=store, incident=incident)

            self.assertEqual(saved_id, "snap-current")
            self.assertEqual(store.load_snapshot("snap-current"), current_snapshot)

    def test_record_deployment_snapshot_returns_saved_snapshot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")

            saved_id = record_deployment_snapshot(
                history_store=store,
                incident=_incident(
                    DeploymentDecision.WARN,
                    current_snapshot=_snapshot_dict("snap-warn", "deploy-warn"),
                ),
            )

            self.assertEqual(saved_id, "snap-warn")

    def test_record_deployment_snapshot_missing_current_snapshot_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")

            saved_id = record_deployment_snapshot(
                history_store=store,
                incident=_incident(DeploymentDecision.ALLOW),
            )

            self.assertIsNone(saved_id)
            self.assertEqual(store.list_snapshots(), [])

    def test_record_deployment_snapshot_block_decision_is_not_saved_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")

            saved_id = record_deployment_snapshot(
                history_store=store,
                incident=_incident(
                    DeploymentDecision.BLOCK,
                    current_snapshot=_snapshot_dict("snap-block", "deploy-block"),
                ),
            )

            self.assertIsNone(saved_id)
            self.assertEqual(store.list_snapshots(), [])

    def test_record_deployment_snapshot_block_decision_is_saved_when_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            current_snapshot = _snapshot_dict("snap-block", "deploy-block")

            saved_id = record_deployment_snapshot(
                history_store=store,
                incident=_incident(DeploymentDecision.BLOCK, current_snapshot=current_snapshot),
                allow_blocked=True,
            )

            self.assertEqual(saved_id, "snap-block")
            self.assertEqual(store.load_snapshot("snap-block"), current_snapshot)

    def test_record_deployment_snapshot_allow_decision_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")

            saved_id = record_deployment_snapshot(
                history_store=store,
                incident=_incident(
                    DeploymentDecision.ALLOW,
                    current_snapshot=_snapshot_dict("snap-allow", "deploy-allow"),
                ),
            )

            self.assertEqual(saved_id, "snap-allow")

    def test_record_deployment_snapshot_warn_decision_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")

            saved_id = record_deployment_snapshot(
                history_store=store,
                incident=_incident(
                    DeploymentDecision.WARN,
                    current_snapshot=_snapshot_dict("snap-warn", "deploy-warn"),
                ),
            )

            self.assertEqual(saved_id, "snap-warn")

    def test_record_deployment_snapshot_does_not_mutate_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            incident = _incident(
                DeploymentDecision.ALLOW,
                current_snapshot=_snapshot_dict("snap-current", "deploy-current"),
            )
            original = copy.deepcopy(incident)

            record_deployment_snapshot(history_store=store, incident=incident)

            self.assertEqual(incident, original)

    def test_record_deployment_snapshot_saved_snapshot_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            record_deployment_snapshot(
                history_store=store,
                incident=_incident(
                    DeploymentDecision.ALLOW,
                    current_snapshot=_snapshot_dict("snap-current", "deploy-current"),
                ),
            )

            serialized = json.dumps(store.load_snapshot("snap-current"))

            self.assertIsInstance(serialized, str)

    def test_record_deployment_snapshot_latest_snapshot_becomes_saved_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_snapshot("snap-old", "deploy-old"))
            current_snapshot = _snapshot_dict("snap-current", "deploy-current")

            record_deployment_snapshot(
                history_store=store,
                incident=_incident(DeploymentDecision.ALLOW, current_snapshot=current_snapshot),
            )

            self.assertEqual(store.load_latest_snapshot(), current_snapshot)


def _snapshot(snapshot_id, deployment_id, *, metadata=None):
    return DeploymentSnapshot(
        snapshot_id=snapshot_id,
        deployment_id=deployment_id,
        created_at="2026-07-02T00:00:00+00:00",
        changed_models=["stg_orders"],
        semantic_context={
            "discovered_kpis": [{"name": "Revenue"}],
            "knowledge_report": {"contracts": [{"kpi_name": "Revenue"}]},
        },
        decision=None,
        incident_summary=None,
        metadata=dict(metadata or {}),
    )


def _snapshot_dict(snapshot_id, deployment_id):
    return {
        "snapshot_id": snapshot_id,
        "deployment_id": deployment_id,
        "created_at": "2026-07-02T00:00:00+00:00",
        "changed_models": ["stg_orders"],
        "semantic_context": {
            "discovered_kpis": [{"name": "Revenue"}],
            "knowledge_report": {"contracts": [{"kpi_name": "Revenue"}]},
        },
        "decision": {"decision": "ALLOW", "health": 100},
        "incident_summary": {"incident_id": "INC-1", "decision": "ALLOW"},
        "metadata": {"source": "unit-test"},
    }


def _incident(decision, *, current_snapshot=None):
    metadata = {}
    if current_snapshot is not None:
        metadata["current_snapshot"] = copy.deepcopy(current_snapshot)
    return Incident(
        incident_id="INC-1",
        health=100 if decision == DeploymentDecision.ALLOW else 75,
        decision=decision,
        severity=Severity.LOW,
        confidence=90,
        root_cause="",
        recommendation="",
        metadata=metadata,
    )


if __name__ == "__main__":
    unittest.main()
