import re
from pathlib import Path
from typing import Iterable

from agent.blast_radius import calculate_blast_radius
from agent.sql_metadata_extractor import strip_jinja
from agent.sql_risk_detector import detect_sql_risks, sql_files_to_scan


SEVERITY_ORDER = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

RISK_CONTEXT = {
    "left_join_filter_risk": {
        "confidence": 0.95,
        "why": (
            "A LEFT JOIN should preserve rows from the left table. Filtering the "
            "right-side table in the WHERE clause can remove unmatched rows and "
            "silently change the business meaning of the model."
        ),
        "business_impact": (
            "This change may silently remove valid rows from the left-side table. "
            "Metrics such as customer lifetime value, revenue, order counts, daily "
            "KPIs, and dashboard totals may become undercounted."
        ),
        "recommendation": (
            "Move the right-table filter into the JOIN condition or explicitly "
            "allow NULLs."
        ),
    },
    "select_star_risk": {
        "confidence": 0.85,
        "why": "New upstream columns can silently change downstream schemas.",
        "business_impact": "Downstream models and dashboards may receive unexpected columns or schema changes.",
        "recommendation": "Select explicit columns.",
    },
    "cross_join_risk": {
        "confidence": 0.9,
        "why": "Cartesian products can multiply rows and inflate metrics.",
        "business_impact": "Metrics based on row counts, revenue, orders, or customer activity may be overstated.",
        "recommendation": "Confirm this is intentional or replace it with a keyed join.",
    },
    "join_without_condition_risk": {
        "confidence": 0.9,
        "why": "A join without ON or USING can multiply rows unexpectedly.",
        "business_impact": "Metrics can be inflated by unintended row multiplication.",
        "recommendation": "Add an explicit ON or USING condition.",
    },
    "division_by_zero_risk": {
        "confidence": 0.8,
        "why": "Ratios can return NULLs or fail when the denominator is zero.",
        "business_impact": "Ratio metrics may fail, disappear, or show misleading blanks in reports.",
        "recommendation": "Use NULLIF around the denominator.",
    },
    "hardcoded_date_filter_risk": {
        "confidence": 0.8,
        "why": "Fixed date filters can silently exclude valid data over time.",
        "business_impact": "Time-based metrics may become stale or undercount newer business activity.",
        "recommendation": "Confirm the filter is intentional or parameterize it.",
    },
    "not_equal_filter_risk": {
        "confidence": 0.8,
        "why": "NOT EQUAL filters exclude NULLs unless NULLs are handled explicitly.",
        "business_impact": "Rows with unknown or missing values may be silently excluded from metrics.",
        "recommendation": "Confirm NULL handling or use explicit logic.",
    },
}


def run_pr_guard(
    project_path: str,
    changed_files: list[str] | None = None,
    fail_on: str = "high",
    output: str = ".relium/pr_guard_report.md",
    github_comment: bool = False,
    comment_output: str = ".relium/pr_guard_comment.md",
) -> dict:
    project = Path(project_path)
    scanned_files = sql_files_to_scan(project_path, changed_files)
    risks = detect_sql_risks(project_path, persist=False, changed_files=changed_files)
    enriched = [_enrich_risk(project, risk) for risk in risks]

    highest = _highest_severity(enriched)
    safe_to_merge = not _has_blocking_risk(enriched, fail_on)
    merge_decision = _merge_decision(enriched, fail_on, safe_to_merge)
    report = {
        "project": project_path,
        "files_scanned": len(scanned_files),
        "risks_found": len(enriched),
        "highest_severity": highest,
        "safe_to_merge": safe_to_merge,
        "merge_decision": merge_decision,
        "risks": enriched,
        "output": output,
        "exit_code": 0 if safe_to_merge else 1,
    }

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(report), encoding="utf-8")
    if github_comment:
        from agent.github_pr_commenter import post_or_update_pr_comment, render_pr_comment, write_pr_comment

        comment_body = render_pr_comment(report)
        write_pr_comment(report, comment_output)
        report["comment_output"] = comment_output
        report["github_comment_status"] = post_or_update_pr_comment(comment_body)
    return report


