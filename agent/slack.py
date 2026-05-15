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