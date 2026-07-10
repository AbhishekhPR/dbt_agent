import re
from enum import Enum
from typing import Any

from agent.decision_engine import DeploymentDecision
from agent.evidence_curation import (
    curate_reasons,
    is_column_level_reason,
    is_supporting_column_reason,
    order_semantic_diff_reasons,
)
from agent.incident import Incident
from agent.reasoning_engine import build_reasoning_report


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    return value


def _top_reasons(incident: Incident) -> list[str]:
    return curate_reasons(incident.signals, max_reasons=5)


def _bullet_list(items: list[str]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {_display_text(item)}" for item in items]


def _business_metric_lines(incident: Incident) -> list[str]:
    for signal in incident.signals:
        if signal.component == "business_metrics":
            return _business_metric_lines_from_metadata(signal.metadata)
    return []


def _assumption_verification_lines(incident: Incident) -> list[str]:
    report = _assumption_verification_report(incident)
    checks = list((report or {}).get("checks") or [])
    if not checks:
        return []

    failed_checks = [
        check
        for check in checks
        if isinstance(check, dict) and str(check.get("status") or "") == "failed"
    ]
    if failed_checks:
        return [
            line
            for line in [_failed_assumption_check_line(check) for check in failed_checks]
            if line
        ]

    evaluated_count = _assumption_evaluated_count(report, checks)
    if evaluated_count > 0:
        return [
            f"{evaluated_count} {_plural(evaluated_count, 'check')} evaluated",
            "All assumption checks passed",
        ]

    check_count = _assumption_check_count(report, checks)
    return [
        (
            f"{check_count} {_plural(check_count, 'check')} generated "
            f"for {_assumption_kpi_label(checks)}"
        ),
        "0 checks evaluated",
        "Not evaluated: no warehouse connection provided",
    ]


def _assumption_verification_report(incident: Incident) -> dict:
    metadata = dict(incident.metadata or {})
    if isinstance(metadata.get("assumption_verification"), dict):
        return dict(metadata["assumption_verification"])
    semantic_context = metadata.get("semantic_context")
    if isinstance(semantic_context, dict) and isinstance(
        semantic_context.get("assumption_verification"),
        dict,
    ):
        return dict(semantic_context["assumption_verification"])
    return {}


def _failed_assumption_check_line(check: dict) -> str:
    if not isinstance(check, dict):
        return ""
    subject = _assumption_subject(check)
    count = check.get("violation_count")
    if count is None:
        return f"FAILED: {subject} {_assumption_check_label(check)}"

    check_type = str(check.get("check_type") or "")
    if check_type == "non_negative":
        return f"FAILED: {subject} has {count} negative {_plural(count, 'value')}"
    if check_type == "not_null":
        return f"FAILED: {subject} has {count} null {_plural(count, 'value')}"
    if check_type == "percentage_range":
        return f"FAILED: {subject} has {count} {_plural(count, 'value')} outside 0 to 100"
    if check_type == "model_not_empty":
        return f"FAILED: {subject} is empty"
    return f"FAILED: {subject} has {count} {_plural(count, 'violation')}"


def _assumption_subject(check: dict) -> str:
    model_name = str(check.get("model_name") or "model")
    column_name = check.get("column_name")
    return f"{model_name}.{column_name}" if column_name else model_name


def _assumption_check_count(report: dict, checks: list[dict]) -> int:
    metadata = dict((report or {}).get("metadata") or {})
    return int(metadata.get("check_count") or len(checks))


def _assumption_evaluated_count(report: dict, checks: list[dict]) -> int:
    metadata = dict((report or {}).get("metadata") or {})
    if metadata.get("evaluated_count") is not None:
        return int(metadata.get("evaluated_count") or 0)
    return len([
        check
        for check in checks
        if isinstance(check, dict) and bool(check.get("evaluated"))
    ])


def _assumption_kpi_label(checks: list[dict]) -> str:
    kpis = []
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            continue
        kpi_name = str(check.get("kpi_name") or "").strip()
        if not kpi_name or kpi_name in seen:
            continue
        seen.add(kpi_name)
        kpis.append(kpi_name)
    if not kpis:
        return "KPI"
    if len(kpis) == 1:
        return kpis[0]
    return f"{len(kpis)} KPIs"


def _plural(count, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


def _assumption_check_label(check: dict) -> str:
    check_type = str(check.get("check_type") or "")
    invariant = str(check.get("invariant") or "")
    if check_type == "model_not_empty":
        return "has rows"
    if check_type == "non_negative":
        return "never negative"
    if check_type == "percentage_range":
        return "between 0 and 100"
    if check_type == "not_null":
        return "not null"
    return invariant or check_type or "verified"


def _semantic_diff_signal(incident: Incident):
    for signal in incident.signals:
        if signal.component == "semantic_diff":
            return signal
    return None


def _outcome_memory_signal(incident: Incident):
    for signal in incident.signals:
        if signal.component == "deployment_outcomes":
            return signal
    return None


def _outcome_memory_lines(incident: Incident) -> list[str]:
    signal = _outcome_memory_signal(incident)
    if signal is None:
        return []
    return _ordered_unique(str(reason) for reason in list(signal.reasons or []) if reason)


def _column_lineage_lines(incident: Incident) -> list[str]:
    lines = []
    for signal in incident.signals:
        metadata = dict(signal.metadata or {})
        signal_column_lines = []
        for item in metadata.get("column_dependency_changes") or []:
            text = str(item)
            if is_column_level_reason(text) and not is_supporting_column_reason(text):
                signal_column_lines.append(text)
        signal_evidence_lines = [
            str(item)
            for item in metadata.get("column_level_evidence") or []
        ]
        lines.extend(signal_column_lines)
        if signal_column_lines or signal_evidence_lines:
            lines.extend(_lineage_summary_lines(signal))
        lines.extend(signal_evidence_lines)
    return _ordered_unique(line for line in lines if line)


def _semantic_diff_lines(signal) -> list[str]:
    metadata = dict(signal.metadata or {})
    lines = order_semantic_diff_reasons(list(signal.reasons or []))
    lines.extend([
        f"Changed KPIs: {_list_text(metadata.get('changed_kpis'))}",
        f"Added KPIs: {_list_text(metadata.get('added_kpis'))}",
        f"Removed KPIs: {_list_text(metadata.get('removed_kpis'))}",
    ])
    lines.extend(_change_lines("Dependency Changes", metadata.get("dependency_changes") or {}))
    lines.extend(_change_lines("Contract Changes", metadata.get("contract_changes") or {}))
    return lines


def _lineage_summary_lines(signal) -> list[str]:
    if signal.component != "semantic_diff":
        return []
    summaries = []
    for reason in signal.reasons or []:
        match = re.match(
            r"^(?P<kpi>.+?) gained upstream dependency (?P<dependency>.+)$",
            str(reason).strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        kpi = match.group("kpi").strip()
        dependency = match.group("dependency").strip()
        if "refund" in dependency.casefold():
            summaries.append(f"{kpi} lineage now includes refund-related data")
        else:
            summaries.append(f"{kpi} lineage now includes {dependency} data")
    return summaries


def _semantic_diff_snapshot_lines(signal) -> list[str]:
    metadata = dict(signal.metadata or {})
    return [
        f"Previous Snapshot: {_display_text(metadata.get('previous_snapshot_id') or 'None')}",
        f"Current Snapshot: {_display_text(metadata.get('current_snapshot_id') or 'None')}",
    ]


def _business_metric_lines_from_metadata(metadata: dict) -> list[str]:
    spike_percentages = metadata.get("spike_percentages") or {}
    if not spike_percentages:
        return ["Healthy"]
    return [
        f"{_business_metric_label(name)} +{_format_percentage(value)}"
        for name, value in spike_percentages.items()
    ]


def _business_metric_label(name: str) -> str:
    labels = {
        "mis_sorts": "Mis-sorts",
    }
    if name in labels:
        return labels[name]
    return " ".join(part.capitalize() for part in str(name).split("_"))


def _format_percentage(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _list_text(values: Any) -> str:
    items = [str(value) for value in list(values or [])]
    if not items:
        return "None"
    return ", ".join(items)


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _change_lines(label: str, changes: dict) -> list[str]:
    lines = []
    for kpi in sorted(changes):
        fields = changes.get(kpi) or {}
        for field_name in sorted(fields):
            detail = fields.get(field_name) or {}
            if not isinstance(detail, dict):
                lines.append(f"{label}: {kpi} {field_name} {_display_text(detail)}")
                continue
            for value in detail.get("added", []) or []:
                lines.append(f"{label}: {kpi} {field_name} added {value}")
            for value in detail.get("removed", []) or []:
                lines.append(f"{label}: {kpi} {field_name} removed {value}")
            if "previous" in detail or "current" in detail:
                lines.append(
                    f"{label}: {kpi} {field_name} changed from "
                    f"{_display_text(detail.get('previous'))} to "
                    f"{_display_text(detail.get('current'))}"
                )
    if not lines:
        return [f"{label}: None"]
    return lines


def _snapshot_value(line: str) -> str:
    return _display_text(line.split(": ", 1)[1] if ": " in line else line)


def render_cli(incident: Incident) -> str:
    reasoning = build_reasoning_report(incident)
    business_metric_lines = _business_metric_lines(incident)
    semantic_diff_signal = _semantic_diff_signal(incident)
    outcome_memory_lines = _outcome_memory_lines(incident)
    column_lineage_lines = _column_lineage_lines(incident)
    assumption_verification_lines = _assumption_verification_lines(incident)
    lines = [
        "Relium Deployment Decision",
        "",
        f"Pipeline Health: {_health_text(incident.health)}",
        f"Deployment Decision: {_decision_label(incident.decision)}",
        f"Severity: {_enum_value(incident.severity)}",
        f"Confidence: {_confidence_text(incident.confidence)}",
        "",
        f"Primary Root Cause: {_display_text(incident.root_cause or 'None')}",
        "",
        "Top Reasons:",
        *_bullet_list(_top_reasons(incident)),
        "",
        f"Recommendation: {_display_text(incident.recommendation or 'None')}",
        "",
        "Signals Considered:",
        *_bullet_list([signal.component for signal in incident.signals]),
    ]

    if outcome_memory_lines:
        lines.extend([
            "",
            "Deployment Outcome Memory",
            *_bullet_list(outcome_memory_lines),
        ])

    if business_metric_lines:
        lines.extend([
            "",
            "Business Metrics",
            *_bullet_list(business_metric_lines),
        ])

    if assumption_verification_lines:
        lines.extend([
            "",
            "Assumption Verification",
            *_bullet_list(assumption_verification_lines),
        ])

    if semantic_diff_signal is not None:
        lines.extend([
            "",
            "Historical Semantic Change",
            *_bullet_list([
                *_semantic_diff_lines(semantic_diff_signal),
                *_semantic_diff_snapshot_lines(semantic_diff_signal),
            ]),
        ])

    if column_lineage_lines:
        lines.extend([
            "",
            "Column-Level Lineage",
            *_bullet_list(column_lineage_lines),
        ])

    if incident.affected_models:
        lines.extend([
            "",
            "Affected Models:",
            *_bullet_list(list(incident.affected_models)),
        ])

    lines.extend([
        "",
        "Reasoning:",
        "",
        f"Executive Summary: {_display_text(reasoning.executive_summary)}",
        "",
        "Evidence:",
        *_format_cli_evidence(reasoning.evidence),
        "",
        f"Conclusion: {_display_text(reasoning.conclusion)}",
        "",
        f"Recommendation: {_display_text(reasoning.recommendation)}",
    ])

    return "\n".join(lines)


def render_markdown(incident: Incident) -> str:
    reasoning = build_reasoning_report(incident)
    business_metric_lines = _business_metric_lines(incident)
    semantic_diff_signal = _semantic_diff_signal(incident)
    outcome_memory_lines = _outcome_memory_lines(incident)
    column_lineage_lines = _column_lineage_lines(incident)
    assumption_verification_lines = _assumption_verification_lines(incident)
    lines = [
        "# Relium Deployment Decision",
        "",
        "## Pipeline Health",
        _health_text(incident.health),
        "",
        "## Deployment Decision",
        _decision_label(incident.decision),
        "",
        "## Severity",
        str(_enum_value(incident.severity)),
        "",
        "## Confidence",
        _confidence_text(incident.confidence),
        "",
        "## Primary Root Cause",
        _display_text(incident.root_cause or "None"),
        "",
        "## Top Reasons",
        *_bullet_list(_top_reasons(incident)),
        "",
        "## Recommendation",
        _display_text(incident.recommendation or "None"),
        "",
        "## Signals Considered",
        *_bullet_list([signal.component for signal in incident.signals]),
    ]

    if outcome_memory_lines:
        lines.extend([
            "",
            "## Deployment Outcome Memory",
            *_bullet_list(outcome_memory_lines),
        ])

    if business_metric_lines:
        lines.extend([
            "",
            "## Business Metrics",
            *_bullet_list(business_metric_lines),
        ])

    if assumption_verification_lines:
        lines.extend([
            "",
            "## Assumption Verification",
            *_bullet_list(assumption_verification_lines),
        ])

    if semantic_diff_signal is not None:
        previous_snapshot, current_snapshot = _semantic_diff_snapshot_lines(semantic_diff_signal)
        lines.extend([
            "",
            "### Historical Semantic Change",
            *_bullet_list(_semantic_diff_lines(semantic_diff_signal)),
            "",
            f"**Previous Snapshot:** {_snapshot_value(previous_snapshot)}  ",
            f"**Current Snapshot:** {_snapshot_value(current_snapshot)}",
        ])

    if column_lineage_lines:
        lines.extend([
            "",
            "## Column-Level Lineage",
            *_bullet_list(column_lineage_lines),
        ])

    if incident.affected_models:
        lines.extend([
            "",
            "## Affected Models",
            *_bullet_list(list(incident.affected_models)),
        ])

    lines.extend([
        "",
        "## Reasoning",
        "",
        "### Executive Summary",
        _display_text(reasoning.executive_summary),
        "",
        "### Evidence",
        *_format_markdown_evidence(reasoning.evidence),
        "",
        "### Conclusion",
        _display_text(reasoning.conclusion),
        "",
        "### Recommendation",
        _display_text(reasoning.recommendation),
    ])

    return "\n".join(lines)


def render_backtest_markdown(result: Any) -> str:
    incident = result.incident
    semantic_diff_signal = _semantic_diff_signal(incident)
    semantic_change_lines = _backtest_semantic_change_lines(semantic_diff_signal)
    column_lineage_lines = _column_lineage_lines(incident)
    assumption_verification_lines = _assumption_verification_lines(incident)

    lines = [
        "# Relium Backtest Result",
        "",
        "## Would Have Decided",
        _would_have_decision_label(result.would_have_decision),
        "",
        "## Historical Deployment",
        _display_text(result.historical_deployment_id),
        "",
        "## Pipeline Health",
        _health_text(result.would_have_health),
        "",
        "## Severity",
        str(_enum_value(incident.severity)),
        "",
        "## Confidence",
        _confidence_text(incident.confidence),
        "",
        "## Primary Root Cause",
        _display_text(incident.root_cause or "None"),
        "",
        "## What Relium Would Have Caught",
        *_bullet_list(_top_reasons(incident)),
        "",
        "## Semantic Change",
        *_bullet_list(semantic_change_lines),
        "",
        "## Column-Level Lineage",
        *_bullet_list(column_lineage_lines),
        "",
        "## Assumption Verification",
        *_bullet_list(assumption_verification_lines),
        "",
        "## Signals Considered",
        *_bullet_list([signal.component for signal in incident.signals]),
        "",
        "## Summary",
        _backtest_summary(result, semantic_diff_signal),
    ]
    return "\n".join(lines)


def render_backtest_cli(result: Any) -> str:
    incident = result.incident
    semantic_diff_signal = _semantic_diff_signal(incident)
    semantic_change_lines = _backtest_semantic_change_lines(semantic_diff_signal)
    lines = [
        "Relium Backtest Result",
        "",
        f"Would Have Decided: {_would_have_decision_label(result.would_have_decision)}",
        f"Historical Deployment: {_display_text(result.historical_deployment_id)}",
        f"Pipeline Health: {_health_text(result.would_have_health)}",
        f"Severity: {_enum_value(incident.severity)}",
        f"Confidence: {_confidence_text(incident.confidence)}",
        "",
        f"Primary Root Cause: {_display_text(incident.root_cause or 'None')}",
        "",
        "What Relium Would Have Caught:",
        *_bullet_list(_top_reasons(incident)),
        "",
        "Semantic Change:",
        *_bullet_list(semantic_change_lines),
        "",
        "Column-Level Lineage:",
        *_bullet_list(_column_lineage_lines(incident)),
        "",
        "Assumption Verification:",
        *_bullet_list(_assumption_verification_lines(incident)),
        "",
        "Signals Considered:",
        *_bullet_list([signal.component for signal in incident.signals]),
        "",
        f"Summary: {_backtest_summary(result, semantic_diff_signal)}",
    ]
    return "\n".join(lines)


def _backtest_semantic_change_lines(signal) -> list[str]:
    if signal is None:
        return []
    metadata = dict(signal.metadata or {})
    lines = order_semantic_diff_reasons(list(signal.reasons or []))
    lines.extend(_change_lines("Dependency Changes", metadata.get("dependency_changes") or {}))
    lines.extend(_change_lines("Contract Changes", metadata.get("contract_changes") or {}))
    return [
        line
        for line in _ordered_unique(lines)
        if not str(line).endswith(": None")
    ]


def _would_have_decision_label(decision: Any) -> str:
    value = str(_enum_value(decision)).upper()
    if value == DeploymentDecision.BLOCK.value:
        return "WOULD BLOCK"
    if value == DeploymentDecision.WARN.value:
        return "WOULD WARN"
    if value == DeploymentDecision.ALLOW.value:
        return "WOULD ALLOW"
    return f"WOULD {value}"


def _backtest_summary(result: Any, semantic_diff_signal) -> str:
    action = _backtest_action(result.would_have_decision)
    root_cause = _display_text(result.incident.root_cause or "the deployment risk changed")
    if semantic_diff_signal is not None:
        return (
            f"Relium would have {action} this deployment before production "
            f"because {root_cause} and the semantic meaning of the KPI changed."
        )
    return (
        f"Relium would have {action} this deployment before production "
        f"because {root_cause}."
    )


def _backtest_action(decision: Any) -> str:
    value = str(_enum_value(decision)).upper()
    if value == DeploymentDecision.BLOCK.value:
        return "blocked"
    if value == DeploymentDecision.WARN.value:
        return "warned on"
    if value == DeploymentDecision.ALLOW.value:
        return "allowed"
    return f"flagged as {value}"


def render_json(incident: Incident) -> dict:
    return {
        "incident_id": incident.incident_id,
        "health": incident.health,
        "decision": _enum_value(incident.decision),
        "severity": _enum_value(incident.severity),
        "confidence": incident.confidence,
        "root_cause": incident.root_cause,
        "recommendation": incident.recommendation,
        "signal_count": len(incident.signals),
        "signal_components": [
            signal.component for signal in incident.signals
        ],
        "top_reasons": _top_reasons(incident),
        "affected_models": list(incident.affected_models),
        "metadata": _serialize(dict(incident.metadata)),
    }


def _format_cli_evidence(evidence) -> list[str]:
    if not evidence:
        return ["- None"]
    return [
        (
            f"- {_display_text(item.title)} "
            f"(severity: {item.severity}, confidence: {item.confidence})"
        )
        for item in evidence
    ]


def _format_markdown_evidence(evidence) -> list[str]:
    if not evidence:
        return ["- None"]
    return [
        (
            f"- **{_display_text(item.title)}** "
            f"(severity: {item.severity}, confidence: {item.confidence})"
        )
        for item in evidence
    ]


def _health_text(health: int) -> str:
    return f"{health} / 100"


def _confidence_text(confidence: int) -> str:
    return f"{confidence}%"


def _decision_label(decision: Any) -> str:
    value = _enum_value(decision)
    if value == DeploymentDecision.BLOCK.value:
        return "BLOCK DEPLOYMENT"
    return str(value)


def _display_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=with\b)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()
