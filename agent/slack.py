import os
import json
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

# Explicitly load .env from project root
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

load_dotenv()

WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

def send_slack_alert(model_name: str, diagnosis: dict):
    """
    Fires a Slack message when a dbt model fails.
    Uses only stdlib urllib — no extra dependencies.
    """

    if not WEBHOOK_URL:
        print("⚠️  No SLACK_WEBHOOK_URL set in .env — skipping Slack alert.")
        return

    if diagnosis.get("incident_report"):
        payload = _build_incident_payload(diagnosis)
        _send_payload(payload)
        return

    severity_emoji = {
        "critical": "🔴",
        "high":     "🟠",
        "medium":   "🟡",
        "low":      "🟢"
    }.get(diagnosis.get("severity", "low"), "⚪")

    severity = diagnosis.get("severity", "unknown").upper()
    root_cause = diagnosis.get("root_cause", "Unknown")
    affected_file = diagnosis.get("affected_file", "Unknown")
    affected_line = diagnosis.get("affected_line", "Unknown")
    explanation = diagnosis.get("explanation", "Unknown")
    suggested_fix = diagnosis.get("suggested_fix", "Unknown")
    data_loss = diagnosis.get("data_loss_risk", False)
    data_loss_text = "⚠️ YES — act immediately" if data_loss else "✅ No immediate risk"

    # Slack Block Kit message
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{severity_emoji} dbt Pipeline Failure — {model_name}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity:*\n{severity_emoji} {severity}"},
                    {"type": "mrkdwn", "text": f"*Data Loss Risk:*\n{data_loss_text}"}
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📌 Root Cause*\n{root_cause}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📁 Affected File*\n`{affected_file}` → `{affected_line}`"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*💬 Explanation*\n{explanation}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*🔧 Suggested Fix*\n```{suggested_fix}```"
                }
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "🤖 Diagnosed by *dbt-agent* — AI-powered pipeline reliability"
                    }
                ]
            }
        ]
    }

    _send_payload(payload)


def _build_incident_payload(diagnosis: dict) -> dict:
    severity = diagnosis.get("severity", "unknown").upper()
    table = diagnosis.get("affected_file", "Unknown")
    primary_hypothesis = _title_case_hypothesis(diagnosis.get("root_cause", "Unknown"))
    explanation = diagnosis.get("explanation", "Unknown")
    anomaly_message = _extract_explanation_value(explanation, "Anomaly") or "Unknown"
    evidence = _extract_explanation_value(explanation, "Evidence") or explanation
    immediate_action = _ensure_period(diagnosis.get("suggested_fix", "Investigate upstream pipeline"))
    report_path = str(diagnosis.get("incident_report", "Unknown")).replace("\\", "/")
    data_loss = "YES" if diagnosis.get("data_loss_risk") else "NO"
    impact_count = diagnosis.get("impact_count", 0)
    affected_models = diagnosis.get("affected_models") or []
    affected_lines = (
        "\n".join(f"- `{model}`" for model in affected_models)
        if affected_models
        else "- None listed"
    )
    title = f"Relium Data Quality Incident — {table}"
    fallback = (
        f"{title}: {anomaly_message}. "
        f"Primary hypothesis: {primary_hypothesis}."
    )

    return {
        "text": fallback,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity:*\n{severity}"},
                    {"type": "mrkdwn", "text": f"*Data Loss Risk:*\n{data_loss}"},
                    {"type": "mrkdwn", "text": f"*Anomaly:*\n{anomaly_message}"},
                    {"type": "mrkdwn", "text": f"*Table:*\n`{table}`"},
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Primary Hypothesis*\n{primary_hypothesis}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Evidence*\n{evidence}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Blast Radius*\n"
                        f"{impact_count} downstream model"
                        f"{'' if impact_count == 1 else 's'} affected\n\n"
                        f"*Affected Models:*\n{affected_lines}"
                    )
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Immediate Action*\n{immediate_action}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Full RCA Report*\n`{report_path}`"
                }
            }
        ]
    }


def _extract_explanation_value(explanation: str, label: str) -> str:
    prefix = f"{label}:"
    for line in str(explanation).splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _title_case_hypothesis(value: str) -> str:
    value = str(value or "Unknown").strip()
    return value[:1].upper() + value[1:]


def _ensure_period(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return "Investigate upstream pipeline."
    return value if value.endswith((".", "!", "?")) else value + "."


def _send_payload(payload: dict):
    # Send using stdlib only
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                print("✅ Slack alert sent successfully.")
            else:
                print(f"⚠️  Slack returned status {response.status}")
    except Exception as e:
        print(f"❌ Failed to send Slack alert: {e}")
