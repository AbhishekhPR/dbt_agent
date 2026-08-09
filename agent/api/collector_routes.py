"""Collector control and metadata snapshot handlers.

These are the endpoints a customer-side collector talks to. They live in their
own module so the /api route table stays readable, but they are registered on
the same Starlette application, behind the same service-token authentication
and the same tenant scoping as every other public route.

Two properties matter most here:

  * A snapshot is validated against everything it claims - tenant, repository,
    environment, review, attempt, request, base and head SHA, manifest hashes,
    expiry, freshness and completeness - BEFORE anything is written. A
    mismatch leaves no partial persistence.

  * Nothing a warehouse returns is trusted as payload. Raw rows, unbounded
    text and SQL are rejected at the boundary rather than sanitised later.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from agent.api.service import ConflictError, NotFoundError
from agent.api.validation import (
    ValidationError,
    optional_choice,
    optional_object,
    optional_str,
    pagination,
    require_idempotency_key,
    require_object,
    require_str,
    require_timestamp,
)
from agent.postgres_lifecycle_store import SnapshotConflict
from agent.metadata_evidence.decision import classify_freshness
from agent.metadata_evidence.review_lifecycle import (
    SnapshotRejected,
    new_snapshot_id,
    validate_and_bind_snapshot,
)

# Bounds. A collector that exceeds these is misconfigured or hostile; either
# way the evidence plane must not absorb it.
MAX_RELATIONS = 200
MAX_COLUMNS_PER_RELATION = 500
MAX_METRICS = 100
MAX_TEXT_VALUE = 256

COLLECTION_STATUSES = {"COLLECTED", "PARTIAL", "UNSUPPORTED", "FAILED", "SKIPPED"}
COMPLETENESS = {"COMPLETE", "PARTIAL", "FAILED"}

# Anything matching these must never arrive in, or be persisted from, a
# snapshot payload.
_FORBIDDEN_KEYS = {
    "rows", "sample_rows", "samples", "records", "data", "raw", "raw_rows",
    "sql", "query", "statement", "dsn", "password", "token", "secret",
    "private_key", "credentials", "connection_string",
}


def _reject_forbidden(payload, path="body"):
    """Refuse raw rows, credentials and SQL at the API boundary."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise ValidationError(
                    f"{path}.{key} is not accepted: snapshots carry metadata only, "
                    f"never raw rows, credentials or SQL")
            _reject_forbidden(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload[:50]):
            _reject_forbidden(value, f"{path}[{index}]")


def _bounded(value):
    if value is None:
        return None
    text = str(value)
    if len(text) > MAX_TEXT_VALUE:
        return text[: MAX_TEXT_VALUE - 3] + "..."
    return text


def _payload_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _serialise_request(request_row):
    """Only what a collector needs. No internal identifiers beyond the scope
    it is already authorised for."""
    return {
        "request_id": request_row["request_id"],
        "review_id": request_row["review_id"],
        "environment": request_row["environment"],
        "reason": request_row["reason"],
        "priority": request_row["priority"],
        "required_evidence_level": request_row["required_evidence_level"],
        "state": request_row["state"],
        "base_sha": request_row["base_sha"],
        "head_sha": request_row["head_sha"],
        "base_manifest_hash": request_row["base_manifest_hash"],
        "head_manifest_hash": request_row["head_manifest_hash"],
        "expires_at": request_row["expires_at"].isoformat()
        if hasattr(request_row["expires_at"], "isoformat") else request_row["expires_at"],
        "attempt": (request_row.get("plan") or {}).get("attempt"),
        "policy_version": (request_row.get("plan") or {}).get("policy_version"),
        "targets": [
            {
                "relation_name": t["relation_name"],
                "relation_database": t["relation_database"],
                "relation_schema": t["relation_schema"],
                "model_unique_id": t["model_unique_id"],
                "columns": t["columns"],
                "required_signals": t["required_signals"],
                "dependency_kind": t["dependency_kind"],
                "criticality": t["criticality"],
            }
            for t in request_row.get("targets", [])
        ],
    }


