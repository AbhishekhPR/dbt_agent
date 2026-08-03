from __future__ import annotations

import hashlib
import json
from pathlib import Path


def export_pilot_store(root: str | Path) -> dict:
    root = Path(root)
    files = []
    for path in sorted(root.rglob("*.json")):
        content = path.read_bytes()
        files.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(content).hexdigest(), "payload": json.loads(content.decode("utf-8"))})
    return {"schema_version": 1, "source": "filesystem-pilot", "files": files, "file_count": len(files)}


def reconcile_export(export: dict, imported_count: int) -> dict:
    expected = int(export.get("file_count", 0))
    return {"expected_count": expected, "imported_count": imported_count, "matched": expected == imported_count, "source": export.get("source"), "schema_version": export.get("schema_version")}
