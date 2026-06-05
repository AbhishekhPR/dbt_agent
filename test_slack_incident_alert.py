import json
import unittest
from unittest.mock import patch


class SlackIncidentAlertTests(unittest.TestCase):
    def test_send_slack_alert_formats_incident_report_payload(self):
        from agent import slack

        diagnosis = {
            "root_cause": "upstream ingestion failure",
            "affected_file": "raw_orders",
            "affected_line": "row_count_anomaly",
            "explanation": "Anomaly: Row count dropped by 96.0%\nEvidence: Expected ~200 rows, observed 8 rows.",
            "suggested_fix": "Check upstream ingestion job for raw_orders",
            "severity": "critical",
            "data_loss_risk": True,
            "impact_count": 3,
            "affected_models": [
                "fct_customer_lifetime_value",
                "fct_revenue",
                "fct_customer_summary",
            ],
            "incident_report": r"incidents\test_project_raw_orders_row_count_anomaly_20260604_094438.md",
        }

        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with patch.object(slack, "WEBHOOK_URL", "https://example.test/slack"), patch(
            "urllib.request.urlopen",
            side_effect=fake_urlopen,
        ), patch("builtins.print"):
            slack.send_slack_alert("DATA QUALITY - raw_orders", diagnosis)

        payload = captured["payload"]
        self.assertEqual(
            payload["text"],
            "Relium Data Quality Incident — raw_orders: Row count dropped by 96.0%. "
            "Primary hypothesis: Upstream ingestion failure.",
        )
        blocks = payload["blocks"]
        self.assertEqual(blocks[0]["type"], "header")
        self.assertEqual(blocks[0]["text"]["text"], "Relium Data Quality Incident — raw_orders")
        self.assertEqual(blocks[1]["type"], "section")
        self.assertEqual(blocks[1]["fields"][0]["text"], "*Severity:*\nCRITICAL")
        self.assertEqual(blocks[1]["fields"][1]["text"], "*Data Loss Risk:*\nYES")
        self.assertEqual(blocks[1]["fields"][2]["text"], "*Anomaly:*\nRow count dropped by 96.0%")
        self.assertEqual(blocks[1]["fields"][3]["text"], "*Table:*\n`raw_orders`")
        self.assertEqual(blocks[2]["type"], "divider")

        text = "\n".join(
            block.get("text", {}).get("text", "")
            for block in blocks
            if isinstance(block.get("text"), dict)
        )
        self.assertIn("Relium Data Quality Incident — raw_orders", text)
        self.assertIn("Primary Hypothesis", text)
        self.assertIn("Upstream ingestion failure", text)
        self.assertIn("*Evidence*\nExpected ~200 rows, observed 8 rows.", text)
        self.assertIn("*Blast Radius*\n3 downstream models affected", text)
        self.assertIn("Affected Models:", text)
        self.assertIn("- `fct_customer_lifetime_value`", text)
        self.assertIn("- `fct_revenue`", text)
        self.assertIn("- `fct_customer_summary`", text)
        self.assertIn("*Immediate Action*\nCheck upstream ingestion job for raw_orders.", text)
        self.assertIn("Full RCA Report", text)
        self.assertIn("`incidents/test_project_raw_orders_row_count_anomaly_20260604_094438.md`", text)
        self.assertNotIn("\\", text)
        self.assertNotIn("🤖", text)
        self.assertNotIn("📌", text)


if __name__ == "__main__":
    unittest.main()