def render_report(report: dict) -> str:
    safe = "YES" if report["safe_to_merge"] else "NO"
    lines = [
        "# Relium PR Guard Report",
        "",
        "## Summary",
        "",
        f"* Project: {report['project']}",
        f"* Files scanned: {report['files_scanned']}",
        f"* Risks found: {report['risks_found']}",
        f"* Highest severity: {report['highest_severity']}",
        f"* Safe to merge: {safe}",
        f"* Merge decision: {report['merge_decision']}",
        "",
        "## Risks",
        "",
    ]

    if not report["risks"]:
        lines.append("No SQL transformation risks found.")
        lines.append("")
        return "\n".join(lines)

    for risk in report["risks"]:
        lines.extend(
            [
                f"### [{risk['severity'].upper()}] {risk['model']}",
                "",
                f"File: {risk['file']}",
                f"Risk: {_sentence(risk['message'])}",
                f"Confidence: {_format_confidence(risk['confidence'])}",
                f"Impact Level: {risk['impact_level']}",
                f"Blast Radius Score: {risk['blast_radius_score']}/10",
                f"Evidence: {risk['evidence']}",
                f"Why it matters: {risk['why_it_matters']}",
                "Business impact:",
                risk["business_impact"],
                f"Recommended Action: {risk['recommended_action']}",
                f"Recommendation: {risk['recommendation']}",
                "Suggested fix:",
                "",
                "```sql",
                risk["suggested_fix"],
                "```",
                "",
                "Affected downstream models:",
                *_bullet_lines(risk["affected_downstream_models"]),
                "",
            ]
        )

    return "\n".join(lines)


def terminal_summary(report: dict) -> str:
    safe = "YES" if report["safe_to_merge"] else "NO"
    return "\n".join(
        [
            "Relium PR Guard",
            "",
            f"Project: {report['project']}",
            f"Files scanned: {report['files_scanned']}",
            f"Risks found: {report['risks_found']}",
            f"Highest severity: {report['highest_severity']}",
            f"Safe to merge: {safe}",
            f"Merge decision: {report['merge_decision']}",
            "",
            f"Report written to {report['output']}",
        ]
    )


def _enrich_risk(project: Path, risk: dict) -> dict:
    risk_type = risk.get("risk_type", "")
    context = RISK_CONTEXT.get(
        risk_type,
        {
            "confidence": 0.75,
            "why": "This pattern can make SQL transformation behavior harder to review.",
            "business_impact": "This pattern may affect downstream model correctness and should be reviewed carefully.",
            "recommendation": risk.get("recommendation", "Review this SQL carefully."),
        },
    )
    affected = _affected_downstream_models(project, risk["model"])
    blast_radius_score = _blast_radius_score(affected, risk)
    impact_level = _impact_level(blast_radius_score)
    return {
        **risk,
        "confidence": context["confidence"],
        "why_it_matters": context["why"],
        "business_impact": context["business_impact"],
        "recommendation": context["recommendation"],
        "impact_level": impact_level,
        "blast_radius_score": blast_radius_score,
        "recommended_action": _recommended_action(risk, affected),
        "suggested_fix": _suggested_fix(project, risk),
        "affected_downstream_models": affected,
    }


def _suggested_fix(project: Path, risk: dict) -> str:
    if risk.get("risk_type") != "left_join_filter_risk":
        return "No automated fix available."

    sql_path = project / risk["file"]
    if not sql_path.exists():
        return "Move the right-table filter into the JOIN condition."

    sql = _normalize_sql(strip_jinja(sql_path.read_text(encoding="utf-8")))
    alias_match = re.search(r"\bWHERE\s+([\w\"`]+)\.", risk.get("evidence", ""), re.IGNORECASE)
    if not alias_match:
        return "Move the right-table filter into the JOIN condition."

    alias = alias_match.group(1).strip('"`')
    join = _left_join_for_alias(sql, alias)
    condition = _where_condition_for_alias(sql, alias)
    if not join or not condition:
        return "Move the right-table filter into the JOIN condition."

    return f"LEFT JOIN {join['table']} {join['alias']}\n    ON {join['on']}\n   AND {condition}"


def _affected_downstream_models(project: Path, model_name: str) -> list[str]:
    blast = calculate_blast_radius(str(project), model_name)
    affected: list[str] = []
    for section in ("directly_affected", "indirectly_affected"):
        for item in blast.get(section, []):
            affected.append(item["model"])
    return list(dict.fromkeys(affected))


