from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class KPIHint:
    source: str
    value: str
    confidence: int
    reason: str


@dataclass
class DiscoveredKPI:
    name: str
    description: str
    industry_hint: str | None = None
    related_models: list[str] = field(default_factory=list)
    related_columns: list[str] = field(default_factory=list)
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


KPI_CATEGORIES = [
    {
        "name": "Revenue / GMV",
        "description": "Revenue, gross merchandise value, order value, or payment volume.",
        "industry_hint": "commerce",
        "keywords": [
            "revenue",
            "gmv",
            "gross_merchandise",
            "order_value",
            "payment_amount",
            "orders",
            "payments",
        ],
    },
    {
        "name": "Conversion",
        "description": "Conversion from visit, trial, funnel, cart, or signup to a target action.",
        "industry_hint": None,
        "keywords": ["conversion", "converted", "funnel", "checkout", "signup", "cart"],
    },
    {
        "name": "Churn / Retention",
        "description": "Customer churn, renewal, retention, or subscription continuity.",
        "industry_hint": "saas",
        "keywords": ["churn", "retention", "renewal", "subscription_cancel", "customer_lifetime"],
    },
    {
        "name": "Recurring Revenue",
        "description": "Recurring subscription revenue such as MRR or ARR.",
        "industry_hint": "saas",
        "keywords": ["mrr", "arr", "recurring_revenue", "subscription_revenue"],
    },
    {
        "name": "Failed Payments",
        "description": "Failed payment attempts, declined charges, and payment recovery.",
        "industry_hint": None,
        "keywords": ["failed_payment", "payment_failed", "declined_payment", "payment_status"],
    },
    {
        "name": "Fulfillment Reliability",
        "description": "Fulfillment accuracy, failed pickups, mis-sorts, staging, and warehouse flow.",
        "industry_hint": "logistics",
        "keywords": ["fulfillment", "failed_pickup", "failed_pickups", "mis_sort", "mis_sorts", "staging_area"],
    },
    {
        "name": "Delivery SLA",
        "description": "On-time delivery, delivery latency, and SLA adherence.",
        "industry_hint": "logistics",
        "keywords": ["delivery_sla", "on_time_delivery", "late_delivery", "delivered_at", "due_at"],
    },
    {
        "name": "Playback Reliability",
        "description": "Playback starts, buffering, stream reliability, and watch experience.",
        "industry_hint": "media",
        "keywords": ["playback", "buffering", "stream_start", "watch_time", "rebuffer"],
    },
    {
        "name": "Fraud",
        "description": "Fraud detection, suspicious activity, chargebacks, and abuse risk.",
        "industry_hint": None,
        "keywords": ["fraud", "chargeback", "risk_score", "suspicious", "abuse"],
    },
    {
        "name": "Inventory Accuracy",
        "description": "Inventory accuracy, stockouts, counts, and availability.",
        "industry_hint": "retail",
        "keywords": ["inventory", "stockout", "stock_count", "inventory_accuracy", "on_hand"],
    },
]


SOURCE_CONFIDENCE = {
    "dbt_metrics": 35,
    "semantic_models": 30,
    "dashboard_names": 25,
    "business_terms": 25,
    "model_names": 20,
    "column_names": 20,
    "sql_expressions": 15,
    "file_paths": 10,
}


def discover_kpis(project_context: dict) -> list[DiscoveredKPI]:
    context = dict(project_context or {})
    observed = _observations(context)
    discovered = []

    for category in KPI_CATEGORIES:
        hints = _hints_for_category(category, observed)
        if not hints:
            continue
        related_models = _related_values("model_names", hints)
        related_columns = _related_values("column_names", hints)
        discovered.append(
            DiscoveredKPI(
                name=category["name"],
                description=category["description"],
                industry_hint=category.get("industry_hint"),
                related_models=related_models,
                related_columns=related_columns,
                confidence=_confidence(hints),
                reasons=[hint.reason for hint in hints],
                metadata={
                    "hints": list(hints),
                    "matched_sources": sorted({hint.source for hint in hints}),
                    "matched_values": [hint.value for hint in hints],
                },
            )
        )

    return sorted(discovered, key=lambda kpi: (-kpi.confidence, kpi.name))


def _observations(context: dict) -> list[tuple[str, str, str]]:
    observations = []
    for source in SOURCE_CONFIDENCE:
        for value in _values(context.get(source)):
            text = _normalise(value)
            if text:
                observations.append((source, str(value), text))
    return observations


def _values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        values = []
        for key, value in raw.items():
            values.extend(_values(key))
            values.extend(_values(value))
        return values
    if isinstance(raw, (list, tuple, set)):
        values = []
        for item in raw:
            values.extend(_values(item))
        return values
    return [str(raw)]


def _hints_for_category(category: dict, observations: list[tuple[str, str, str]]) -> list[KPIHint]:
    hints = []
    for source, value, normalised in observations:
        matched = [
            keyword for keyword in category["keywords"]
            if _keyword_matches(normalised, keyword)
        ]
        if matched:
            reason = (
                f"{source} value '{value}' matched KPI concept "
                f"{category['name']} via {', '.join(matched)}"
            )
            hints.append(
                KPIHint(
                    source=source,
                    value=value,
                    confidence=SOURCE_CONFIDENCE[source],
                    reason=reason,
                )
            )
    return hints


def _related_values(source: str, hints: list[KPIHint]) -> list[str]:
    values = []
    for hint in hints:
        if hint.source == source and hint.value not in values:
            values.append(hint.value)
    return values


def _confidence(hints: list[KPIHint]) -> int:
    source_bonus = len({hint.source for hint in hints}) * 8
    evidence_bonus = min(len(hints) * 10, 45)
    base = max(hint.confidence for hint in hints)
    return min(95, base + source_bonus + evidence_bonus)


def _keyword_matches(text: str, keyword: str) -> bool:
    return _normalise(keyword) in text


def _normalise(value: Any) -> str:
    text = str(value).lower()
    chars = []
    previous_was_separator = False
    for char in text:
        if char.isalnum():
            chars.append(char)
            previous_was_separator = False
        elif not previous_was_separator:
            chars.append("_")
            previous_was_separator = True
    return "".join(chars).strip("_")
