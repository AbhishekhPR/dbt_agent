import re
from pathlib import Path
from typing import Any

from agent.signals import Severity, Signal
from agent.sql_analyzer import analyze_sql_logic


RULE_RECOMMENDATIONS = {
    "SELECT_STAR": "Replace SELECT * with explicit column selection for the fields this model actually needs.",
    "MISSING_JOIN_ON": "Add an explicit ON clause or use CROSS JOIN intentionally.",
    "LEFT_JOIN_NULLIFIED": "Move right-side filters into JOIN clauses or preserve NULL rows explicitly.",
    "COUNT_AFTER_JOIN": "Use COUNT(DISTINCT key) or deduplicate upstream before aggregating.",
    "MISSING_OR_RENAMED_COLUMNS": "Verify upstream schema and update column names or mappings.",
}


RULE_IDS = {
    "select_star": "SELECT_STAR",
    "missing_join_on": "MISSING_JOIN_ON",
    "left_join_nullified_by_where": "LEFT_JOIN_NULLIFIED",
    "count_after_join": "COUNT_AFTER_JOIN",
    "missing_or_renamed_columns": "MISSING_OR_RENAMED_COLUMNS",
}


SEVERITY_SCORES = {
    "critical": -50,
    "high": -35,
    "medium": -15,
    "low": 0,
    "clean": 0,
}


def run_ast_analysis(sql: str, model_name: str, dialect: str | None = None) -> dict:
    report = analyze_sql_logic(model_name, sql or "")
    bugs = [_bug_from_finding(finding) for finding in report.get("findings", [])]
    return {
        "model_name": model_name,
        "dialect": dialect,
        "overall_risk": report.get("overall_risk", "clean"),
        "summary": report.get("summary", "Found 0 potential issue(s)"),
        "bugs": bugs,
        "safe_to_run": report.get("safe_to_run", True),
        "data_loss_risk": any(bug["severity"] in {"critical", "high"} for bug in bugs),
        "findings": list(report.get("findings", [])),
    }


def analyze_all_models_ast(project_path: str, dialect: str = "sqlite") -> list[dict]:
    models_path = Path(project_path) / "models"
    if not models_path.exists():
        return []
    reports = []
    for sql_file in sorted(models_path.glob("**/*.sql")):
        reports.append(
            run_ast_analysis(
                sql_file.read_text(encoding="utf-8"),
                sql_file.stem,
                dialect=dialect,
            )
        )
    return reports


def strip_jinja(sql: str) -> str:
    text = str(sql or "")
    text = re.sub(
        r"\{\{\s*ref\(['\"]([^'\"]+)['\"]\)\s*\}\}",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\{\{\s*source\(['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\)\s*\}\}",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\{#.*?#\}", " ", text, flags=re.DOTALL)
    text = re.sub(r"\{[%{].*?[%}]\}", " ", text, flags=re.DOTALL)
    return text


def to_signal(report: dict[str, Any]) -> Signal:
    risk = str(report.get("overall_risk") or "clean").lower()
    severity = _severity(risk)
    bugs = list(report.get("bugs") or [])
    reasons = [
        bug.get("description") or bug.get("category") or "SQL logic risk detected"
        for bug in bugs
    ]
    if not reasons and risk in {"clean", "low"}:
        reasons = ["No SQL logic risks detected"]
    return Signal(
        component="ast",
        severity=severity,
        confidence=90 if bugs else 75,
        score=SEVERITY_SCORES.get(risk, 0),
        reasons=reasons,
        metadata={
            "model_name": report.get("model_name"),
            "overall_risk": report.get("overall_risk", "clean"),
            "bug_count": len(bugs),
            "bugs": bugs,
        },
    )


def _bug_from_finding(finding: dict) -> dict:
    rule = RULE_IDS.get(finding.get("rule_id"), str(finding.get("rule_id") or "UNCLASSIFIED").upper())
    return {
        "rule": rule,
        "category": finding.get("title", rule),
        "severity": str(finding.get("severity", "low")).lower(),
        "description": finding.get("why_it_matters") or finding.get("title") or rule,
        "recommendation": finding.get("recommendation") or RULE_RECOMMENDATIONS.get(rule, "Review the affected model."),
        "fix": finding.get("recommendation") or RULE_RECOMMENDATIONS.get(rule, "Review the affected model."),
        "impact": finding.get("why_it_matters") or "",
        "line_reference": finding.get("evidence") or "",
        "confidence": finding.get("confidence", "medium"),
    }


def _severity(risk: str) -> Severity:
    if risk == "critical":
        return Severity.CRITICAL
    if risk == "high":
        return Severity.HIGH
    if risk == "medium":
        return Severity.MEDIUM
    return Severity.LOW
