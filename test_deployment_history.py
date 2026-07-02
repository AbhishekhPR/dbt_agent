import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_snapshot import DeploymentSnapshot


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


if __name__ == "__main__":
    unittest.main()
