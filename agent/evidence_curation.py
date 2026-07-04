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
    "kpi_impact": 2,
    "semantic_contract": 3,
    "ast": 4,
    "metadata_checks": 4,
    "metadata_drift": 4,
    "blast_radius": 4,
    "historical_reliability": 4,
    "business_metrics": 4,
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


def order_semantic_diff_reasons(reasons: list[Any]) -> list[str]:
    candidates = []
    for reason_index, reason in enumerate(reasons or []):
        cleaned_reason = clean_reason(reason)
        if not cleaned_reason:
            continue
        candidates.append(
            (
                _semantic_diff_sort_priority(cleaned_reason),
                reason_index,
                cleaned_reason,
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    return _ordered_unique(item[2] for item in candidates)


def is_column_level_reason(reason: Any) -> bool:
    return _column_reason_model(clean_reason(reason)) is not None


def is_supporting_column_reason(reason: Any) -> bool:
    model = _column_reason_model(clean_reason(reason))
    return bool(model and _is_staging_or_source_model(model))


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
                    "_priority": _item_priority(component, cleaned_reason),
                    "_semantic_diff_priority": _semantic_diff_sort_priority(
                        cleaned_reason
                    ),
                    "_signal_index": signal_index,
                    "_reason_index": reason_index,
                }
            )

    candidates.sort(
        key=lambda item: (
            item["_priority"],
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


def _item_priority(component: str, reason: str) -> tuple[int, int]:
    normalized = str(component or "").lower()
    if normalized == "semantic_diff":
        semantic_priority = _semantic_diff_sort_priority(reason)
        if is_supporting_column_reason(reason):
            return (8, semantic_priority)
        if is_column_level_reason(reason):
            return (1, semantic_priority)
        return (0, semantic_priority)
    return (_priority(normalized), 0)


def semantic_diff_reason_priority(reason: Any) -> int:
    return _semantic_diff_sort_priority(clean_reason(reason))


def _semantic_diff_sort_priority(reason: Any) -> int:
    cleaned = clean_reason(reason).casefold()
    if is_supporting_column_reason(cleaned):
        return 100
    if is_column_level_reason(cleaned):
        if "upstream column" in cleaned:
            return 20
        return 30

    for priority, (subject, actions) in enumerate(SEMANTIC_DIFF_REASON_PATTERNS):
        if subject in cleaned and any(action in cleaned for action in actions):
            return priority

    for offset, (subject, actions) in enumerate(GENERIC_KPI_REASON_PATTERNS):
        if subject in cleaned and any(action in cleaned for action in actions):
            return len(SEMANTIC_DIFF_REASON_PATTERNS) + offset

    return len(SEMANTIC_DIFF_REASON_PATTERNS) + len(GENERIC_KPI_REASON_PATTERNS)


def _column_reason_model(reason: str) -> str | None:
    match = re.match(
        r"^(?P<model>[A-Za-z_][\w]*)\.[A-Za-z_][\w]* "
        r"(?:output column was (?:added|removed)|(?:gained|lost) upstream column)\b",
        reason,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group("model")


def _is_staging_or_source_model(model_name: str) -> bool:
    normalized = str(model_name or "").casefold()
    return normalized.startswith((
        "stg_",
        "stage_",
        "staging_",
        "src_",
        "source_",
        "raw_",
    ))


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value