def _relation_evidence_view(relation):
    """One relation's measured evidence, safe to disclose.

    Aggregates only. ``min_value`` and ``max_value`` are the sole fields in
    the schema derived from an actual cell, and they are deliberately not
    projected here: a JSON viewer in the dashboard must not become a way to
    read customer data.
    """
    return {
        "relation_name": relation.get("relation_name"),
        "relation_schema": relation.get("relation_schema"),
        "relation_type": relation.get("relation_type"),
        "exists_in_production": relation.get("exists_in_production"),
        "collection_status": relation.get("collection_status"),
        "schema_fingerprint": relation.get("schema_fingerprint"),
        "row_count": relation.get("row_count"),
        "freshness_lag_seconds": relation.get("freshness_lag_seconds"),
        "unevaluated_checks": relation.get("unevaluated_checks") or [],
        "collection_error": relation.get("collection_error"),
        "observed_at": (relation["observed_at"].isoformat()
                        if hasattr(relation.get("observed_at"), "isoformat")
                        else relation.get("observed_at")),
        "columns": [
            {
                "column_name": column.get("column_name"),
                "exists_in_production": column.get("exists_in_production"),
                "data_type": column.get("data_type"),
                "is_nullable": column.get("is_nullable"),
                "ordinal_position": column.get("ordinal_position"),
                "null_count": column.get("null_count"),
                "null_rate": column.get("null_rate"),
                "duplicate_count": column.get("duplicate_count"),
                "duplicate_rate": column.get("duplicate_rate"),
                "distinct_count": column.get("distinct_count"),
                "cardinality": column.get("cardinality"),
                "collection_status": column.get("collection_status"),
            }
            for column in (relation.get("columns") or [])
        ],
    }


def _parse_relations(body):
    relations = body.get("relations")
    if relations is None:
        return []
    if not isinstance(relations, list):
        raise ValidationError("relations must be an array")
    if len(relations) > MAX_RELATIONS:
        raise ValidationError(f"relations exceeds the {MAX_RELATIONS} bound")
    parsed = []
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            raise ValidationError(f"relations[{index}] must be an object")
        columns = relation.get("columns") or []
        if not isinstance(columns, list):
            raise ValidationError(f"relations[{index}].columns must be an array")
        if len(columns) > MAX_COLUMNS_PER_RELATION:
            raise ValidationError(
                f"relations[{index}].columns exceeds the "
                f"{MAX_COLUMNS_PER_RELATION} bound")
        status = relation.get("collection_status", "COLLECTED")
        if status not in COLLECTION_STATUSES:
            raise ValidationError(f"relations[{index}].collection_status is invalid")
        parsed_columns = []
        for c_index, column in enumerate(columns):
            if not isinstance(column, dict) or not column.get("column_name"):
                raise ValidationError(
                    f"relations[{index}].columns[{c_index}] requires column_name")
            c_status = column.get("collection_status", "COLLECTED")
            if c_status not in COLLECTION_STATUSES:
                raise ValidationError(
                    f"relations[{index}].columns[{c_index}].collection_status is invalid")
            # A collector reports every column the request named, including one
            # it could not find. Dropping that flag stored an absent column as
            # a present one with unknown metrics, which the decision engine
            # read as "nothing to report". `exists` is the collector's spelling;
            # `exists_in_production` matches the relation level and wins.
            exists = column.get("exists_in_production", column.get("exists", True))
            parsed_columns.append({
                "column_name": str(column["column_name"])[:200],
                "exists_in_production": bool(exists),
                "data_type": _bounded(column.get("data_type")),
                "is_nullable": column.get("is_nullable"),
                "ordinal_position": column.get("ordinal_position"),
                "null_count": column.get("null_count"),
                "null_rate": column.get("null_rate"),
                "duplicate_count": column.get("duplicate_count"),
                "duplicate_rate": column.get("duplicate_rate"),
                "distinct_count": column.get("distinct_count"),
                "cardinality": column.get("cardinality"),
                # Extremes are evidence, not payload: bounded on the way in.
                "min_value": _bounded(column.get("min_value")),
                "max_value": _bounded(column.get("max_value")),
                "collection_status": c_status,
            })
        if not relation.get("relation_name"):
            raise ValidationError(f"relations[{index}] requires relation_name")
        parsed.append({
            "relation_name": str(relation["relation_name"])[:400],
            "relation_database": _bounded(relation.get("relation_database")),
            "relation_schema": _bounded(relation.get("relation_schema")),
            "relation_type": _bounded(relation.get("relation_type")),
            "model_unique_id": _bounded(relation.get("model_unique_id")),
            "exists_in_production": bool(relation.get("exists_in_production", True)),
            "schema_fingerprint": _bounded(relation.get("schema_fingerprint")),
            "row_count": relation.get("row_count"),
            "freshness_timestamp": relation.get("freshness_timestamp"),
            "freshness_lag_seconds": relation.get("freshness_lag_seconds"),
            "lineage_level": _bounded(relation.get("lineage_level")),
            "lineage_completeness": relation.get("lineage_completeness"),
            "dbt_run_status": _bounded(relation.get("dbt_run_status")),
            "dbt_test_status": _bounded(relation.get("dbt_test_status")),
            "dbt_execution_ms": relation.get("dbt_execution_ms"),
            "collection_status": status,
            "unevaluated_checks": relation.get("unevaluated_checks") or [],
            "collection_error": _bounded(relation.get("collection_error")),
            "observed_at": relation.get("observed_at"),
            "columns": parsed_columns,
        })
    return parsed


