from agent.diagnose import FailureDiagnosis


def render_diagnosis(diagnosis: FailureDiagnosis) -> str:
    """Render a diagnosis without performing I/O or adapter work."""
    if not isinstance(diagnosis, FailureDiagnosis):
        raise TypeError("diagnosis must be a FailureDiagnosis")

    lines = [
        f"Severity: {diagnosis.severity.value}",
        f"Confidence: {diagnosis.confidence}%",
        f"Category: {diagnosis.category}",
        f"Root cause: {diagnosis.root_cause}",
        f"Explanation: {diagnosis.explanation}",
        "Evidence:",
    ]
    if diagnosis.evidence:
        lines.extend(f"- {item}" for item in diagnosis.evidence)
    else:
        lines.append("- None provided")

    lines.append(f"Recommendation: {diagnosis.recommendation}")
    if diagnosis.affected_model is not None:
        lines.append(f"Affected model: {diagnosis.affected_model}")
    if diagnosis.affected_file is not None:
        lines.append(f"Affected file: {diagnosis.affected_file}")
    if diagnosis.affected_line is not None:
        lines.append(f"Affected line: {diagnosis.affected_line}")
    lines.append(
        "Data loss risk: "
        + ("YES - act immediately" if diagnosis.data_loss_risk else "No")
    )
    return "\n".join(lines)
