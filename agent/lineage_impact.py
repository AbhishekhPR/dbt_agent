from __future__ import annotations

import hashlib
import json
import uuid


class ManifestBindingError(ValueError):
    pass


def build_lineage_record(*, model, upstream_models, downstream_models, columns, kpis, manifest_hash, expected_commit, manifest_commit, deployment_id=None):
    if expected_commit != manifest_commit:
        raise ManifestBindingError("Manifest hash is not bound to the reviewed commit")
    column_complete = bool(columns) and all(values for values in columns.values())
    completeness = {"model": "complete" if upstream_models is not None and downstream_models is not None else "incomplete", "column": "complete" if column_complete else "incomplete", "kpi": "complete" if kpis is not None else "incomplete"}
    return {"model": model, "upstream_models": list(upstream_models or []), "downstream_models": list(downstream_models or []), "columns": columns or {}, "affected_kpis": list(kpis or []), "manifest_hash": manifest_hash, "commit_sha": expected_commit, "deployment_id": deployment_id, "completeness": completeness, "claims_exhaustive_impact": completeness["column"] == "complete" and completeness["model"] == "complete"}


def persist_lineage(store, organization_id, repository_id, environment, record):
    store._tenant(organization_id, repository_id, environment)
    lineage_id = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    store.connection.execute("INSERT OR REPLACE INTO lineage_records VALUES (?, ?, ?, ?, ?, ?)", (lineage_id, organization_id, repository_id, environment, record["model"], json.dumps(record, sort_keys=True)))
    store.connection.commit()
    return {"lineage_id": lineage_id, **record}
