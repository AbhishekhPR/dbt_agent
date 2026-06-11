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
        "why": (
            "A LEFT JOIN should preserve rows from the left table. Filtering the "
            "right-side table in the WHERE clause can remove unmatched rows and "
            "silently change the business meaning of the model."
        ),
        "recommendation": (
            "Move the right-table filter into the JOIN condition or explicitly "
            "allow NULLs."
        ),
    },
    "select_star_risk": {
        "why": "New upstream columns can silently change downstream schemas.",
        "recommendation": "Select explicit columns.",
    },
    "cross_join_risk": {
        "why": "Cartesian products can multiply rows and inflate metrics.",
        "recommendation": "Confirm this is intentional or replace it with a keyed join.",
    },
    "join_without_condition_risk": {
        "why": "A join without ON or USING can multiply rows unexpectedly.",
        "recommendation": "Add an explicit ON or USING condition.",
    },
    "division_by_zero_risk": {
        "why": "Ratios can return NULLs or fail when the denominator is zero.",
        "recommendation": "Use NULLIF around the denominator.",
    },
    "hardcoded_date_filter_risk": {
        "why": "Fixed date filters can silently exclude valid data over time.",
        "recommendation": "Confirm the filter is intentional or parameterize it.",
    },
    "not_equal_filter_risk": {
        "why": "NOT EQUAL filters exclude NULLs unless NULLs are handled explicitly.",
        "recommendation": "Confirm NULL handling or use explicit logic.",
    },
}


def run_pr_guard(
    project_path: str,
    changed_files: list[str] | None = None,
    fail_on: str = "high",
    output: str = ".relium/pr_guard_report.md",
) -> dict:
    project = Path(project_path)
    scanned_files = sql_files_to_scan(project_path, changed_files)
    risks = detect_sql_risks(project_path, persist=False, changed_files=changed_files)
    enriched = [_enrich_risk(project, risk) for risk in risks]

    highest = _highest_severity(enriched)
    safe_to_merge = not _has_blocking_risk(enriched, fail_on)
    report = {
        "project": project_path,
        "files_scanned": len(scanned_files),
        "risks_found": len(enriched),
        "highest_severity": highest,
        "safe_to_merge": safe_to_merge,
        "risks": enriched,
        "output": output,
        "exit_code": 0 if safe_to_merge else 1,
    }

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(report), encoding="utf-8")
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
                f"Evidence: {risk['evidence']}",
                f"Why it matters: {risk['why_it_matters']}",
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
            "",
            f"Report written to {report['output']}",
        ]
    )


def _enrich_risk(project: Path, risk: dict) -> dict:
    risk_type = risk.get("risk_type", "")
    context = RISK_CONTEXT.get(
        risk_type,
        {
            "why": "This pattern can make SQL transformation behavior harder to review.",
            "recommendation": risk.get("recommendation", "Review this SQL carefully."),
        },
    )
    return {
        **risk,
        "why_it_matters": context["why"],
        "recommendation": context["recommendation"],
        "suggested_fix": _suggested_fix(project, risk),
        "affected_downstream_models": _affected_downstream_models(project, risk["model"]),
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

    return f"LEFT JOIN {join['table']} {join['alias']}\nON {join['on']}\nAND {condition}"


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


def _bullet_lines(items: Iterable[str]) -> list[str]:
    values = list(items)
    if not values:
        return ["- None found"]
    return [f"- {item}" for item in values]


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
