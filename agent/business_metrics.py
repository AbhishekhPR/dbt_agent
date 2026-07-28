from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.signals import Signal


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    description: str
    event_type: str | None = None
    expected_field: str | None = None
    actual_field: str | None = None
    status_field: str | None = None
    status_value: str | None = None
    boolean_field: str | None = None
    numeric_field: str | None = None
    numeric_min: int | float | None = None
    requires_late_delivery: bool = False
    spike_multiplier: int | float = 3


METRIC_FIELDS = [
    "carts_delivered_wrong_staging_area_and_late",
    "mis_sorts",
    "totes_loaded_in_incorrect_order",
    "failed_pickups",
    "overflow_avalanches",
]
REQUIRED_FIELDS = METRIC_FIELDS + ["total_events"]

DEFAULT_OPERATIONAL_METRICS = [
    MetricDefinition(
        name="carts_delivered_wrong_staging_area_and_late",
        description="Carts delivered to the wrong staging area after the due time.",
        event_type="cart_delivered",
        expected_field="expected_staging_area",
        actual_field="actual_staging_area",
        requires_late_delivery=True,
    ),
    MetricDefinition(
        name="mis_sorts",
        description="Sort events where the actual location differs from expected.",
        event_type="sort",
        expected_field="expected_sort_location",
        actual_field="actual_sort_location",
    ),
    MetricDefinition(
        name="totes_loaded_in_incorrect_order",
        description="Totes loaded outside the expected sequence.",
        event_type="tote_loaded",
        expected_field="expected_load_sequence",
        actual_field="actual_load_sequence",
    ),
    MetricDefinition(
        name="failed_pickups",
        description="Pickup events with failed status.",
        event_type="pickup",
        status_field="pickup_status",
        status_value="failed",
    ),
    MetricDefinition(
        name="overflow_avalanches",
        description="Overflow events where an avalanche was detected.",
        event_type="overflow",
        boolean_field="avalanche_detected",
        numeric_field="overflow_count",
        numeric_min=1,
    ),
]


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
    return calculate_metrics(events, DEFAULT_OPERATIONAL_METRICS)


def calculate_metrics(events: list[dict], definitions: list[MetricDefinition]) -> dict:
    definitions_copy = list(definitions)
    metrics = {definition.name: 0 for definition in definitions_copy}
    metrics["total_events"] = len(events)

    for event in events:
        for definition in definitions_copy:
            if _matches_definition(event, definition):
                metrics[definition.name] += 1

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
    spike_percentages = _spike_percentages(metrics_copy, baseline_copy, spike_fields)

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
            "spike_percentages": spike_percentages,
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


def _matches_definition(event: dict, definition: MetricDefinition) -> bool:
    if definition.event_type and event.get("event_type") != definition.event_type:
        return False
    if definition.expected_field or definition.actual_field:
        if not (
            definition.expected_field
            and definition.actual_field
            and _mismatch(event, definition.expected_field, definition.actual_field)
        ):
            return False
    if definition.status_field:
        actual = str(event.get(definition.status_field, "")).lower()
        expected = str(definition.status_value or "").lower()
        if actual != expected:
            return False
    if definition.boolean_field and event.get(definition.boolean_field) is not True:
        return False
    if definition.numeric_field and definition.numeric_min is not None:
        if _number(event.get(definition.numeric_field)) < definition.numeric_min:
            return False
    if definition.requires_late_delivery and not _is_late_delivery(event):
        return False
    return True


def _is_late_delivery(event: dict) -> bool:
    delivered_at = event.get("delivered_at")
    due_at = event.get("due_at")
    return bool(delivered_at and due_at and _timestamp(delivered_at) > _timestamp(due_at))


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


def _spike_percentages(
    metrics: dict,
    baseline: dict | None,
    spike_fields: list[str],
) -> dict[str, float]:
    if not baseline:
        return {}
    percentages = {}
    for field in spike_fields:
        current = _number(metrics.get(field))
        previous = _number(baseline.get(field))
        if previous == 0:
            percentages[field] = 100.0
        else:
            percentages[field] = round(((current - previous) / previous) * 100, 1)
    return percentages


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
