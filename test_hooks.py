import builtins
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agent.cli import cli
from agent.diagnose import FailureDiagnosis
from agent.hooks import RunResultsError, WatchReport, run_post_hook


REPOSITORY_ROOT = Path(__file__).resolve().parent


def _write_run_results(project: Path, payload) -> Path:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    results_path = target / "run_results.json"
    if isinstance(payload, str):
        results_path.write_text(payload, encoding="utf-8")
    else:
        results_path.write_text(json.dumps(payload), encoding="utf-8")
    return results_path


def _complete_failure(**overrides):
    result = {
        "status": "error",
        "unique_id": "model.jaffle_shop.fct_orders",
        "message": 'column "order_status" does not exist',
        "compiled_code": "select order_status from stg_orders",
        "relation_name": "analytics.fct_orders",
    }
    result.update(overrides)
    return result


def _file_manifest(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RunResultsParsingTests(unittest.TestCase):
    def test_missing_run_results_file_is_a_controlled_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                RunResultsError,
                "run_results.json not found",
            ):
                run_post_hook(tmp)

    def test_invalid_json_is_a_controlled_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, "{not json")

            with self.assertRaisesRegex(
                RunResultsError,
                "Invalid run_results.json",
            ):
                run_post_hook(project)

    def test_missing_results_property_is_a_controlled_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, {"metadata": {}})

            with self.assertRaisesRegex(RunResultsError, "results.*list"):
                run_post_hook(project)

    def test_non_list_results_is_a_controlled_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, {"results": {"status": "error"}})

            with self.assertRaisesRegex(RunResultsError, "results.*list"):
                run_post_hook(project)

    def test_empty_results_returns_an_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, {"results": []})

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(report.diagnoses, ())
        self.assertEqual(report.malformed_entries, 0)

    def test_successful_run_returns_no_diagnoses(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(
                project,
                {
                    "results": [
                        {
                            "status": "success",
                            "unique_id": "model.jaffle_shop.fct_orders",
                        }
                    ]
                },
            )

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(report.diagnoses, ())

    def test_complete_failed_model_produces_typed_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, {"results": [_complete_failure()]})

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(len(report.diagnoses), 1)
        diagnosis = report.diagnoses[0]
        self.assertIsInstance(diagnosis, FailureDiagnosis)
        self.assertEqual(diagnosis.affected_model, "fct_orders")
        self.assertIsNone(diagnosis.affected_file)
        self.assertIsNone(diagnosis.affected_line)
        self.assertEqual(
            diagnosis.metadata,
            {
                "unique_id": "model.jaffle_shop.fct_orders",
                "relation_name": "analytics.fct_orders",
            },
        )
        self.assertTrue(any("Model SQL" in item for item in diagnosis.evidence))

    def test_multiple_failed_models_are_all_diagnosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(
                project,
                {
                    "results": [
                        _complete_failure(),
                        _complete_failure(
                            unique_id="model.jaffle_shop.dim_customers",
                            message="permission denied for relation customers",
                            compiled_code=None,
                            relation_name=None,
                        ),
                    ]
                },
            )

            report = run_post_hook(project)

        self.assertEqual(
            [diagnosis.affected_model for diagnosis in report.diagnoses],
            ["fct_orders", "dim_customers"],
        )

    def test_failed_model_without_optional_artifact_fields_is_diagnosed(self):
        failure = {
            "status": "error",
            "unique_id": "model.jaffle_shop.fct_orders",
            "message": "permission denied for relation orders",
        }

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, {"results": [failure]})

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(len(report.diagnoses), 1)
        diagnosis = report.diagnoses[0]
        self.assertEqual(diagnosis.affected_model, "fct_orders")
        self.assertEqual(
            diagnosis.metadata,
            {"unique_id": "model.jaffle_shop.fct_orders"},
        )
        self.assertNotIn("compiled_code", diagnosis.metadata)
        self.assertNotIn("relation_name", diagnosis.metadata)

    def test_nested_model_identifier_uses_only_the_explicit_model_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(
                project,
                {
                    "results": [
                        _complete_failure(
                            unique_id="model.jaffle_shop.staging.orders"
                        )
                    ]
                },
            )

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(report.diagnoses[0].affected_model, "orders")
        self.assertIsNone(report.diagnoses[0].affected_file)

    def test_non_model_failures_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(
                project,
                {
                    "results": [
                        _complete_failure(
                            unique_id="test.jaffle_shop.not_null_fct_orders_id"
                        ),
                        _complete_failure(
                            unique_id="snapshot.jaffle_shop.orders_snapshot"
                        ),
                    ]
                },
            )

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(report.diagnoses, ())

    def test_malformed_entries_are_skipped_while_valid_failures_survive(self):
        malformed_entries = [
            None,
            "not an object",
            {},
            {"status": "error", "message": "missing unique id"},
            {
                "status": "error",
                "unique_id": "model.jaffle_shop.missing_message",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(
                project,
                {"results": [*malformed_entries, _complete_failure()]},
            )

            report = run_post_hook(project)

        self.assertIsInstance(report, WatchReport)
        self.assertEqual(len(report.diagnoses), 1)
        self.assertEqual(report.malformed_entries, len(malformed_entries))


class HooksImportTests(unittest.TestCase):
    def test_hooks_source_has_no_cli_or_click_dependency(self):
        source = (REPOSITORY_ROOT / "agent" / "hooks.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("agent.cli", source)
        self.assertNotRegex(source, r"(?m)^\s*(?:from|import)\s+click\b")
        self.assertNotIn("print_diagnosis", source)

    def test_cli_then_hooks_import_order(self):
        self._assert_import_order("import agent.cli; import agent.hooks")

    def test_hooks_then_cli_import_order(self):
        self._assert_import_order("import agent.hooks; import agent.cli")

    def _assert_import_order(self, statement: str):
        completed = subprocess.run(
            [sys.executable, "-c", statement],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertNotIn("Traceback", completed.stderr)


class WatchMutationFirewallTests(unittest.TestCase):
    def test_default_watch_leaves_failed_project_byte_for_byte_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_run_results(project, {"results": [_complete_failure()]})
            model_path = project / "models" / "marts" / "fct_orders.sql"
            model_path.parent.mkdir(parents=True)
            model_path.write_text(
                "select order_status from stg_orders\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "-q"],
                cwd=project,
                check=True,
                capture_output=True,
            )
            original_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            before = _file_manifest(project)

            original_open = builtins.open

            def guarded_open(file, mode="r", *args, **kwargs):
                if any(flag in mode for flag in ("w", "a", "x", "+")):
                    raise AssertionError(f"unexpected file write: {file}")
                return original_open(file, mode, *args, **kwargs)

            with (
                patch.object(builtins, "open", side_effect=guarded_open),
                patch.object(Path, "write_text") as write_text,
                patch.object(Path, "write_bytes") as write_bytes,
                patch.object(subprocess, "run") as run_command,
                patch.object(subprocess, "Popen") as popen_command,
                patch.object(os, "system") as os_system,
                patch("agent.github_pr.create_fix_pr") as create_fix_pr,
                patch("agent.github_pr.create_branch") as create_branch,
                patch("agent.github_pr.push_file") as push_file,
                patch("agent.github_pr.open_pull_request") as open_pull_request,
                patch("agent.github_pr.github_request") as github_request,
                patch("agent.slack.send_slack_alert") as send_slack_alert,
                patch(
                    "agent.slack_alerts.send_validation_alert"
                ) as send_validation_alert,
                patch.object(urllib.request, "urlopen") as urlopen,
                patch.object(socket, "create_connection") as create_connection,
            ):
                result = CliRunner().invoke(
                    cli,
                    ["watch", "--project", str(project)],
                )

            after = _file_manifest(project)
            final_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(after, before)
        self.assertEqual(final_branch, original_branch)
        write_text.assert_not_called()
        write_bytes.assert_not_called()
        run_command.assert_not_called()
        popen_command.assert_not_called()
        os_system.assert_not_called()
        create_fix_pr.assert_not_called()
        create_branch.assert_not_called()
        push_file.assert_not_called()
        open_pull_request.assert_not_called()
        github_request.assert_not_called()
        send_slack_alert.assert_not_called()
        send_validation_alert.assert_not_called()
        urlopen.assert_not_called()
        create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
