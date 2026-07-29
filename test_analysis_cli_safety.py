import builtins
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent.cli import cli


REPOSITORY_ROOT = Path(__file__).resolve().parent
MAIN_PATH = REPOSITORY_ROOT / "main.py"


class AnalysisCliSafetyTests(unittest.TestCase):
    def test_help_describes_local_read_only_analysis_without_notifications(self):
        runner = CliRunner()

        for command in ("analyze", "ast"):
            with self.subTest(command=command):
                result = runner.invoke(cli, [command, "--help"])
                help_text = result.output.casefold()

                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("local", help_text)
                self.assertIn("read-only", help_text)
                self.assertIn("no notification", help_text)

    def test_analyze_preserves_high_risk_output_without_delivery_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = _write_project(
                Path(temp_dir),
                "select count(*) from customers join orders",
            )
            slack_module = _adapter_module(
                "agent.slack",
                "send_slack_alert",
            )

            with patch.dict(sys.modules, {"agent.slack": slack_module}):
                result = CliRunner().invoke(
                    cli,
                    ["analyze", "--project", str(project)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Risk:", result.output)
        self.assertIn("CRITICAL", result.output)
        self.assertIn("Found 2 potential issue(s)", result.output)
        self.assertNotIn("Slack alerted", result.output)
        self.assertNotIn("Traceback", result.output)
        slack_module.send_slack_alert.assert_not_called()

    def test_ast_preserves_each_high_risk_finding_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = _write_project(
                Path(temp_dir),
                "select 1 / amount from orders",
            )
            slack_module = _adapter_module(
                "agent.slack",
                "send_slack_alert",
            )

            with patch.dict(sys.modules, {"agent.slack": slack_module}):
                result = CliRunner().invoke(
                    cli,
                    ["ast", "--project", str(project)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Total bugs found: 1", result.output)
        self.assertEqual(
            result.output.count("Division without a zero-safe guard"),
            1,
            result.output,
        )
        self.assertNotIn("Traceback", result.output)
        slack_module.send_slack_alert.assert_not_called()

    def test_analyze_default_path_cannot_reach_mutation_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = _write_project(
                Path(temp_dir),
                "select count(*) from customers join orders",
            )
            before = _manifest(project)

            result, boundary_calls, imported_modules = _invoke_with_firewall(
                ["analyze", "--project", str(project)]
            )
            after = _manifest(project)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        _assert_firewall_clean(self, boundary_calls)
        self.assertNotIn("agent.slack", imported_modules)

    def test_ast_default_path_cannot_reach_mutation_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = _write_project(
                Path(temp_dir),
                "select 1 / amount from orders",
            )
            before = _manifest(project)

            result, boundary_calls, imported_modules = _invoke_with_firewall(
                ["ast", "--project", str(project)]
            )
            after = _manifest(project)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        _assert_firewall_clean(self, boundary_calls)
        self.assertNotIn("agent.slack", imported_modules)

    def test_real_entry_point_is_local_and_leaves_projects_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            working_directory = temp_path / "working"
            working_directory.mkdir()
            analyze_project = _write_project(
                temp_path / "analyze",
                "select count(*) from customers join orders",
            )
            ast_project = _write_project(
                temp_path / "ast",
                "select 1 / amount from orders",
            )
            before = {
                "analyze": _manifest(analyze_project),
                "ast": _manifest(ast_project),
            }
            disposable_root_before = _manifest(temp_path)
            environment = os.environ.copy()
            environment.update(
                {
                    "GROQ_API_KEY": "",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SLACK_WEBHOOK_URL": "",
                }
            )

            completed = {}
            for command, project in (
                ("analyze", analyze_project),
                ("ast", ast_project),
            ):
                completed[command] = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(MAIN_PATH),
                        command,
                        "--project",
                        str(project),
                    ],
                    cwd=working_directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            after = {
                "analyze": _manifest(analyze_project),
                "ast": _manifest(ast_project),
            }
            disposable_root_after = _manifest(temp_path)

        self.assertEqual(before, after)
        self.assertEqual(disposable_root_before, disposable_root_after)
        for command, process in completed.items():
            with self.subTest(command=command):
                combined = f"{process.stdout}\n{process.stderr}"
                self.assertEqual(process.returncode, 0, combined)
                self.assertNotIn("Traceback", combined)
                self.assertNotIn("slack", combined.casefold())
                self.assertNotIn("webhook", combined.casefold())
                self.assertNotIn("network", combined.casefold())
                if command == "analyze":
                    self.assertIn("Found 2 potential issue(s)", combined)
                else:
                    self.assertEqual(
                        combined.count("Division without a zero-safe guard"),
                        1,
                        combined,
                    )


def _write_project(parent: Path, sql: str) -> Path:
    project = parent / "project"
    models = project / "models"
    models.mkdir(parents=True)
    (models / "risk.sql").write_text(sql, encoding="utf-8")
    return project


def _manifest(root: Path) -> tuple[tuple[str, str, str | None], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "directory", None))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative, "file", digest))
    return tuple(entries)


