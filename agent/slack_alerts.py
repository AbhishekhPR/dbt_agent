import json
import os
import urllib.request


def build_slack_payload(
    project_name: str,
    model_name: str,
    severity: str,
    reason: str,
    affected_models: list[str],
    anomalies: list[str],
    safe_to_continue: bool,
    recommendation: str,
    static_analysis_text: str | None = None,
    metadata_checks: dict | None = None,
    drift_result: dict | None = None,
) -> dict:
    safe_text = "YES" if safe_to_continue else "NO"
    static_text = static_analysis_text or reason
    metadata_text = _format_metadata_checks(metadata_checks, anomalies)
    drift_text = _format_drift_section(drift_result)
    full_text = _build_full_message_text(
        project_name=project_name,
        model_name=model_name,
        severity=severity,
        safe_text=safe_text,
        static_text=static_text,
        metadata_text=metadata_text,
        drift_text=drift_text,
        recommendation=recommendation,
    )
    return {
        "text": full_text,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": full_text,
                },
            },
        ],
    }


def send_validation_alert(
    project_name: str,
    model_name: str,
    severity: str,
    reason: str,
    affected_models: list[str],
    anomalies: list[str],
    safe_to_continue: bool,
    recommendation: str,
    static_analysis_text: str | None = None,
    metadata_checks: dict | None = None,
    drift_result: dict | None = None,
    emit_status: bool = True,
) -> bool:
    normalized = severity.upper()
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        if emit_status:
            print("Slack alert sent: NO")
        return False

    drift_level = (drift_result or {}).get("drift_level", "").upper()
    should_send = (
        normalized in {"HIGH", "CRITICAL"}
        or not safe_to_continue
        or drift_level == "HIGH"
    )
    if not should_send:
        if emit_status:
            print("Slack alert sent: NO")
        return False

    payload = build_slack_payload(
        project_name=project_name,
        model_name=model_name,
        severity=normalized,
        reason=reason,
        affected_models=affected_models,
        anomalies=anomalies,
        safe_to_continue=safe_to_continue,
        recommendation=recommendation,
        static_analysis_text=static_analysis_text,
        metadata_checks=metadata_checks,
        drift_result=drift_result,
    )
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            sent = 200 <= response.status < 300
    except Exception:
        sent = False
    if emit_status:
        print(f"Slack alert sent: {'YES' if sent else 'NO'}")
    return sent


def _format_metadata_checks(
    metadata_checks: dict | None,
    anomalies: list[str],
) -> str:
    if metadata_checks:
        return "\n".join(
            [
                f"- Row count: {metadata_checks.get('row_count', 0)}",
                f"- Null count: {metadata_checks.get('null_count', 0)}",
                (
                    "- Duplicate customer_id count: "
                    f"{metadata_checks.get('duplicate_count', 0)}"
                ),
                (
                    "- Freshness timestamp: "
                    f"{metadata_checks.get('freshness_timestamp') or 'None'}"
                ),
                (
                    "- Schema columns: "
                    f"{metadata_checks.get('schema_column_count', 0)}"
                ),
            ]
        )
    if anomalies:
        return "\n".join(f"- {item}" for item in anomalies)
    return "- None"


def _format_drift_section(drift_result: dict | None) -> str:
    if not drift_result:
        return "Drift detection: not enough history yet."
    return "\n".join(
        [
            "Drift detection:",
            f"- Row count change: {_fmt_pct(drift_result.get('row_count_change_pct', 0.0))}",
            (
                "- Duplicate count change: "
                f"{_fmt_pct(drift_result.get('duplicate_count_change_pct', 0.0))}"
            ),
            (
                "- Freshness regression: "
                f"{'YES' if drift_result.get('freshness_regressed') else 'NO'}"
            ),
            f"- Metadata Drift: {drift_result.get('drift_level', 'LOW')}",
        ]
    )


def _fmt_pct(value: float) -> str:
    if value == int(value):
        return f"{int(value):+d}%"
    return f"{value:+.1f}%"


def _build_full_message_text(
    project_name: str,
    model_name: str,
    severity: str,
    safe_text: str,
    static_text: str,
    metadata_text: str,
    drift_text: str,
    recommendation: str,
) -> str:
    return "\n".join(
        [
            "🚨 Relium Pipeline Risk Alert",
            "",
            f"Project: {project_name}",
            f"Model: {model_name}",
            "",
            f"Risk: {severity}",
            f"Safe to continue: {safe_text}",
            "",
            "Static analysis:",
            static_text,
            "",
            "Metadata checks:",
            metadata_text,
            "",
            drift_text,
            "",
            "Recommendation:",
            recommendation,
        ]
    )
