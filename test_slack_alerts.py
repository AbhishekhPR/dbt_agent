import json
import os
import unittest
from unittest.mock import patch


class SlackAlertsTests(unittest.TestCase):
    def test_slack_payload_includes_project_and_model(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=["mart_customer_health", "mart_revenue"],
            anomalies=[
                "Row count lower than expected demo baseline",
                "Duplicate customer_id values detected",
            ],
            safe_to_continue=False,
            recommendation="Review the join filter before merging.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
        )

        text = json.dumps(payload)
        self.assertIn("demo_project", text)
        self.assertIn("fct_customer_lifetime_value", text)
        self.assertIn("Relium Pipeline Risk Alert", text)

    def test_slack_payload_includes_risk_and_safe_to_continue_verdict(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the join filter before merging.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
        )

        text = json.dumps(payload)
        self.assertIn("Risk", text)
        self.assertIn("HIGH", text)
        self.assertIn("Safe to continue", text)
        self.assertIn("NO", text)

    def test_slack_payload_includes_static_analysis_section(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the join filter before merging.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
        )

        text = json.dumps(payload)
        self.assertIn("Static analysis", text)
        self.assertIn("Potential LEFT JOIN nullification detected.", text)

    def test_slack_payload_includes_metadata_checks_section(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the join filter before merging.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
        )

        text = json.dumps(payload)
        self.assertIn("Metadata checks", text)
        self.assertIn("Row count: 6", text)
        self.assertIn("Null count: 0", text)
        self.assertIn("Duplicate customer_id count: 5", text)
        self.assertIn("Freshness timestamp: 2026-06-24T12:00:00", text)
        self.assertIn("Schema columns: 6", text)

    def test_slack_payload_includes_drift_section_when_present(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the join filter before merging.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
            drift_result={
                "row_count_change_pct": 200.0,
                "duplicate_count_change_pct": 400.0,
                "freshness_regressed": False,
                "drift_level": "HIGH",
            },
        )

        text = json.dumps(payload)
        self.assertIn("Drift detection", text)
        self.assertIn("Row count change: +200%", text)
        self.assertIn("Duplicate count change: +400%", text)
        self.assertIn("Freshness regression: NO", text)
        self.assertIn("Metadata Drift: HIGH", text)

    def test_slack_payload_handles_missing_drift_cleanly(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the join filter before merging.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
        )

        text = json.dumps(payload)
        self.assertIn("Drift detection: not enough history yet.", text)

    def test_slack_payload_text_has_blank_lines_between_sections(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="relium_demo",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the SQL transformation before deployment.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
            drift_result={
                "row_count_change_pct": 200.0,
                "duplicate_count_change_pct": 400.0,
                "freshness_regressed": False,
                "drift_level": "HIGH",
            },
        )

        self.assertIn(
            "\n\nProject: relium_demo\nModel: fct_customer_lifetime_value\n\nRisk: HIGH\nSafe to continue: NO\n\nStatic analysis:\nPotential LEFT JOIN nullification detected.\n\nMetadata checks:\n- Row count: 6",
            payload["text"],
        )
        self.assertIn(
            "\n\nDrift detection:\n- Row count change: +200%\n- Duplicate count change: +400%\n- Freshness regression: NO\n- Metadata Drift: HIGH\n\nRecommendation:\nReview the SQL transformation before deployment.",
            payload["text"],
        )

    def test_slack_block_text_has_blank_lines_between_sections(self):
        from agent.slack_alerts import build_slack_payload

        payload = build_slack_payload(
            project_name="relium_demo",
            model_name="fct_customer_lifetime_value",
            severity="HIGH",
            reason="LEFT JOIN risk plus duplicate customer rows.",
            affected_models=[],
            anomalies=[],
            safe_to_continue=False,
            recommendation="Review the SQL transformation before deployment.",
            static_analysis_text="Potential LEFT JOIN nullification detected.",
            metadata_checks={
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
            },
            drift_result={
                "row_count_change_pct": 200.0,
                "duplicate_count_change_pct": 400.0,
                "freshness_regressed": False,
                "drift_level": "HIGH",
            },
        )

        section_text = payload["blocks"][0]["text"]["text"]
        self.assertIn(
            "🚨 Relium Pipeline Risk Alert\n\nProject: relium_demo",
            section_text,
        )
        self.assertIn(
            "Safe to continue: NO\n\nStatic analysis:",
            section_text,
        )
        self.assertIn(
            "Potential LEFT JOIN nullification detected.\n\nMetadata checks:",
            section_text,
        )
        self.assertIn(
            "- Metadata Drift: HIGH\n\nRecommendation:",
            section_text,
        )

    def test_slack_is_skipped_if_env_var_missing(self):
        from agent.slack_alerts import send_validation_alert

        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as printed:
            sent = send_validation_alert(
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                severity="HIGH",
                reason="Risk detected",
                affected_models=[],
                anomalies=["Row count dropped"],
                safe_to_continue=False,
                recommendation="Investigate before merging.",
                static_analysis_text="Potential LEFT JOIN nullification detected.",
                metadata_checks={
                    "row_count": 6,
                    "null_count": 0,
                    "duplicate_count": 5,
                    "freshness_timestamp": "2026-06-24T12:00:00",
                    "schema_column_count": 6,
                },
            )

        self.assertFalse(sent)
        printed.assert_any_call("Slack alert sent: NO")


if __name__ == "__main__":
    unittest.main()