def _highest_severity(risks: list[dict]) -> str:
    if not risks:
        return "NONE"
    return max(
        (risk["severity"].lower() for risk in risks),
        key=lambda severity: SEVERITY_ORDER.get(severity, -1),
    ).upper()


def _has_blocking_risk(risks: list[dict], fail_on: str) -> bool:
    threshold = SEVERITY_ORDER[fail_on.lower()]
    return any(SEVERITY_ORDER.get(risk["severity"].lower(), 0) >= threshold for risk in risks)


def _merge_decision(risks: list[dict], fail_on: str, safe_to_merge: bool) -> str:
    threshold = fail_on.lower()
    if not safe_to_merge:
        blocking = [
            risk["severity"].lower()
            for risk in risks
            if SEVERITY_ORDER.get(risk["severity"].lower(), 0) >= SEVERITY_ORDER[threshold]
        ]
        severity = max(blocking, key=lambda item: SEVERITY_ORDER.get(item, -1)).upper()
        return f"Blocked because {severity} risk transformation logic was detected."

    if threshold == "critical":
        return "Allowed because no CRITICAL risks were detected."
    if threshold == "high":
        return "Allowed because no HIGH or CRITICAL risks were detected."
    severity_names = [
        name.upper()
        for name, rank in SEVERITY_ORDER.items()
        if rank >= SEVERITY_ORDER[threshold]
    ]
    return f"Allowed because no {'/'.join(severity_names)} risks were detected."


def _bullet_lines(items: Iterable[str]) -> list[str]:
    values = list(items)
    if not values:
        return ["- None found"]
    return [f"- {item}" for item in values]


def _format_confidence(confidence: float) -> str:
    return f"{round(confidence * 100)}%"


def _impact_level(blast_radius_score: int) -> str:
    if blast_radius_score <= 3:
        return "LOW"
    if blast_radius_score <= 6:
        return "MEDIUM"
    if blast_radius_score <= 9:
        return "HIGH"
    return "CRITICAL"


def _blast_radius_score(downstream_models: list[str], risk: dict) -> int:
    score = len(downstream_models) * 2
    lower_models = [model.lower() for model in downstream_models]
    if any("dashboard" in model for model in lower_models):
        score += 3
    if any("executive" in model for model in lower_models):
        score += 2
    if score == 0 and risk.get("severity", "").lower() == "high":
        score = 1
    return min(score, 10)


def _recommended_action(risk: dict, downstream_models: list[str]) -> str:
    severity = risk.get("severity", "").lower()
    if severity == "critical":
        return "Block merge until fixed."
    if severity == "high" and risk.get("risk_type") == "left_join_filter_risk":
        if downstream_models:
            return (
                "Fix before merge. This risky transformation may silently remove records "
                "and affect downstream business models."
            )
        return (
            "Review before merge. No downstream models were found, but this SQL pattern can silently "
            "change row preservation behavior."
        )
    return "Review and document if intentional."


def _sentence(text: str) -> str:
    text = text.rstrip(".")
    return f"{text}."


def _normalize_sql(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"\s+", " ", sql).strip()


def _left_join_for_alias(sql: str, alias: str) -> dict | None:
    pattern = re.compile(
        rf"\bLEFT(?:\s+OUTER)?\s+JOIN\s+([\w\"`\.]+)\s+(?:AS\s+)?({re.escape(alias)})\s+\bON\b\s+"
        r"(.*?)(?=\b(?:LEFT|RIGHT|INNER|FULL|OUTER|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        re.IGNORECASE,
    )
    match = pattern.search(sql)
    if not match:
        return None
    return {
        "table": match.group(1).strip('"`'),
        "alias": match.group(2).strip('"`'),
        "on": match.group(3).strip(" ;"),
    }


def _where_condition_for_alias(sql: str, alias: str) -> str | None:
    match = re.search(
        r"\bWHERE\b\s+(.*?)(?=\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE,
    )
    if not match:
        return None
    conditions = re.split(r"\s+AND\s+", match.group(1), flags=re.IGNORECASE)
    alias_pattern = re.compile(rf"\b{re.escape(alias)}\.", re.IGNORECASE)
    for condition in conditions:
        cleaned = condition.strip(" ();")
        if alias_pattern.search(cleaned):
            return cleaned
    return None
