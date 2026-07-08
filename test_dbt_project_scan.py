import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


class _CapturedClickResult:
    def __init__(self, result, output):
        self._result = result
        self.output = output

    def __getattr__(self, name):
        return getattr(self._result, name)


class DbtProjectScanTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tmpdir.name)
        (self.project / "dbt_project.yml").write_text(
            "name: fixture_project\n",
            encoding="utf-8",
        )
        self._write_manifest()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_manifest(self):
        nodes = {
            "model.fixture_project.customers": self._model_node(
                "customers", "models/customers.sql", []
            ),
            "model.fixture_project.orders": self._model_node(
                "orders", "models/orders.sql", ["model.fixture_project.customers"]
            ),
            "model.fixture_project.customer_orders": self._model_node(
                "customer_orders", "models/customer_orders.sql", ["model.fixture_project.customers"]
            ),
            "model.fixture_project.fct_customer_lifetime_value": self._model_node(
                "fct_customer_lifetime_value",
                "models/fct_customer_lifetime_value.sql",
                [
                    "model.fixture_project.orders",
                    "model.fixture_project.customer_orders",
                ],
            ),
            "test.fixture_project.not_a_model": {
                "resource_type": "test",
                "name": "not_a_model",
                "compiled_path": "target/compiled/fixture_project/models/not_a_model.sql",
                "depends_on": {"nodes": ["model.fixture_project.customers"]},
            },
        }
        manifest = {
            "metadata": {"project_name": "fixture_project"},
            "nodes": nodes,
        }
        target = self.project / "target"
        target.mkdir()
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    @staticmethod
    def _model_node(name, path, dependencies):
        return {
            "resource_type": "model",
            "name": name,
            "compiled_path": f"target/compiled/fixture_project/{path}",
            "path": path,
            "depends_on": {"nodes": dependencies},
        }

    def _write_compiled_models(self):
        compiled = self.project / "target" / "compiled" / "fixture_project" / "models"
        compiled.mkdir(parents=True)
        (compiled / "customers.sql").write_text("SELECT * FROM raw_customers", encoding="utf-8")
        (compiled / "orders.sql").write_text(
            "SELECT c.customer_id FROM customers c "
            "LEFT JOIN profiles p ON c.profile_id = p.id WHERE p.active = 1",
            encoding="utf-8",
        )
        (compiled / "customer_orders.sql").write_text(
            "SELECT customer_id FROM customers",
            encoding="utf-8",
        )
        (compiled / "fct_customer_lifetime_value.sql").write_text(
            "SELECT customer_id FROM customer_orders",
            encoding="utf-8",
        )
        (compiled / "not_a_model.sql").write_text(
            "SELECT * FROM customers",
            encoding="utf-8",
        )

    def _invoke_scan(self, *args):
        from agent.cli import scan as scan_command

        leaked_stdout = io.StringIO()
        with contextlib.redirect_stdout(leaked_stdout):
            result = CliRunner().invoke(scan_command, [str(arg) for arg in args])

        combined_output = (result.output or "") + leaked_stdout.getvalue()
        return _CapturedClickResult(result, combined_output)

    def test_scan_prefers_compiled_model_artifacts_and_aggregates_risks(self):
        from agent.dbt_project_scan import scan_dbt_project

        self._write_compiled_models()
        run_models = self.project / "target" / "run" / "fixture_project" / "models"
        run_models.mkdir(parents=True)
        (run_models / "customers.sql").write_text("SELECT * FROM ignored_run_copy", encoding="utf-8")

        report = scan_dbt_project(str(self.project))

        self.assertEqual(report["project_name"], "fixture_project")
        self.assertEqual(report["models_scanned"], 4)
        self.assertEqual(report["risks_found"], 2)
        self.assertEqual(report["highest_severity"], "HIGH")
        self.assertFalse(report["safe_to_merge"])

    def test_scan_falls_back_to_run_artifacts(self):
        from agent.dbt_project_scan import scan_dbt_project

        run_models = self.project / "target" / "run" / "fixture_project" / "models"
        run_models.mkdir(parents=True)
        for name in ("customers", "orders", "customer_orders", "fct_customer_lifetime_value"):
            (run_models / f"{name}.sql").write_text("SELECT customer_id FROM source_table", encoding="utf-8")

        report = scan_dbt_project(str(self.project))

        self.assertEqual(report["models_scanned"], 4)
        self.assertEqual(report["risks_found"], 0)
        self.assertEqual(report["highest_severity"], "NONE")
        self.assertTrue(report["safe_to_merge"])

    def test_scan_traverses_manifest_downstream_models_breadth_first(self):
        from agent.dbt_project_scan import scan_dbt_project

        self._write_compiled_models()

        report = scan_dbt_project(str(self.project), changed_model="CUSTOMERS")

        self.assertEqual(report["changed_model"], "customers")
        self.assertEqual(
            report["affected_models"],
            ["orders", "customer_orders", "fct_customer_lifetime_value"],
        )

    def test_scan_without_changed_model_has_no_affected_models(self):
        from agent.dbt_project_scan import scan_dbt_project

        self._write_compiled_models()

        report = scan_dbt_project(str(self.project))

        self.assertIsNone(report["changed_model"])
        self.assertEqual(report["affected_models"], [])

    def test_scan_errors_when_no_compiled_or_run_artifacts_exist(self):
        from agent.dbt_project_scan import scan_dbt_project

        with self.assertRaisesRegex(ValueError, "target/compiled or target/run"):
            scan_dbt_project(str(self.project))

    def test_scan_falls_back_to_nested_compiled_path_and_skips_missing_models(self):
        from agent.dbt_project_scan import scan_dbt_project

        manifest = {
            "metadata": {"project_name": "jaffle_shop"},
            "nodes": {
                "model.jaffle_shop.stg_customers": {
                    "resource_type": "model",
                    "name": "stg_customers",
                    "compiled_path": "target/compiled/jaffle_shop/models/stg_customers.sql",
                    "path": "models/staging/stg_customers.sql",
                    "package_name": "jaffle_shop",
                    "depends_on": {"nodes": []},
                },
                "model.jaffle_shop.missing_model": {
                    "resource_type": "model",
                    "name": "missing_model",
                    "compiled_path": "target/compiled/jaffle_shop/models/missing_model.sql",
                    "path": "models/missing_model.sql",
                    "package_name": "jaffle_shop",
                    "depends_on": {"nodes": []},
                },
            },
        }
        (self.project / "target" / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        nested_model = (
            self.project
            / "target"
            / "compiled"
            / "jaffle_shop"
            / "models"
            / "staging"
            / "stg_customers.sql"
        )
        nested_model.parent.mkdir(parents=True)
        nested_model.write_text("SELECT customer_id FROM source_customers", encoding="utf-8")

        report = scan_dbt_project(str(self.project))

        self.assertEqual(report["project_name"], "jaffle_shop")
        self.assertEqual(report["models_scanned"], 1)
        self.assertEqual(report["risks_found"], 0)

    def test_scan_cli_prints_report_and_changed_model(self):
        self._write_compiled_models()

        result = self._invoke_scan(
            "--project", self.project, "--changed-model", "customers"
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Relium Scan Report", result.output)
        self.assertIn("Project: fixture_project", result.output)
        self.assertIn("Models scanned: 4", result.output)
        self.assertIn("Risks found: 2", result.output)
        self.assertIn("Highest severity: HIGH", result.output)
        self.assertIn("Changed model: customers", result.output)
        self.assertIn(
            "Affected downstream models: [orders, customer_orders, fct_customer_lifetime_value]",
            result.output,
        )
        self.assertIn("Safe to merge: NO", result.output)

    def test_scan_cli_captures_output_without_stdout_leak(self):
        self._write_compiled_models()
        leaked_stdout = io.StringIO()
        leaked_dunder_stdout = io.StringIO()

        with (
            contextlib.redirect_stdout(leaked_stdout),
            patch.object(sys, "__stdout__", leaked_dunder_stdout),
        ):
            result = self._invoke_scan(
                "--project", self.project, "--changed-model", "customers"
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Relium Scan Report", result.output)
        self.assertNotIn("Relium Scan Report", leaked_stdout.getvalue())
        self.assertNotIn("Relium Scan Report", leaked_dunder_stdout.getvalue())

    def test_verbose_report_lists_every_model_with_path_and_risks(self):
        from agent.dbt_project_scan import (
            format_verbose_scan_report,
            scan_dbt_project,
        )

        self._write_compiled_models()

        output = format_verbose_scan_report(
            scan_dbt_project(str(self.project), changed_model="customers")
        )

        self.assertIn("Scanned models:", output)
        self.assertIn("Model: customers", output)
        self.assertIn("Model: orders", output)
        self.assertIn("Model: customer_orders", output)
        self.assertIn("Model: fct_customer_lifetime_value", output)
        self.assertIn("Compiled SQL:", output)
        self.assertIn("[HIGH] LEFT_JOIN_NULLIFIED:", output)
        self.assertIn("No risks found", output)
        self.assertIn(
            "Downstream models: [orders, customer_orders, fct_customer_lifetime_value]",
            output,
        )

    def test_scan_cli_without_changed_model_uses_empty_list(self):
        self._write_compiled_models()

        result = self._invoke_scan("--project", self.project)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Changed model: not provided", result.output)
        self.assertIn("Affected downstream models: []", result.output)
        self.assertNotIn("Scanned models:", result.output)

    def test_scan_cli_verbose_prints_model_audit(self):
        self._write_compiled_models()

        result = self._invoke_scan(
            "--project",
            self.project,
            "--changed-model",
            "customers",
            "--verbose",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Scanned models:", result.output)
        self.assertIn("Compiled SQL:", result.output)

    def test_markdown_format_and_output_file(self):
        self._write_compiled_models()
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "relium_report.md"
            result = self._invoke_scan("--project", self.project, "--format", "markdown", "--output", output)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("## Relium PR Risk Summary", result.output)
            self.assertIn("### Merge Recommendation", result.output)
            self.assertIn("**DO NOT MERGE YET**", result.output)
            self.assertIn("### Changed Model Impact", result.output)
            self.assertIn("### Findings", result.output)
            self.assertIn("#### LEFT_JOIN_NULLIFIED", result.output)
            self.assertIn("Severity: HIGH", result.output)
            self.assertIn("### Scan Details", result.output)
            self.assertIn("| Project | fixture_project |", result.output)
            self.assertLess(
                result.output.index("### Findings"),
                result.output.index("### Scan Details"),
            )
            self.assertEqual(output.read_text(encoding="utf-8"), result.output.rstrip("\n"))

    def test_markdown_groups_repeated_rule_findings_by_model(self):
        from agent.dbt_project_scan import format_markdown_scan_report

        select_star = {
            "rule": "SELECT_STAR",
            "severity": "low",
            "description": "SELECT * picks up all columns from upstream sources.",
            "fix": "Explicitly list the columns you need:\n  SELECT col1, col2, col3 FROM ...",
        }
        report = {
            "project_name": "jaffle_shop",
            "models_scanned": 6,
            "risks_found": 6,
            "highest_severity": "LOW",
            "changed_models": ["stg_orders"],
            "affected_models": ["orders", "order_items", "customers"],
            "safe_to_merge": True,
            "model_reports": [
                {"model_name": name, "bugs": [select_star]}
                for name in (
                    "stg_customers", "stg_locations", "stg_orders",
                    "stg_order_items", "stg_products", "stg_supplies",
                )
            ],
        }

        rendered = format_markdown_scan_report(report)

        self.assertEqual(rendered.count("#### SELECT_STAR"), 1)
        self.assertIn("Severity: LOW", rendered)
        self.assertIn("Affected models:", rendered)
        self.assertIn("- stg_supplies", rendered)
        self.assertIn("Why it matters:", rendered)
        self.assertIn("Recommendation:", rendered)
        self.assertIn(
            "Replace SELECT * with explicit column selection for the fields this model actually needs.",
            rendered,
        )
        self.assertNotIn("SELECT col1, col2, col3 FROM", rendered)
        self.assertNotIn("```", rendered)
        self.assertLess(rendered.index("#### SELECT_STAR"), rendered.index("### Scan Details"))

    def test_scan_cli_accepts_diff_base_and_auto_detects_models(self):
        self._write_compiled_models()
        with patch(
            "agent.changed_models.detect_changed_models", return_value=["customers"]
        ) as detect_changed_models:
            result = self._invoke_scan("--project", self.project, "--diff-base", "HEAD")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Changed model: customers", result.output)
        detect_changed_models.assert_called_once_with(self.project, "HEAD")


if __name__ == "__main__":
    unittest.main()
