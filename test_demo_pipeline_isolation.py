import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


REPO_ROOT = Path(__file__).resolve().parent
MAIN_PATH = REPO_ROOT / "main.py"
ARTIFACT_NAMES = {
    "demo_pipeline.db",
    "relium_metadata.db",
    "pipeline_validation_report.md",
    "pipeline_validation_report.json",
}


def _snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    stat = path.stat()
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _manifest(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): _snapshot(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_main(args, cwd, environment=None):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if environment:
        env.update(environment)
    return subprocess.run(
        [sys.executable, str(MAIN_PATH), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _report_result():
    return {
        "generated_timestamp": "2026-07-30 12:00 UTC",
        "project_name": "relium_demo",
        "model_name": "fct_customer_lifetime_value",
        "changed_model": "fct_customer_lifetime_value",
        "scan_id": "scan-fixed",
        "scenario": "normal",
        "severity": "HIGH",
        "safe_to_continue": False,
        "static_analysis_text": "Potential LEFT JOIN nullification detected.",
        "sql_risks": [],
        "affected_models": [],
        "recommendation": "Review the SQL transformation before deployment.",
        "row_count": 3,
        "null_count": 0,
        "duplicate_count": 0,
        "freshness_timestamp": "2026-06-22T12:00:00",
        "schema_column_count": 6,
        "drift_result": None,
    }


class DemoPipelineWorkspaceTests(unittest.TestCase):
    def test_workspace_is_required_and_help_discloses_artifacts_and_reuse(self):
        from agent.cli import cli

        runner = CliRunner()
        missing = runner.invoke(cli, ["demo-pipeline"])
        help_result = runner.invoke(cli, ["demo-pipeline", "--help"])

        self.assertEqual(missing.exit_code, 2, missing.output)
        self.assertIn("Missing option '--workspace'", missing.output)
        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertIn("local workspace", help_result.output.lower())
        self.assertIn("demo_pipeline.db", help_result.output)
        self.assertIn("relium_metadata.db", help_result.output)
        self.assertIn("pipeline_validation_report.md", help_result.output)
        self.assertIn("pipeline_validation_report.json", help_result.output)
        self.assertIn("reusing a", help_result.output.lower())
        self.assertIn("without sending notifications", help_result.output.lower())

    def test_real_command_contains_all_artifacts_in_selected_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            workspace = root / "selected workspace # % ü"
            cwd.mkdir()
            before_cwd = _manifest(cwd)
            repository_artifacts = {
                name: _snapshot(REPO_ROOT / name) for name in ARTIFACT_NAMES
            }

            result = _run_main(
                [
                    "demo-pipeline",
                    "--workspace",
                    str(workspace),
                    "--scenario",
                    "normal",
                ],
                cwd,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("Slack", result.stdout + result.stderr)
            self.assertNotIn("webhook", result.stdout + result.stderr.lower())
            self.assertEqual(_manifest(cwd), before_cwd)
            self.assertEqual(
                {path.name for path in workspace.iterdir() if path.is_file()},
                ARTIFACT_NAMES,
            )
            self.assertEqual(
                {name: _snapshot(REPO_ROOT / name) for name in ARTIFACT_NAMES},
                repository_artifacts,
            )
            resolved = str(workspace.resolve())
            self.assertIn(resolved, result.stdout)
            for name in sorted(ARTIFACT_NAMES):
                self.assertIn(str((workspace / name).resolve()), result.stdout)

    def test_reusing_workspace_appends_history_and_other_workspace_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            cwd.mkdir()

            first = _run_main(
                ["demo-pipeline", "--workspace", str(workspace_a)],
                cwd,
            )
            b_run = _run_main(
                ["demo-pipeline", "--workspace", str(workspace_b)],
                cwd,
            )
            b_before = _manifest(workspace_b)
            second = _run_main(
                [
                    "demo-pipeline",
                    "--workspace",
                    str(workspace_a),
                    "--scenario",
                    "duplicate-spike",
                ],
                cwd,
            )

            for result in (first, b_run, second):
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(_manifest(workspace_b), b_before)
            connection = sqlite3.connect(workspace_a / "relium_metadata.db")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM relium_scan_runs"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM relium_model_metrics"
                    ).fetchone()[0],
                    2,
                )
            finally:
                connection.close()
            connection = sqlite3.connect(workspace_b / "relium_metadata.db")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM relium_scan_runs"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_workspace_file_is_a_controlled_operational_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "not-a-directory"
            workspace.write_text("existing", encoding="utf-8")
            before = _snapshot(workspace)

            result = _run_main(
                ["demo-pipeline", "--workspace", str(workspace)],
                root,
            )

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(_snapshot(workspace), before)

    def test_relative_workspace_and_creation_failure_are_controlled(self):
        from agent.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            relative = Path("relative workspace # % ü")
            result = runner.invoke(
                cli,
                ["demo-pipeline", "--workspace", str(relative)],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(
                {path.name for path in relative.iterdir() if path.is_file()},
                ARTIFACT_NAMES,
            )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "pathlib.Path.mkdir",
            side_effect=PermissionError("workspace denied"),
        ):
            denied = runner.invoke(
                cli,
                ["demo-pipeline", "--workspace", str(Path(tmp) / "denied")],
            )
        self.assertEqual(denied.exit_code, 1, denied.output)
        self.assertIn("Error:", denied.output)
        self.assertNotIn("Traceback", denied.output)

    def test_redirecting_workspace_is_rejected(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "redirected"
            with patch(
                "agent.demo_pipeline._has_redirecting_component",
                return_value=True,
            ):
                result = CliRunner().invoke(
                    cli,
                    ["demo-pipeline", "--workspace", str(workspace)],
                )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("symbolic links or junctions", result.output)
            self.assertNotIn("Traceback", result.output)
            self.assertFalse(workspace.exists())

    def test_real_high_scenario_does_not_inspect_webhook_or_reach_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            workspace = root / "workspace"
            firewall = root / "firewall"
            cwd.mkdir()
            firewall.mkdir()
            (firewall / "sitecustomize.py").write_text(
                "\n".join(
                    [
                        "import os, socket, urllib.request",
                        "_getenv = os.getenv",
                        "def guarded_getenv(key, *args):",
                        "    if key == 'SLACK_WEBHOOK_URL':",
                        "        raise SystemExit('webhook environment accessed')",
                        "    return _getenv(key, *args)",
                        "os.getenv = guarded_getenv",
                        "def blocked(*args, **kwargs):",
                        "    raise SystemExit('network boundary reached')",
                        "urllib.request.urlopen = blocked",
                        "socket.create_connection = blocked",
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_main(
                [
                    "demo-pipeline",
                    "--workspace",
                    str(workspace),
                    "--scenario",
                    "duplicate-spike",
                ],
                cwd,
                {
                    "PYTHONPATH": str(firewall),
                    "SLACK_WEBHOOK_URL": "https://example.invalid/hook",
                },
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("Slack", result.stdout + result.stderr)
            self.assertEqual(
                {path.name for path in workspace.iterdir() if path.is_file()},
                ARTIFACT_NAMES,
            )
            self.assertEqual(list(cwd.iterdir()), [])

    def test_real_operational_failures_are_controlled_and_databases_remain_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "pipeline_validation_report.md").mkdir()

            report_failure = _run_main(
                ["demo-pipeline", "--workspace", str(workspace)],
                root,
            )
            invalid_scenario = _run_main(
                [
                    "demo-pipeline",
                    "--workspace",
                    str(root / "other"),
                    "--scenario",
                    "unsupported",
                ],
                root,
            )

            self.assertEqual(
                report_failure.returncode,
                1,
                report_failure.stdout + report_failure.stderr,
            )
            self.assertIn("Error:", report_failure.stderr)
            self.assertNotIn("Traceback", report_failure.stderr)
            self.assertEqual(invalid_scenario.returncode, 2)
            self.assertNotIn("Traceback", invalid_scenario.stderr)
            for database in ("demo_pipeline.db", "relium_metadata.db"):
                connection = sqlite3.connect(workspace / database)
                try:
                    self.assertEqual(
                        connection.execute("PRAGMA integrity_check").fetchone()[0],
                        "ok",
                    )
                finally:
                    connection.close()
            connection = sqlite3.connect(workspace / "relium_metadata.db")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM relium_scan_runs"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM relium_model_metrics"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_metadata_query_failure_is_controlled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            connection = sqlite3.connect(workspace / "relium_metadata.db")
            try:
                connection.executescript(
                    """
                    CREATE TABLE relium_scan_runs (
                        scan_id TEXT PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        risk_level TEXT NOT NULL,
                        safe_to_merge INTEGER NOT NULL,
                        affected_models TEXT NOT NULL,
                        report_text TEXT
                    );
                    CREATE TABLE relium_model_metrics (
                        scan_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        row_count INTEGER,
                        null_count INTEGER,
                        duplicate_count INTEGER,
                        freshness_timestamp TEXT,
                        schema_column_count INTEGER
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()

            result = _run_main(
                ["demo-pipeline", "--workspace", str(workspace)],
                root,
            )

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_malformed_workspace_databases_fail_cleanly_without_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            warehouse_workspace = root / "malformed-warehouse"
            warehouse_workspace.mkdir()
            warehouse = warehouse_workspace / "demo_pipeline.db"
            warehouse.write_bytes(b"not sqlite")
            warehouse_before = _snapshot(warehouse)

            warehouse_result = _run_main(
                ["demo-pipeline", "--workspace", str(warehouse_workspace)],
                root,
            )

            self.assertEqual(
                warehouse_result.returncode,
                1,
                warehouse_result.stdout + warehouse_result.stderr,
            )
            self.assertIn("Error:", warehouse_result.stderr)
            self.assertNotIn("Traceback", warehouse_result.stderr)
            self.assertEqual(_snapshot(warehouse), warehouse_before)

            metadata_workspace = root / "malformed-metadata"
            metadata_workspace.mkdir()
            metadata = metadata_workspace / "relium_metadata.db"
            metadata.write_bytes(b"not sqlite")
            metadata_before = _snapshot(metadata)

            metadata_result = _run_main(
                ["demo-pipeline", "--workspace", str(metadata_workspace)],
                root,
            )

            self.assertEqual(
                metadata_result.returncode,
                1,
                metadata_result.stdout + metadata_result.stderr,
            )
            self.assertIn("Error:", metadata_result.stderr)
            self.assertNotIn("Traceback", metadata_result.stderr)
            self.assertEqual(_snapshot(metadata), metadata_before)
            connection = sqlite3.connect(
                metadata_workspace / "demo_pipeline.db"
            )
            try:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                connection.close()

    def test_service_requires_explicit_artifact_paths_and_never_reaches_network(self):
        from agent.demo_pipeline import DemoPipelineError, run_demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                "warehouse_db_path": root / "demo_pipeline.db",
                "metadata_db_path": root / "relium_metadata.db",
                "markdown_report_path": root / "pipeline_validation_report.md",
                "json_report_path": root / "pipeline_validation_report.json",
            }
            blocked_modules = {
                name: None
                for name in (
                    "agent.github_pr",
                    "agent.groq_client",
                    "agent.slack",
                    "agent.slack_alerts",
                    "dotenv",
                    "groq",
                    "httpx",
                    "requests",
                )
            }
            boundaries = (
                "os.system",
                "socket.create_connection",
                "socket.socket",
                "subprocess.Popen",
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "subprocess.run",
                "urllib.request.urlopen",
            )
            real_connect = sqlite3.connect
            sqlite_connections = []

            def connect(database, *args, **kwargs):
                sqlite_connections.append((database, dict(kwargs)))
                return real_connect(database, *args, **kwargs)

            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, blocked_modules))
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {"SLACK_WEBHOOK_URL": "https://example.invalid/hook"},
                    )
                )
                for target in boundaries:
                    stack.enter_context(
                        patch(
                            target,
                            side_effect=AssertionError(
                                f"unexpected external boundary: {target}"
                            ),
                        )
                    )
                stack.enter_context(
                    patch("sqlite3.connect", side_effect=connect)
                )
                result = run_demo_pipeline(scenario="normal", **paths)

            self.assertEqual(result["scenario"], "normal")
            self.assertNotIn("slack_sent", result)
            self.assertEqual(
                {path.name for path in root.iterdir() if path.is_file()},
                ARTIFACT_NAMES,
            )
            allowed_databases = {
                paths["warehouse_db_path"].resolve(),
                paths["metadata_db_path"].resolve(),
            }
            self.assertTrue(sqlite_connections)
            for database, options in sqlite_connections:
                if options.get("uri"):
                    self.assertTrue(
                        str(database).startswith(
                            paths["metadata_db_path"].resolve().as_uri()
                        )
                    )
                else:
                    self.assertIn(Path(database).resolve(), allowed_databases)

            redirected = root / "redirected"
            redirected.mkdir()
            redirected_paths = {
                key: redirected / path.name for key, path in paths.items()
            }
            with patch(
                "agent.demo_pipeline._has_redirecting_component",
                return_value=True,
            ), self.assertRaises(DemoPipelineError):
                run_demo_pipeline(scenario="normal", **redirected_paths)
            self.assertEqual(list(redirected.iterdir()), [])

            other_workspace = root / "other"
            other_workspace.mkdir()
            inconsistent_paths = {
                **paths,
                "json_report_path": (
                    other_workspace / "pipeline_validation_report.json"
                ),
            }
            with patch("agent.demo_pipeline.sqlite3.connect") as connect_mock:
                with self.assertRaises(DemoPipelineError):
                    run_demo_pipeline(scenario="normal", **inconsistent_paths)
            connect_mock.assert_not_called()
            self.assertEqual(list(other_workspace.iterdir()), [])

            misnamed_paths = {
                **paths,
                "json_report_path": root / "unexpected.json",
            }
            with patch("agent.demo_pipeline.sqlite3.connect") as connect_mock:
                with self.assertRaises(DemoPipelineError):
                    run_demo_pipeline(scenario="normal", **misnamed_paths)
            connect_mock.assert_not_called()
            self.assertFalse((root / "unexpected.json").exists())

    def test_import_and_help_are_pure_and_do_not_import_slack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = (
                "import importlib.abc, os, pathlib, socket, sqlite3, subprocess, sys, urllib.request\n"
                "from click.testing import CliRunner\n"
                "before=dict(os.environ)\n"
                "blocked=('dotenv','agent.slack','agent.slack_alerts','agent.groq_client','agent.github_pr','agent.github_app','groq','httpx','requests')\n"
                "class BlockedImports(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, fullname, path=None, target=None):\n"
                "        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):\n"
                "            raise AssertionError('external module imported: ' + fullname)\n"
                "        return None\n"
                "sys.meta_path.insert(0, BlockedImports())\n"
                "def fail(*args, **kwargs):\n"
                "    raise AssertionError('external or mutation boundary reached')\n"
                "sqlite3.connect=fail\n"
                "urllib.request.urlopen=fail\n"
                "socket.create_connection=fail\n"
                "socket.socket=fail\n"
                "subprocess.run=fail\n"
                "subprocess.Popen=fail\n"
                "os.system=fail\n"
                "import agent.demo_pipeline\n"
                "import agent.pipeline_validation_report\n"
                "from agent.cli import cli\n"
                "help_result=CliRunner().invoke(cli,['demo-pipeline','--help'])\n"
                "assert help_result.exit_code == 0, help_result.output\n"
                "assert dict(os.environ) == before\n"
                "assert list(pathlib.Path.cwd().iterdir()) == []\n"
            )
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            imported = subprocess.run(
                [sys.executable, "-c", script],
                cwd=root,
                env={
                    **environment,
                    "PYTHONPATH": str(REPO_ROOT),
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            help_result = _run_main(["demo-pipeline", "--help"], root)

            self.assertEqual(
                imported.returncode,
                0,
                imported.stdout + imported.stderr,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertEqual(list(root.iterdir()), [])


class PipelineReportTransactionTests(unittest.TestCase):
    def _existing_reports(self, root):
        markdown = Path(root) / "pipeline_validation_report.md"
        json_report = Path(root) / "pipeline_validation_report.json"
        markdown.write_text("previous markdown", encoding="utf-8")
        json_report.write_text('{"previous": true}\n', encoding="utf-8")
        return markdown, json_report

    def _assert_previous_reports(self, markdown, json_report):
        self.assertEqual(
            markdown.read_text(encoding="utf-8"),
            "previous markdown",
        )
        self.assertEqual(
            json_report.read_text(encoding="utf-8"),
            '{"previous": true}\n',
        )
        self.assertEqual(
            sorted(
                path.name
                for path in markdown.parent.iterdir()
                if path.name.startswith(".")
            ),
            [],
        )

    def test_serialization_failures_preserve_both_reports(self):
        from agent.pipeline_validation_report import (
            PipelineReportError,
            write_pipeline_validation_report,
        )

        for target in ("markdown", "json"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                markdown, json_report = self._existing_reports(tmp)
                patch_target = (
                    "agent.pipeline_validation_report."
                    "format_pipeline_validation_report"
                    if target == "markdown"
                    else "agent.pipeline_validation_report.json.dumps"
                )
                with patch(
                    patch_target,
                    side_effect=ValueError(f"{target} serialization failed"),
                ), self.assertRaises(PipelineReportError):
                    write_pipeline_validation_report(
                        _report_result(),
                        markdown,
                        json_report,
                    )
                self._assert_previous_reports(markdown, json_report)

    def test_temporary_creation_write_flush_and_fsync_failures_preserve_reports(self):
        from agent.pipeline_validation_report import (
            PipelineReportError,
            write_pipeline_validation_report,
        )

        class FailingTemporary:
            def __init__(self, name, failure):
                self.name = str(name)
                self.failure = failure

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def write(self, value):
                if self.failure == "write":
                    raise OSError("write failed")

            def flush(self):
                if self.failure == "flush":
                    raise OSError("flush failed")

            def fileno(self):
                return 99

        for stage_number in (1, 2):
            for failure in ("create", "write", "flush", "fsync"):
                with self.subTest(
                    stage=stage_number,
                    failure=failure,
                ), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    markdown, json_report = self._existing_reports(root)
                    temporary = (
                        root
                        / f".pipeline_validation_report.{stage_number}.failure.tmp"
                    )
                    real_named_temporary_file = tempfile.NamedTemporaryFile
                    named_temporary_calls = 0

                    def named_temporary_file(*args, **kwargs):
                        nonlocal named_temporary_calls
                        named_temporary_calls += 1
                        if named_temporary_calls != stage_number:
                            return real_named_temporary_file(*args, **kwargs)
                        if failure == "create":
                            raise OSError("creation failed")
                        return FailingTemporary(temporary, failure)

                    real_fsync = os.fsync
                    fsync_calls = 0

                    def fsync(file_descriptor):
                        nonlocal fsync_calls
                        fsync_calls += 1
                        if failure == "fsync" and fsync_calls == stage_number:
                            raise OSError("fsync failed")
                        return real_fsync(file_descriptor)

                    with patch(
                        "agent.pipeline_validation_report.tempfile.NamedTemporaryFile",
                        side_effect=named_temporary_file,
                    ), patch(
                        "agent.pipeline_validation_report.os.fsync",
                        side_effect=fsync,
                    ), self.assertRaises(PipelineReportError):
                        write_pipeline_validation_report(
                            _report_result(),
                            markdown,
                            json_report,
                        )
                    self._assert_previous_reports(markdown, json_report)

    def test_first_and_second_replace_failures_preserve_both_reports(self):
        from agent.pipeline_validation_report import (
            PipelineReportError,
            write_pipeline_validation_report,
        )

        for failed_target in ("markdown", "json"):
            with self.subTest(target=failed_target), tempfile.TemporaryDirectory() as tmp:
                markdown, json_report = self._existing_reports(tmp)
                real_replace = os.replace
                failed_once = False

                def replace(source, destination):
                    nonlocal failed_once
                    destination = Path(destination)
                    expected = markdown if failed_target == "markdown" else json_report
                    if destination == expected and not failed_once:
                        failed_once = True
                        raise OSError(f"{failed_target} replacement failed")
                    return real_replace(source, destination)

                with patch(
                    "agent.pipeline_validation_report.os.replace",
                    side_effect=replace,
                ), self.assertRaises(PipelineReportError):
                    write_pipeline_validation_report(
                        _report_result(),
                        markdown,
                        json_report,
                    )
                self._assert_previous_reports(markdown, json_report)

    def test_rollback_failure_is_reported_explicitly(self):
        from agent.pipeline_validation_report import (
            PipelineReportError,
            write_pipeline_validation_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            markdown, json_report = self._existing_reports(tmp)
            real_replace = os.replace
            calls = 0

            def replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("JSON replacement failed")
                if calls == 3:
                    raise OSError("Markdown rollback failed")
                return real_replace(source, destination)

            with patch(
                "agent.pipeline_validation_report.os.replace",
                side_effect=replace,
            ), self.assertRaisesRegex(
                PipelineReportError,
                "Markdown rollback also failed",
            ):
                write_pipeline_validation_report(
                    _report_result(),
                    markdown,
                    json_report,
                )
            self.assertEqual(
                json_report.read_text(encoding="utf-8"),
                '{"previous": true}\n',
            )

    def test_temporary_cleanup_failure_is_reported_explicitly(self):
        from agent.pipeline_validation_report import (
            PipelineReportError,
            write_pipeline_validation_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            markdown, json_report = self._existing_reports(tmp)
            with patch(
                "agent.pipeline_validation_report.os.replace",
                side_effect=OSError("replacement failed"),
            ), patch(
                "agent.pipeline_validation_report._remove_staged_report",
                return_value=OSError("cleanup failed"),
            ), self.assertRaisesRegex(
                PipelineReportError,
                "temporary report cleanup also failed",
            ):
                write_pipeline_validation_report(
                    _report_result(),
                    markdown,
                    json_report,
                )

    def test_identical_result_produces_byte_identical_reports(self):
        from agent.pipeline_validation_report import write_pipeline_validation_report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "pipeline_validation_report.md"
            json_report = root / "pipeline_validation_report.json"

            write_pipeline_validation_report(
                _report_result(),
                markdown,
                json_report,
            )
            first = (_snapshot(markdown), _snapshot(json_report))
            write_pipeline_validation_report(
                _report_result(),
                markdown,
                json_report,
            )
            second = (_snapshot(markdown), _snapshot(json_report))

            self.assertEqual(
                [(item["sha256"], item["size"]) for item in first],
                [(item["sha256"], item["size"]) for item in second],
            )
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["pipeline_validation_report.json", "pipeline_validation_report.md"],
            )


if __name__ == "__main__":
    unittest.main()
