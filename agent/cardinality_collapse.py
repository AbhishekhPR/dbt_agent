"""Deterministic D10 cardinality-collapse evaluation from warehouse metadata."""

from __future__ import annotations

import math
from statistics import median
from typing import Any


def evaluate_cardinality_collapse(observation: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    thresholds = {"warning_distinct_ratio": 0.8, "critical_distinct_ratio": 0.5, "warning_null_key_ratio": 0.2, "critical_null_key_ratio": 0.5, "minimum_sample_size": 10, **(thresholds or {})}
    current = observation.get("current_distinct_key_count")
    previous = observation.get("previous_distinct_key_count")
    current_rows = observation.get("current_row_count")
    previous_rows = observation.get("previous_row_count")
    baseline = observation.get("historical_baseline_window")
    base = _result_base(observation)
    if observation.get("declared_grain_changed"):
        return {**base, "status": "NOT EVALUATED", "reason": "Declared grain changed; collapse is not comparable."}
    if observation.get("intentional_change"):
        return {**base, "status": "HEALTHY", "intentional_change": True, "reason": "Intentional-change contract applies."}
    required = (current, previous, current_rows, previous_rows, baseline)
    if any(value is None for value in required) or not baseline or observation.get("sample_size", 0) < thresholds["minimum_sample_size"]:
        return {**base, "status": "NOT EVALUATED", "reason": "Insufficient metadata, history, or observation size."}
    if float(previous) <= 0 or float(previous_rows) <= 0:
        return {**base, "status": "NOT EVALUATED", "reason": "Previous counts must be positive."}
    distinct_ratio = float(current) / float(previous)
    rows_ratio = float(current_rows) / float(previous_rows)
    current_rpk = float(current_rows) / max(float(current), 1.0)
    previous_rpk = float(previous_rows) / max(float(previous), 1.0)
    null_ratio = float(observation.get("null_key_count", 0)) / max(float(current_rows), 1.0)
    baseline_median = median(float(value) for value in baseline)
    baseline_mad = median(abs(float(value) - baseline_median) for value in baseline)
    z_score = (float(current) - baseline_median) / (1.4826 * baseline_mad) if baseline_mad else 0.0
    metrics = {
        "absolute_distinct_count_change": float(current) - float(previous),
        "distinct_key_ratio": round(distinct_ratio, 6),
        "rows_per_key_ratio": round(current_rpk / previous_rpk, 6),
        "uniqueness_ratio": round(float(current) / max(float(current_rows), 1.0), 6),
        "null_key_ratio": round(null_ratio, 6),
        "historical_z_score": round(z_score, 6),
        "row_count_ratio": round(rows_ratio, 6),
    }
    status = "HEALTHY"
    reasons = []
    if distinct_ratio <= thresholds["critical_distinct_ratio"]:
        status = "CRITICAL"
        reasons.append("Distinct-key count collapsed below the critical ratio.")
    elif distinct_ratio <= thresholds["warning_distinct_ratio"]:
        status = "WARN"
        reasons.append("Distinct-key count declined below the warning ratio.")
    if null_ratio >= thresholds["critical_null_key_ratio"]:
        status = "CRITICAL"
        reasons.append("Null-key ratio exceeded the critical threshold.")
    elif null_ratio >= thresholds["warning_null_key_ratio"] and status == "HEALTHY":
        status = "WARN"
        reasons.append("Null-key ratio exceeded the warning threshold.")
    if observation.get("rollback_observed") and distinct_ratio >= thresholds["warning_distinct_ratio"]:
        status = "HEALTHY"
        reasons.append("Behavior recovered after rollback observation.")
    return {**base, "status": status, "metrics": metrics, "reasons": reasons, "historical_baseline": {"median": baseline_median, "mad": baseline_mad}, "rollback_observed": bool(observation.get("rollback_observed"))}


def _result_base(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_identity": observation.get("model_identity"),
        "declared_grain": observation.get("declared_grain", []),
        "key_columns": observation.get("key_columns", []),
        "deployment_id": observation.get("deployment_id"),
        "pr_number": observation.get("pr_number"),
        "commit_sha": observation.get("commit_sha"),
        "downstream_models": list(observation.get("downstream_models", [])),
        "affected_kpis": list(observation.get("affected_kpis", [])),
        "repeated_deployment_ids": list(observation.get("repeated_deployment_ids", [])),
        "intentional_change": bool(observation.get("intentional_change")),
        "rollback_observed": bool(observation.get("rollback_observed")),
    }
