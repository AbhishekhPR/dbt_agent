import re
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from agent.signals import Severity, Signal
from agent.sql_analyzer import analyze_sql_logic


RULE_RECOMMENDATIONS = {
    "SELECT_STAR": "Replace SELECT * with explicit column selection for the fields this model actually needs.",
    "MISSING_JOIN_ON": "Add an explicit ON clause or use CROSS JOIN intentionally.",
    "LEFT_JOIN_NULLIFIED": "Move right-side filters into JOIN clauses or preserve NULL rows explicitly.",
    "COUNT_AFTER_JOIN": "Use COUNT(DISTINCT key) or deduplicate upstream before aggregating.",
    "MISSING_OR_RENAMED_COLUMNS": "Verify upstream schema and update column names or mappings.",
    "DIVISION_BY_ZERO": "Wrap the denominator in NULLIF(denominator, 0) or add an explicit CASE guard.",
    "INTEGER_DIVISION": "Cast an operand to a floating-point type or multiply the numerator by 1.0.",
    "HARDCODED_DATE_FILTER": "Confirm the date is intentional or parameterize it (e.g. current_date - interval).",
    "NOT_EQUAL_NULL_RISK": "Add an explicit OR column IS NULL clause or use IS DISTINCT FROM instead of != / <>.",
}


RULE_IDS = {
    "select_star": "SELECT_STAR",
    "missing_join_on": "MISSING_JOIN_ON",
    "left_join_nullified_by_where": "LEFT_JOIN_NULLIFIED",
    "count_after_join": "COUNT_AFTER_JOIN",
    "missing_or_renamed_columns": "MISSING_OR_RENAMED_COLUMNS",
    "division_by_zero": "DIVISION_BY_ZERO",
    "integer_division": "INTEGER_DIVISION",
    "hardcoded_date_filter": "HARDCODED_DATE_FILTER",
    "not_equal_null_risk": "NOT_EQUAL_NULL_RISK",
}


SEVERITY_SCORES = {
    "critical": -50,
    "high": -35,
    "medium": -15,
    "low": 0,
    "clean": 0,
}


def run_ast_analysis(sql: str, model_name: str, dialect: str | None = None) -> dict:
    sql = sql or ""
    report = analyze_sql_logic(model_name, sql)
    findings = list(report.get("findings", [])) + _additional_findings(
        sql,
        dialect=dialect,
    )
    bugs = [_bug_from_finding(finding) for finding in findings]
    overall_risk = _overall_risk(findings)
    return {
        "model_name": model_name,
        "dialect": dialect,
        "overall_risk": overall_risk,
        "summary": f"Found {len(findings)} potential issue(s)",
        "bugs": bugs,
        "safe_to_run": overall_risk in ("low", "clean"),
        "data_loss_risk": any(bug["severity"] in {"critical", "high"} for bug in bugs),
        "findings": findings,
    }


def _overall_risk(findings: list[dict]) -> str:
    if any(finding["severity"] == "critical" for finding in findings):
        return "critical"
    if any(finding["severity"] == "high" for finding in findings):
        return "high"
    if findings:
        return "medium"
    return "clean"


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


