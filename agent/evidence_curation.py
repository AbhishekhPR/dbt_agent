import re
from enum import Enum
from typing import Any


LOW_LEVEL_REASON_PATTERNS = (
    "matched kpi concept",
    "business_terms value",
    "column_names value",
    "file_paths value",
    "model_names value",
    "dbt_metrics value",
    "dashboard_names value",
)

COMPONENT_LABELS = {
    "semantic_diff": "Historical Semantic Change",
    "semantic_contract": "Semantic Contract",
    "kpi_impact": "KPI Impact",
    "ast": "SQL Logic",
    "metadata_checks": "Metadata Checks",
    "metadata_drift": "Metadata Drift",
    "blast_radius": "Blast Radius",
    "historical_reliability": "Historical Reliability",
}

COMPONENT_PRIORITIES = {
    "semantic_diff": 0,
    "semantic_contract": 1,
    "kpi_impact": 2,
    "ast": 3,
    "metadata_checks": 3,
    "metadata_drift": 3,
    "blast_radius": 3,
    "historical_reliability": 3,
    "business_metrics": 3,
}

SEMANTIC_DIFF_REASON_PATTERNS = (
    ("invariant", ("removed", "lost", "dropped")),
    ("invariant", ("changed", "updated", "modified")),
    ("upstream dependency", ("added", "gained", "introduced")),
    ("upstream dependency", ("changed", "updated", "modified")),
    ("upstream dependency", ("removed", "lost", "dropped")),
    ("contract meaning", ("changed", "updated", "modified")),
    ("assumption", ("changed", "updated", "modified")),
    ("related column", ("changed", "updated", "modified")),
    ("related model", ("changed", "updated", "modified")),
    ("related model", ("added", "gained", "introduced")),
    ("downstream consumer", ("changed", "updated", "modified")),
    ("downstream consumer", ("added", "gained", "introduced")),
)

GENERIC_KPI_REASON_PATTERNS = (
    ("kpi", ("added", "gained", "introduced")),
    ("kpi", ("removed", "lost", "dropped")),
)


def curate_reasons(signals, max_reasons: int = 8) -> list[str]:
    return [
        item["reason"]
        for item in _curated_items(signals)[:max_reasons]
    ]


def curate_evidence(signals, max_items: int = 12) -> list[dict]:
    return [
        {
            "label": item["label"],
            "reason": item["reason"],
            "severity": item["severity"],
            "confidence": item["confidence"],
            "component": item["component"],
        }
        for item in _curated_items(signals)[:max_items]
    ]


def label_for_component(component: str) -> str:
    normalized = str(component or "").lower()
    if normalized in COMPONENT_LABELS:
        return COMPONENT_LABELS[normalized]
    return " ".join(part.capitalize() for part in str(component or "Signal").split("_"))


def clean_reason(reason: Any) -> str:
    text = str(reason).strip()
    text = re.sub(r"\bvia(?=payments\b)", "via ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmatched(?=KPI\b)", "matched ", text)
    text = re.sub(r"\bRevenue\s*/\s*GMV\b", "Revenue / GMV", text)
    text = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=with\b)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def is_low_level_reason(reason: Any) -> bool:
    return _is_low_level_reason(clean_reason(reason))


def _curated_items(signals) -> list[dict]:
    candidates = []
    for signal_index, signal in enumerate(list(signals or [])):
        component = str(getattr(signal, "component", "") or "")
        reasons = list(getattr(signal, "reasons", None) or ["Signal detected"])
        for reason_index, reason in enumerate(reasons):
            cleaned_reason = clean_reason(reason)
            if not cleaned_reason or _is_low_level_reason(cleaned_reason):
                continue
            candidates.append(
                {
                    "label": label_for_component(component),
                    "reason": cleaned_reason,
                    "severity": _enum_value(getattr(signal, "severity", "")),
                    "confidence": getattr(signal, "confidence", 0),
                    "component": component,
                    "_priority": _priority(component),
                    "_semantic_diff_priority": semantic_diff_reason_priority(
                        cleaned_reason
                    ),
                    "_signal_index": signal_index,
                    "_reason_index": reason_index,
                }
            )

    candidates.sort(
        key=lambda item: (
            item["_priority"],
            item["_semantic_diff_priority"]
            if str(item["component"]).lower() == "semantic_diff"
            else 0,
            item["_signal_index"],
            item["_reason_index"],
        )
    )
    return _dedupe(candidates)


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for item in items:
        key = item["reason"].casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                key: value
                for key, value in item.items()
                if not key.startswith("_")
            }
        )
    return deduped


def _is_low_level_reason(reason: str) -> bool:
    lowered = reason.casefold()
    return any(pattern in lowered for pattern in LOW_LEVEL_REASON_PATTERNS)


def _priority(component: str) -> int:
    return COMPONENT_PRIORITIES.get(str(component or "").lower(), 4)


def semantic_diff_reason_priority(reason: Any) -> int:
    cleaned = clean_reason(reason).casefold()
    for priority, (subject, actions) in enumerate(SEMANTIC_DIFF_REASON_PATTERNS):
        if subject in cleaned and any(action in cleaned for action in actions):
            return priority

    for offset, (subject, actions) in enumerate(GENERIC_KPI_REASON_PATTERNS):
        if subject in cleaned and any(action in cleaned for action in actions):
            return len(SEMANTIC_DIFF_REASON_PATTERNS) + offset

    return len(SEMANTIC_DIFF_REASON_PATTERNS) + len(GENERIC_KPI_REASON_PATTERNS)


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value
