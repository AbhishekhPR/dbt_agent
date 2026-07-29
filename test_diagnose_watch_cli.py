import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from agent.cli import cli


REPOSITORY_ROOT = Path(__file__).resolve().parent
MAIN_PATH = REPOSITORY_ROOT / "main.py"


def _diagnose_arguments(root: Path) -> list[str]:
    log_path = root / "error.log"
    model_path = root / "model.sql"
    schema_path = root / "schema.yml"
    log_path.write_text(
        'Runtime Error: column "order_status" does not exist',
        encoding="utf-8",
    )
    model_path.write_text(
        "select order_status from stg_orders",
        encoding="utf-8",
    )
    schema_path.write_text(
        "columns:\n  - status\n",
        encoding="utf-8",
    )
    return [
        "diagnose",
        "--log",
        str(log_path),
        "--model",
        str(model_path),
        "--schema",
        str(schema_path),
    ]


class DiagnoseCliTests(unittest.TestCase):
    def test_minimal_valid_invocation_renders_canonical_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            arguments = _diagnose_arguments(Path(tmp))
            result = CliRunner().invoke(cli, arguments)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Severity: HIGH", result.output)
        self.assertIn("Root cause:", result.output)
        self.assertIn("Evidence:", result.output)
        self.assertIn("Recommendation:", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_missing_input_file_is_a_controlled_click_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_path = root / "model.sql"
            schema_path = root / "schema.yml"
            model_path.write_text("select 1", encoding="utf-8")
            schema_path.write_text("columns: []", encoding="utf-8")

            result = CliRunner().invoke(
                cli,
                [
                    "diagnose",
                    "--log",
                    str(root / "missing.log"),
                    "--model",
                    str(model_path),
                    "--schema",
                    str(schema_path),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Error log file not found", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_invalid_utf8_is_a_controlled_click_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arguments = _diagnose_arguments(root)
            (root / "error.log").write_bytes(b"\xff")

            result = CliRunner().invoke(cli, arguments)

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to diagnose:", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("UnicodeDecodeError", result.output)

    def test_real_main_entry_point_diagnoses_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arguments = _diagnose_arguments(root)
            completed = subprocess.run(
                [sys.executable, str(MAIN_PATH), *arguments],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, combined)
        self.assertIn("Severity: HIGH", completed.stdout)
        self.assertIn("Root cause:", completed.stdout)
        self.assertIn("Recommendation:", completed.stdout)
        self.assertNotIn("Traceback", combined)


class WatchCliTests(unittest.TestCase):
    def test_missing_run_results_is_a_controlled_click_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = CliRunner().invoke(
                cli,
                ["watch", "--project", tmp],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("run_results.json not found", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_invalid_run_results_is_a_controlled_click_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / "run_results.json").write_text(
                "{not json",
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                cli,
                ["watch", "--project", tmp],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid run_results.json", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("JSONDecodeError", result.output)

    def test_successful_run_reports_no_failed_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / "run_results.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "status": "success",
                                "unique_id": "model.project.orders",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                cli,
                ["watch", "--project", tmp],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No failed models found", result.output)
        self.assertNotIn("Traceback", result.output)


if __name__ == "__main__":
    unittest.main()