def _parse_metrics(body):
    metrics = body.get("metrics")
    if metrics is None:
        return []
    if not isinstance(metrics, list):
        raise ValidationError("metrics must be an array")
    if len(metrics) > MAX_METRICS:
        raise ValidationError(f"metrics exceeds the {MAX_METRICS} bound")
    parsed = []
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or not metric.get("metric_name"):
            raise ValidationError(f"metrics[{index}] requires metric_name")
        parsed.append({
            "metric_name": str(metric["metric_name"])[:200],
            "model_unique_id": _bounded(metric.get("model_unique_id")),
            "relation_name": _bounded(metric.get("relation_name")),
            "metric_value": metric.get("metric_value"),
            "metric_text": _bounded(metric.get("metric_text")),
            # The expression itself is never stored, only its fingerprint.
            "expression_fingerprint": _bounded(metric.get("expression_fingerprint")),
            "collection_status": metric.get("collection_status", "COLLECTED"),
            "observed_at": metric.get("observed_at"),
        })
    return parsed


def build_handlers():
    """Return {name: handler} for registration in the /api route table."""

    # -- collection requests ----------------------------------------------

    def list_collection_requests(request, body, scope, service):
        environment = scope.require_environment(
            request.query_params.get("environment"))
        limit, _ = pagination(request.query_params)
        rows = service.store.pending_collection_requests(
            scope.organization_id, scope.repository_id,
            environment=environment, limit=limit)
        return 200, {"status": "ok",
                     "requests": [_serialise_request(r) for r in rows],
                     "count": len(rows)}

    def get_collection_request(request, body, scope, service):
        request_id = request.path_params["request_id"]
        row = service.store.get_collection_request(
            scope.organization_id, scope.repository_id, request_id)
        if row is None:
            raise NotFoundError("collection request not found")
        scope.require_environment(row["environment"])
        return 200, {"status": "ok", "request": _serialise_request(row)}

    def acknowledge_collection_request(request, body, scope, service):
        request_id = request.path_params["request_id"]
        collector_id = require_str(body, "collector_id")
        existing = service.store.get_collection_request(
            scope.organization_id, scope.repository_id, request_id)
        if existing is None:
            raise NotFoundError("collection request not found")
        scope.require_environment(existing["environment"])
        row = service.store.acknowledge_collection_request(
            scope.organization_id, scope.repository_id, request_id,
            collector_id=collector_id)
        if row is None:
            # Already acknowledged, completed or expired. Idempotent rather
            # than an error: a collector retrying must not be punished.
            return 200, {"status": "not_acknowledgeable",
                         "request": _serialise_request(existing)}
        return 200, {"status": "acknowledged",
                     "request": _serialise_request(
                         service.store.get_collection_request(
                             scope.organization_id, scope.repository_id, request_id))}

    def report_collection_failure(request, body, scope, service):
        request_id = request.path_params["request_id"]
        reason = require_str(body, "reason")
        existing = service.store.get_collection_request(
            scope.organization_id, scope.repository_id, request_id)
        if existing is None:
            raise NotFoundError("collection request not found")
        scope.require_environment(existing["environment"])
        row = service.store.close_collection_request(
            scope.organization_id, scope.repository_id, request_id,
            state="FAILED", failure_reason=_bounded(reason))
        service.store.append_audit(
            scope.organization_id, scope.repository_id, actor="collector",
            event_type="collection.failed", reference_type="collection_request",
            reference_id=request_id, payload={"reason": _bounded(reason)})
        return 200, {"status": "recorded", "request": _serialise_request(row)}

    # -- snapshots ---------------------------------------------------------

    def submit_snapshot(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        _reject_forbidden(body)

        review_id = require_str(body, "review_id")
        environment = scope.require_environment(optional_str(body, "environment"))
        request_id = optional_str(body, "request_id")
        attempt = body.get("attempt")
        completeness = optional_choice(body, "completeness", COMPLETENESS) or "COMPLETE"
        observed_at = require_timestamp(body, "observed_at")
        collected_at = body.get("collected_at")
        collected_at = require_timestamp(body, "collected_at") if collected_at else observed_at

        relations = _parse_relations(body)
        metrics = _parse_metrics(body)

        collector_id = optional_str(body, "collector_id")
        if collector_id:
            collector = service.store.get_collector(
                scope.organization_id, scope.repository_id, collector_id)
            if collector is None:
                raise NotFoundError("unknown collector")
            if collector["revoked"]:
                raise ConflictError("collector identity has been revoked")

        review = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if review is None:
            # Non-disclosing: a review in another tenant is indistinguishable
            # from one that does not exist.
            raise NotFoundError("review not found")

        ttl_seconds = body.get("ttl_seconds")
        canonical = {
            "review_id": review_id, "environment": environment,
            "relations": relations, "metrics": metrics,
            "completeness": completeness, "observed_at": str(observed_at),
        }
        payload_hash = _payload_hash(canonical)
        snapshot_id = optional_str(body, "snapshot_id") or new_snapshot_id()

        provisional = {
            "observed_at": observed_at, "ttl_seconds": ttl_seconds,
            "relations": relations,
        }
        freshness = classify_freshness(provisional)

        try:
            snapshot, created = service.store.submit_metadata_snapshot(
                scope.organization_id, scope.repository_id, environment,
                snapshot_id=snapshot_id, idempotency_key=key, payload_hash=payload_hash,
                evidence_hash=_payload_hash({"relations": relations, "metrics": metrics}),
                observed_at=observed_at, collected_at=collected_at,
                relations=relations, metrics=metrics,
                collector_id=collector_id,
                collector_version=optional_str(body, "collector_version"),
                adapter_type=optional_str(body, "adapter_type"),
                request_id=request_id, review_id=review_id,
                base_sha=optional_str(body, "base_sha"),
                head_sha=optional_str(body, "head_sha"),
                production_deployment_sha=optional_str(body, "production_deployment_sha"),
                base_manifest_hash=optional_str(body, "base_manifest_hash"),
                head_manifest_hash=optional_str(body, "head_manifest_hash"),
                configuration_version=optional_str(body, "configuration_version"),
                completeness=completeness, freshness_state=freshness,
                provenance=optional_object(body, "provenance") or {},
                ttl_seconds=ttl_seconds,
                )
        except SnapshotConflict as exc:
            raise ConflictError(str(exc)) from None

        stored = service.store.get_snapshot(
            scope.organization_id, scope.repository_id, snapshot["snapshot_id"],
            expand=False)
        try:
            validate_and_bind_snapshot(
                service.store,
                organization_id=scope.organization_id,
                repository_id=scope.repository_id,
                environment=environment,
                review_id=review_id, snapshot=stored,
                request_id=request_id, attempt=attempt,
            )
        except SnapshotRejected as exc:
            return 409, {"status": "rejected", "reason": exc.reason,
                         "checks": exc.checks,
                         "snapshot_id": snapshot["snapshot_id"]}

        return (202 if created else 200), {
            "status": "accepted",
            "snapshot_id": snapshot["snapshot_id"],
            "review_id": review_id,
            "completeness": completeness,
            "freshness_state": freshness,
            "recomputation_queued": True,
        }

    def get_snapshot_status(request, body, scope, service):
        snapshot_id = request.path_params["snapshot_id"]
        snapshot = service.store.get_snapshot(
            scope.organization_id, scope.repository_id, snapshot_id)
        if snapshot is None:
            raise NotFoundError("snapshot not found")
        bindings = [
            b for b in service.store.review_bindings(
                scope.organization_id, scope.repository_id, snapshot["review_id"] or "")
            if b["snapshot_id"] == snapshot_id
        ] if snapshot.get("review_id") else []
        # The measured evidence itself, not just a count of it. A reviewer
        # cannot check a null-rate finding against a number the API refuses to
        # show. Everything here is aggregate metadata the collector computed -
        # shapes, counts and rates. No warehouse row can reach this payload:
        # min_value/max_value are the only cell-derived fields the schema has,
        # and they are omitted rather than disclosed.
        return 200, {
            "status": "ok",
            "snapshot": {
                "snapshot_id": snapshot["snapshot_id"],
                "review_id": snapshot["review_id"],
                "environment": snapshot["environment"],
                "completeness": snapshot["completeness"],
                "freshness_state": snapshot["freshness_state"],
                "observed_at": snapshot["observed_at"].isoformat(),
                "received_at": snapshot["received_at"].isoformat(),
                "collected_at": (snapshot["collected_at"].isoformat()
                                 if snapshot.get("collected_at") else None),
                "ttl_seconds": snapshot.get("ttl_seconds"),
                "request_id": snapshot.get("request_id"),
                "collector_id": snapshot.get("collector_id"),
                "collector_version": snapshot.get("collector_version"),
                "adapter_type": snapshot.get("adapter_type"),
                "base_sha": snapshot.get("base_sha"),
                "head_sha": snapshot.get("head_sha"),
                "base_manifest_hash": snapshot.get("base_manifest_hash"),
                "head_manifest_hash": snapshot.get("head_manifest_hash"),
                "relation_count": len(snapshot.get("relations") or []),
                "metric_count": len(snapshot.get("metrics") or []),
                "relations": [_relation_evidence_view(r)
                              for r in (snapshot.get("relations") or [])],
            },
            "bindings": [
                {"binding_state": b["binding_state"],
                 "rejection_reason": b["rejection_reason"]} for b in bindings],
        }

    def get_review_evidence_coverage(request, body, scope, service):
        review_id = request.path_params["review_id"]
        review = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if review is None:
            raise NotFoundError("review not found")
        states = service.store.evidence_states(
            scope.organization_id, scope.repository_id, review_id)
        return 200, {
            "status": "ok",
            "review_id": review_id,
            "attempt": review["attempt"],
            "lifecycle_state": review["lifecycle_state"],
            "decision": review["decision"],
            "evidence_coverage": review["evidence_coverage"],
            "health": review["health"],
            "enforcement_mode": review["enforcement_mode"],
            "policy_version": review["policy_version"],
            "evidence": [
                {"attempt": s["attempt"], "source": s["evidence_source"],
                 "requirement": s["requirement"], "state": s["state"],
                 "evidence_state_group": s["evidence_state_group"],
                 "detail": s["detail"]}
                for s in states
            ],
        }

    def register_collector(request, body, scope, service):
        collector_id = require_str(body, "collector_id")
        environment = scope.require_environment(optional_str(body, "environment"))
        row = service.store.register_collector(
            scope.organization_id, scope.repository_id, environment,
            collector_id=collector_id,
            collector_version=optional_str(body, "collector_version"),
            adapter_type=optional_str(body, "adapter_type"),
            description=optional_str(body, "description"))
        return 200, {"status": "registered", "collector": {
            "collector_id": row["collector_id"], "environment": row["environment"],
            "collector_version": row["collector_version"],
            "adapter_type": row["adapter_type"], "revoked": row["revoked"]}}

    return {
        "list_collection_requests": list_collection_requests,
        "get_collection_request": get_collection_request,
        "acknowledge_collection_request": acknowledge_collection_request,
        "report_collection_failure": report_collection_failure,
        "submit_snapshot": submit_snapshot,
        "get_snapshot_status": get_snapshot_status,
        "get_review_evidence_coverage": get_review_evidence_coverage,
        "register_collector": register_collector,
    }


COLLECTOR_ROUTES = (
    ("GET", "/api/collection-requests", "list_collection_requests", False),
    ("GET", "/api/collection-requests/{request_id}", "get_collection_request", False),
    ("POST", "/api/collection-requests/{request_id}/acknowledge",
     "acknowledge_collection_request", True),
    ("POST", "/api/collection-requests/{request_id}/failure",
     "report_collection_failure", True),
    ("POST", "/api/metadata-snapshots", "submit_snapshot", True),
    ("GET", "/api/metadata-snapshots/{snapshot_id}", "get_snapshot_status", False),
    ("GET", "/api/reviews/{review_id}/evidence-coverage",
     "get_review_evidence_coverage", False),
    ("POST", "/api/collectors", "register_collector", True),
)
