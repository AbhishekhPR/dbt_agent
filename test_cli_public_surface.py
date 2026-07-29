import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from agent.cli import cli


REPOSITORY_ROOT = Path(__file__).resolve().parent
MAIN_PATH = REPOSITORY_ROOT / "main.py"
SUPPORTED_COMMANDS = frozenset(
    {
        "analyze",
        "ast",
        "backtest-deployment",
        "blast",
        "compare-last-run",
        "demo-pipeline",
        "diagnose",
        "diff",
        "history",
        "init-baseline",
        "outcome-summary",
        "pr-review-demo",
        "pr_guard",
        "quality",
        "record-outcome",
        "review-deployment",
        "root_cause",
        "scan",
        "watch",
    }
)
REMOVED_OR_UNSUPPORTED_COMMANDS = frozenset(
    {
        "freshness",
        "pr_review",
        "review",
        "simulate",
        "sql_metadata",
        "sql_risks",
    }
)


class CliPublicSurfaceTests(unittest.TestCase):
    def test_exact_supported_command_set_is_registered(self):
        registered_commands = set(cli.commands)

        self.assertEqual(registered_commands, SUPPORTED_COMMANDS)
        self.assertTrue(
            registered_commands.isdisjoint(REMOVED_OR_UNSUPPORTED_COMMANDS),
            registered_commands & REMOVED_OR_UNSUPPORTED_COMMANDS,
        )

    def test_every_supported_command_help_loads_cleanly(self):
        runner = CliRunner()

        for command in sorted(SUPPORTED_COMMANDS):
            with self.subTest(command=command):
                result = runner.invoke(cli, [command, "--help"])

                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIsNone(result.exception)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_real_entry_point_exposes_only_supported_commands(self):
        invocations = (
            (("--help",), 0),
            (("simulate", "--help"), 2),
            (("freshness", "--help"), 2),
            (("sql_metadata", "--help"), 2),
            (("sql_risks", "--help"), 2),
        )

        for arguments, expected_exit_code in invocations:
            with self.subTest(arguments=arguments):
                with tempfile.TemporaryDirectory() as working_directory:
                    completed = subprocess.run(
                        [sys.executable, str(MAIN_PATH), *arguments],
                        cwd=working_directory,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                combined_output = completed.stdout + completed.stderr
                self.assertEqual(
                    completed.returncode,
                    expected_exit_code,
                    combined_output,
                )
                self.assertNotIn("ModuleNotFoundError", combined_output)
                self.assertNotIn("Traceback", combined_output)
                if expected_exit_code == 2:
                    self.assertIn("No such command", completed.stderr)


if __name__ == "__main__":
    unittest.main()
