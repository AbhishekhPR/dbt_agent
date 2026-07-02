import copy
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class DeploymentHistoryStore:
    def __init__(self, path):
        self.path = Path(path)

    def save_snapshot(self, snapshot) -> None:
        snapshot_dict = _snapshot_dict(snapshot)
        snapshot_id = snapshot_dict.get("snapshot_id")
        if not snapshot_id:
            raise ValueError("Snapshot must include a snapshot_id.")

        snapshots = [
            existing
            for existing in self.list_snapshots()
            if existing.get("snapshot_id") != snapshot_id
        ]
        snapshots.append(snapshot_dict)
        self._write_snapshots(snapshots)

    def load_snapshot(self, snapshot_id):
        for snapshot in self.list_snapshots():
            if snapshot.get("snapshot_id") == snapshot_id:
                return copy.deepcopy(snapshot)
        return None

    def load_latest_snapshot(self):
        snapshots = self.list_snapshots()
        if not snapshots:
            return None
        return copy.deepcopy(snapshots[-1])

    def list_snapshots(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._read_snapshots())

    def _read_snapshots(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return []
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return []

        snapshots = payload.get("snapshots") if isinstance(payload, dict) else payload
        if not isinstance(snapshots, list):
            return []
        return [
            copy.deepcopy(snapshot)
            for snapshot in snapshots
            if isinstance(snapshot, dict) and snapshot.get("snapshot_id")
        ]

    def _write_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"snapshots": [_serializable(snapshot) for snapshot in snapshots]}
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


def _snapshot_dict(snapshot) -> dict[str, Any]:
    if hasattr(snapshot, "to_dict"):
        return copy.deepcopy(snapshot.to_dict())
    if is_dataclass(snapshot):
        return _serializable(snapshot)
    return _serializable(copy.deepcopy(dict(snapshot or {})))


def _serializable(value):
    if is_dataclass(value):
        return _serializable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _serializable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value