def _adapter_module(module_name: str, *function_names: str) -> types.ModuleType:
    module = types.ModuleType(module_name)
    for function_name in function_names:
        setattr(module, function_name, MagicMock(name=function_name))
    return module


def _invoke_with_firewall(arguments):
    slack = _adapter_module("agent.slack", "send_slack_alert")
    groq = _adapter_module("agent.groq_client", "call_llm", "call_llm_json")
    github = _adapter_module(
        "agent.github_pr",
        "create_branch",
        "create_fix_pr",
        "github_request",
        "open_pull_request",
        "push_file",
    )
    github_client = _adapter_module("agent.github_app.client", "GitHubClient")
    requests = _adapter_module("requests", "get", "post", "put", "request")
    requests.sessions = types.SimpleNamespace(
        Session=MagicMock(name="RequestsSession")
    )
    httpx = _adapter_module("httpx", "Client", "AsyncClient", "request")
    groq_sdk = _adapter_module("groq", "Groq")
    imported_modules = []
    original_import = builtins.__import__
    original_open = builtins.open
    original_io_open = io.open

    def tracking_import(name, *args, **kwargs):
        imported_modules.append(name)
        return original_import(name, *args, **kwargs)

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(marker in str(mode) for marker in ("w", "a", "x", "+")):
            raise AssertionError(f"filesystem write attempted: {file} ({mode})")
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if any(marker in str(mode) for marker in ("w", "a", "x", "+")):
            raise AssertionError(f"filesystem write attempted: {file} ({mode})")
        return original_io_open(file, mode, *args, **kwargs)

    boundary_calls = {
        "slack": slack.send_slack_alert,
        "groq_call": groq.call_llm,
        "groq_json": groq.call_llm_json,
        "groq_client": groq_sdk.Groq,
        "github_request": github.github_request,
        "github_branch": github.create_branch,
        "github_fix_pr": github.create_fix_pr,
        "github_pull_request": github.open_pull_request,
        "github_push": github.push_file,
        "github_client": github_client.GitHubClient,
        "requests_get": requests.get,
        "requests_post": requests.post,
        "requests_put": requests.put,
        "requests_request": requests.request,
        "requests_session": requests.sessions.Session,
        "httpx_client": httpx.Client,
        "httpx_async_client": httpx.AsyncClient,
        "httpx_request": httpx.request,
    }

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                sys.modules,
                {
                    "agent.github_app.client": github_client,
                    "agent.github_pr": github,
                    "agent.groq_client": groq,
                    "agent.slack": slack,
                    "groq": groq_sdk,
                    "httpx": httpx,
                    "requests": requests,
                },
            )
        )
        stack.enter_context(
            patch("builtins.__import__", side_effect=tracking_import)
        )
        stack.enter_context(patch("builtins.open", side_effect=guarded_open))
        stack.enter_context(patch("io.open", side_effect=guarded_io_open))
        for target in (
            "pathlib.Path.write_text",
            "pathlib.Path.write_bytes",
            "pathlib.Path.touch",
            "pathlib.Path.mkdir",
            "os.mkdir",
            "os.makedirs",
            "os.rename",
            "os.replace",
            "os.system",
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "urllib.request.urlopen",
            "socket.create_connection",
            "socket.socket",
            "sqlite3.connect",
        ):
            boundary_calls[target] = stack.enter_context(patch(target))

        result = CliRunner().invoke(cli, arguments)

    return result, boundary_calls, imported_modules


def _assert_firewall_clean(test_case, boundary_calls):
    for boundary_name, boundary in boundary_calls.items():
        with test_case.subTest(boundary=boundary_name):
            boundary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