_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_WHERE_CLAUSE_RE = re.compile(
    r"\bwhere\b(.*?)(?:\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_CASE_ZERO_GUARD_TEMPLATE = r"when\s+{}\s*(?:=\s*0|<=\s*0)\b"
_DATE_FILTER_RE = re.compile(
    r"[\w\.\"]+\s*(?:=|<>|!=|>=|<=|>|<)\s*(?:date\s*|timestamp\s*)?'(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_NOT_EQUAL_RE = re.compile(r"([\w\.\"]+)\s*(?:!=|<>)\s*", re.IGNORECASE)


def _additional_findings(
    sql: str,
    *,
    dialect: str | None = None,
) -> list[dict]:
    findings = []
    for finder in (
        lambda value: _division_by_zero_finding(value, dialect=dialect),
        lambda value: _integer_division_finding(value, dialect=dialect),
        _hardcoded_date_filter_finding,
        _not_equal_null_risk_finding,
    ):
        finding = finder(sql)
        if finding is not None:
            findings.append(finding)
    return findings


def _extract_where_clause(sql: str) -> str:
    match = _WHERE_CLAUSE_RE.search(sql)
    return match.group(1) if match else ""


def _is_safe_numeric_literal(token: str) -> bool:
    try:
        return float(token) != 0
    except ValueError:
        return False


def _has_case_zero_guard(sql: str, denominator: str) -> bool:
    pattern = re.compile(_CASE_ZERO_GUARD_TEMPLATE.format(re.escape(denominator)), re.IGNORECASE)
    return bool(pattern.search(sql))


def _find_division_by_zero_risk(
    sql: str,
    *,
    dialect: str | None = None,
) -> list[str]:
    without_comments = _COMMENT_RE.sub(" ", sql)
    tree = _parse_sql_tree(without_comments, dialect=dialect)
    if tree is None:
        return []

    risky = []
    for division in tree.find_all(exp.Div):
        denominator = division.right
        denominator_sql = denominator.sql()
        if "NULLIF" in denominator_sql.upper():
            continue
        if isinstance(denominator, exp.Literal) and _is_safe_numeric_literal(
            str(denominator.this)
        ):
            continue
        if _has_case_zero_guard(without_comments, denominator_sql):
            continue
        risky.append(division.sql())
    return risky


def _division_by_zero_finding(
    sql: str,
    *,
    dialect: str | None = None,
) -> dict | None:
    risky = _find_division_by_zero_risk(sql, dialect=dialect)
    if not risky:
        return None
    return {
        "rule_id": "division_by_zero",
        "severity": "high",
        "title": "Division without a zero-safe guard",
        "evidence": "; ".join(risky),
        "why_it_matters": (
            "Dividing by a column or expression that can be zero raises a runtime "
            "error on some engines or silently returns NULL on others, corrupting "
            "downstream ratios and metrics."
        ),
        "recommendation": RULE_RECOMMENDATIONS["DIVISION_BY_ZERO"],
        "confidence": "medium",
    }


def _integer_division_finding(
    sql: str,
    *,
    dialect: str | None = None,
) -> dict | None:
    tree = _parse_sql_tree(sql, dialect=dialect)
    if tree is None:
        return None

    risky = []
    for division in tree.find_all(exp.Div):
        expression_sql = division.sql()
        normalized = expression_sql.upper()
        if "NULLIF" in normalized:
            continue
        if any(marker in normalized for marker in ("1.0", "100.0", "FLOAT", "CAST")):
            continue
        if not isinstance(division.left, (exp.Sum, exp.Count)) and not isinstance(
            division.right,
            (exp.Sum, exp.Count),
        ):
            continue
        risky.append(expression_sql)

    if not risky:
        return None
    return {
        "rule_id": "integer_division",
        "severity": "medium",
        "title": "Integer division may truncate decimal values",
        "evidence": "; ".join(dict.fromkeys(risky)),
        "why_it_matters": (
            "When both aggregate operands are integers, SQL engines can truncate "
            "the decimal portion of averages, rates, and percentages."
        ),
        "recommendation": RULE_RECOMMENDATIONS["INTEGER_DIVISION"],
        "confidence": "medium",
    }


def _parse_sql_tree(
    sql: str,
    *,
    dialect: str | None = None,
) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(strip_jinja(sql), read=dialect)
    except (sqlglot.errors.ParseError, ValueError):
        return None


def _find_hardcoded_date_filters(sql: str) -> list[str]:
    without_comments = _COMMENT_RE.sub(" ", sql)
    where_clause = _extract_where_clause(without_comments)
    if not where_clause:
        return []
    return [match.group(1) for match in _DATE_FILTER_RE.finditer(where_clause)]


def _hardcoded_date_filter_finding(sql: str) -> dict | None:
    dates = _find_hardcoded_date_filters(sql)
    if not dates:
        return None
    return {
        "rule_id": "hardcoded_date_filter",
        "severity": "medium",
        "title": "Hardcoded date literal in filter",
        "evidence": f"WHERE clause compares against fixed date(s): {', '.join(sorted(set(dates)))}",
        "why_it_matters": (
            "A fixed date filter silently stops matching new data as time passes, "
            "quietly excluding valid rows without any error."
        ),
        "recommendation": RULE_RECOMMENDATIONS["HARDCODED_DATE_FILTER"],
        "confidence": "medium",
    }


def _has_null_guard(where_clause: str, column: str) -> bool:
    pattern = re.compile(rf"{re.escape(column)}\s+is\s+null", re.IGNORECASE)
    return bool(pattern.search(where_clause))


def _find_not_equal_null_risk(sql: str) -> list[str]:
    without_comments = _COMMENT_RE.sub(" ", sql)
    where_clause = _extract_where_clause(without_comments)
    if not where_clause:
        return []
    risky = []
    for match in _NOT_EQUAL_RE.finditer(where_clause):
        column = match.group(1).strip()
        if _has_null_guard(where_clause, column):
            continue
        risky.append(column)
    return risky


def _not_equal_null_risk_finding(sql: str) -> dict | None:
    columns = _find_not_equal_null_risk(sql)
    if not columns:
        return None
    return {
        "rule_id": "not_equal_null_risk",
        "severity": "high",
        "title": "Not-equal filter may silently exclude NULL rows",
        "evidence": f"WHERE clause uses != / <> on: {', '.join(sorted(set(columns)))} without a NULL guard",
        "why_it_matters": (
            "Under SQL three-valued logic, `NULL != value` evaluates to UNKNOWN, "
            "so rows with NULL in that column are silently dropped from the result."
        ),
        "recommendation": RULE_RECOMMENDATIONS["NOT_EQUAL_NULL_RISK"],
        "confidence": "medium",
    }


def _severity(risk: str) -> Severity:
    if risk == "critical":
        return Severity.CRITICAL
    if risk == "high":
        return Severity.HIGH
    if risk == "medium":
        return Severity.MEDIUM
    return Severity.LOW
