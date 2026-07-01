from datetime import datetime
from typing import Any

from agent.signals import Signal


METRIC_FIELDS = [
    "carts_delivered_wrong_staging_area_and_late",
    "mis_sorts",
    "totes_loaded_in_incorrect_order",
    "failed_pickups",
    "overflow_avalanches",
]
REQUIRED_FIELDS = METRIC_FIELDS + ["total_events"]


SEVERITY_CONFIDENCE = {
    "HIGH": 95,
    "MEDIUM": 85,
    "LOW": 90,
}
SEVERITY_SCORES = {
    "HIGH": -35,
    "MEDIUM": -15,
    "LOW": 0,
}


def calculate_operational_metrics(events: list[dict]) -> dict:
    metrics = {field: 0 for field in METRIC_FIELDS}
    metrics["total_events"] = len(events)

    for event in events:
        if _wrong_staging_area_and_late(event):
            metrics["carts_delivered_wrong_staging_area_and_late"] += 1
        if _mismatch(event, "expected_sort_location", "actual_sort_location"):
            metrics["mis_sorts"] += 1
        if _mismatch(event, "expected_load_sequence", "actual_load_sequence"):
            metrics["totes_loaded_in_incorrect_order"] += 1
        if str(event.get("pickup_status", "")).lower() == "failed":
            metrics["failed_pickups"] += 1
        if event.get("avalanche_detected") is True and _number(event.get("overflow_count")) > 0:
            metrics["overflow_avalanches"] += 1

    return metrics


def evaluate_metric_reliability(
    metrics: dict,
    baseline: dict | None = None,
) -> dict:
    metrics_copy = dict(metrics)
    baseline_copy = dict(baseline) if baseline is not None else None
    missing_fields = [
        field for field in REQUIRED_FIELDS
        if field not in metrics_copy
    ]
    spike_fields = _spike_fields(metrics_copy, baseline_copy)

    reasons = []
    if missing_fields:
        reasons.append("Missing metric fields detected")
    if _number(metrics_copy.get("total_events")) <= 0:
        reasons.append("Zero or empty event volume detected")
    if spike_fields:
        reasons.append("High severity metric spike detected")
    if not reasons:
        reasons.append("Business metrics within expected range")

    severity = _severity(
        missing_fields=missing_fields,
        zero_volume=_number(metrics_copy.get("total_events")) <= 0,
        spike_fields=spike_fields,
    )

    return {
        "severity": severity,
        "confidence": SEVERITY_CONFIDENCE[severity],
        "score": SEVERITY_SCORES[severity],
        "reasons": reasons,
        "metadata": {
            "metrics": metrics_copy,
            "baseline": baseline_copy,
            "missing_fields": missing_fields,
            "spike_fields": spike_fields,
            "total_events": metrics_copy.get("total_events", 0),
        },
    }


def to_signal(result: dict) -> Signal:
    return Signal(
        component="business_metrics",
        severity=result.get("severity", "LOW"),
        confidence=int(result.get("confidence", 90)),
        score=int(result.get("score", 0)),
        reasons=list(result.get("reasons", [])),
        metadata=dict(result.get("metadata", {})),
    )


def _wrong_staging_area_and_late(event: dict) -> bool:
    if not _mismatch(event, "expected_staging_area", "actual_staging_area"):
        return False
    delivered_at = event.get("delivered_at")
    due_at = event.get("due_at")
    if not delivered_at or not due_at:
        return False
    return _timestamp(delivered_at) > _timestamp(due_at)


def _mismatch(event: dict, expected_field: str, actual_field: str) -> bool:
    expected = event.get(expected_field)
    actual = event.get(actual_field)
    return expected is not None and actual is not None and expected != actual


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _spike_fields(metrics: dict, baseline: dict | None) -> list[str]:
    if not baseline:
        return []
    spike_fields = []
    for field in METRIC_FIELDS:
        current = _number(metrics.get(field))
        previous = _number(baseline.get(field))
        if previous > 0 and current >= previous * 3:
            spike_fields.append(field)
        elif previous == 0 and current >= 3:
            spike_fields.append(field)
    return spike_fields


def _severity(
    *,
    missing_fields: list[str],
    zero_volume: bool,
    spike_fields: list[str],
) -> str:
    if missing_fields or zero_volume or spike_fields:
        return "HIGH"
    return "LOW"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
