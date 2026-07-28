from agent.redaction import redact_text


_CUSTOMER_IMPACTS = {
    "DIVISION_BY_ZERO": (
        "The denominator may be zero, causing an error or NULL result."
    ),
    "INTEGER_DIVISION": (
        "Rates, averages, and percentages may lose their decimal portion."
    ),
    "NOT_EQUAL_NULL_RISK": (
        "Rows with NULL values may be removed unintentionally."
    ),
}

_CUSTOMER_FIXES = {
    "DIVISION_BY_ZERO": (
        "Use `NULLIF(denominator, 0)` or an explicit `CASE` guard."
    ),
    "INTEGER_DIVISION": "Cast one operand to `DECIMAL` or `FLOAT`.",
    "NOT_EQUAL_NULL_RISK": (
        "Handle NULL explicitly or use `IS DISTINCT FROM`."
    ),
}


def render_review_comment(result: dict) -> str:
    decision = str(result.get("decision") or "UNKNOWN").upper()
    if decision == "SKIPPED":
        return str(result.get("rendered", {}).get("markdown", "")).strip()

    incident = dict(result.get("incident") or {})
    severity = str(incident.get("severity") or "LOW").title()
    models = _affected_models(result, incident)
    findings = list(result.get("material_findings") or [])[:3]

    lines = [f"### Relium PR Guard — {redact_text(decision)}", ""]
    if findings:
        lines.extend(
            [
                _risk_summary(models),
                "",
                _reason_heading(decision),
                "",
            ]
        )
        for finding in findings:
            lines.extend(_finding_lines(finding))
    elif decision != "ALLOW":
        lines.extend(
            [
                _risk_summary(models),
                "",
                _reason_heading(decision),
                "",
            ]
        )
        reasons = _material_reasons(incident)
        lines.extend(f"- {reason}" for reason in reasons)
        if reasons:
            lines.append("")
        recommendation = str(incident.get("recommendation") or "").strip()
        if recommendation:
            lines.extend(
                [
                    f"**Recommendation:** {redact_text(recommendation)}",
                    "",
                ]
            )
    else:
        lines.extend(["No material deployment risks detected.", ""])

    lines.extend(
        [
            f"Decision: {redact_text(decision)}",
            f"Risk level: {redact_text(severity)}",
        ]
    )
    lines.extend(_affected_model_lines(models))
    return "\n".join(lines)


def _affected_models(result: dict, incident: dict) -> list[str]:
    values = incident.get("affected_models") or result.get("changed_models") or []
    models = []
    seen = set()
    for value in values:
        model = redact_text(str(value))
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def _risk_summary(models: list[str]) -> str:
    if len(models) == 1:
        return f"This change may produce incorrect results in `{models[0]}`."
    return "This change may produce incorrect results in the affected dbt models."


def _reason_heading(decision: str) -> str:
    if decision == "BLOCK":
        return "#### Why Relium blocked this PR"
    return "#### Why Relium flagged this PR"


def _finding_lines(finding: dict) -> list[str]:
    rule = str(finding.get("rule") or "")
    title = redact_text(
        str(finding.get("title") or "SQL risk detected")
    )
    impact = redact_text(
        _CUSTOMER_IMPACTS.get(
            rule,
            str(
                finding.get("impact")
                or "This SQL pattern may produce incorrect results."
            ),
        )
    )
    recommended_fix = redact_text(
        _CUSTOMER_FIXES.get(
            rule,
            str(
                finding.get("recommended_fix")
                or "Review and correct the affected SQL."
            ),
        )
    )
    return [
        f"**{title}**",
        impact,
        f"**Fix:** {recommended_fix}",
        "",
    ]


def _material_reasons(incident: dict) -> list[str]:
    reasons = []
    seen = set()
    for value in incident.get("top_reasons") or []:
        reason = redact_text(str(value))
        if reason and reason not in seen:
            seen.add(reason)
            reasons.append(reason)
        if len(reasons) >= 3:
            break
    return reasons


def _affected_model_lines(models: list[str]) -> list[str]:
    if not models:
        return []
    if len(models) == 1:
        return [f"Affected model: `{models[0]}`"]
    return ["Affected models: " + ", ".join(f"`{model}`" for model in models)]
