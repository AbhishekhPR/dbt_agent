import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from click.testing import CliRunner

from agent.cli import cli
from agent.pr_guard import PrGuardError, run_pr_guard, terminal_summary


class PrGuardModuleImportTests(unittest.TestCase):
    def test_module_exposes_run_pr_guard_and_terminal_summary(self):
        # Regression test: agent/cli.py lazily imports run_pr_guard and
        # terminal_summary from agent.pr_guard. That module previously did
        # not exist, so any real invocation of the `pr_guard` CLI command
        # raised ModuleNotFoundError even though the full test suite passed.
        self.assertTrue(callable(run_pr_guard))
        self.assertTrue(callable(terminal_summary))


class PrGuardCliCommandTests(unittest.TestCase):
    def test_pr_guard_command_runs_successfully_against_fixture_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=False)
            report_path = Path(tmp) / "report.md"
            result, output = _invoke(
                ["pr_guard", "--project", str(project), "--output", str(report_path)]
            )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("PR Guard: PASSED", output)

    def test_pr_guard_command_fails_on_risky_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=True)
            report_path = Path(tmp) / "report.md"
            result, output = _invoke(
                ["pr_guard", "--project", str(project), "--output", str(report_path)]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("PR Guard: FAILED", output)

    def test_pr_guard_command_invalid_project_path_exits_nonzero(self):
        result, output = _invoke(["pr_guard", "--project", "definitely-missing-project"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not found", output)

    def test_pr_guard_command_scans_only_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=True)
            report_path = Path(tmp) / "report.md"
            result, output = _invoke(
                [
                    "pr_guard",
                    "--project",
                    str(project),
                    "--changed-files",
                    "models/safe_model.sql",
                    "--output",
                    str(report_path),
                ]
            )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Models scanned: 1", output)

    def test_pr_guard_command_github_comment_does_not_require_or_expose_a_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=False)
            comment_output = Path(tmp) / "comment.md"
            result, output = _invoke(
                [
                    "pr_guard",
                    "--project",
                    str(project),
                    "--github-comment",
                    "--comment-output",
                    str(comment_output),
                ]
            )

            written = comment_output.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Comment markdown written to", output)
        self.assertIn("GitHub environment not detected", output)
        self.assertNotIn("ghp_", output)
        self.assertNotIn("ghp_", written)


class RunPrGuardTests(unittest.TestCase):
    def test_missing_project_raises_pr_guard_error(self):
        with self.assertRaises(PrGuardError):
            run_pr_guard("definitely-missing-project")

    def test_clean_project_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=False)
            output = Path(tmp) / "report.md"
            report = run_pr_guard(str(project), output=str(output))
            report_written = output.exists()

        self.assertTrue(report["passed"])
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report_written)

    def test_risky_project_fails_default_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=True)
            output = Path(tmp) / "report.md"
            report = run_pr_guard(str(project), output=str(output))

        self.assertFalse(report["passed"])
        self.assertEqual(report["exit_code"], 1)

    def test_result_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=True)
            output = Path(tmp) / "report.md"
            report = run_pr_guard(str(project), output=str(output))

        serialized = json.dumps(report)
        self.assertIsInstance(serialized, str)

    def test_output_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=True)
            output = Path(tmp) / "report.md"
            first = run_pr_guard(str(project), output=str(output))
            second = run_pr_guard(str(project), output=str(output))

        self.assertEqual(first["highest_severity"], second["highest_severity"])
        self.assertEqual(first["model_reports"], second["model_reports"])

    def test_changed_files_input_is_not_mutated(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=False)
            output = Path(tmp) / "report.md"
            changed_files = ["models/safe_model.sql"]
            original = list(changed_files)

            run_pr_guard(str(project), changed_files=changed_files, output=str(output))

        self.assertEqual(changed_files, original)

    def test_github_comment_status_requires_no_token_and_makes_no_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _write_fixture_project(tmp, risky=False)
            output = Path(tmp) / "report.md"
            comment_output = Path(tmp) / "comment.md"
            report = run_pr_guard(
                str(project),
                output=str(output),
                github_comment=True,
                comment_output=str(comment_output),
            )

        status = report["github_comment_status"]
        self.assertFalse(status["posted"])
        self.assertEqual(status["reason"], "missing_environment")


def _invoke(args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = CliRunner().invoke(cli, args)
    output = result.output + stdout.getvalue() + stderr.getvalue()
    return result, output


def _write_fixture_project(tmp, *, risky: bool) -> Path:
    project = Path(tmp) / "project"
    models = project / "models"
    models.mkdir(parents=True)
    (models / "safe_model.sql").write_text(
        "SELECT id, name FROM customers", encoding="utf-8"
    )
    if risky:
        (models / "risky_model.sql").write_text(
            "SELECT * FROM orders WHERE status != 'cancelled'",
            encoding="utf-8",
        )
    return project


if __name__ == "__main__":
    unittest.main()
