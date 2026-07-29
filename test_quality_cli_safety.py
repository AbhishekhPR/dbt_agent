import builtins
import hashlib
import io
import json
import os
import shutil
import sqlite3
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
PROJECT_ID = "orders_project"


class QualityCliSafetyTests(unittest.TestCase):
    def test_help_discloses_read_only_comparison_and_explicit_update(self):
        result = CliRunner().invoke(cli, ["quality", "--help"])
        help_text = result.output.casefold()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("local", help_text)
        self.assertIn("read-only", help_text)
        self.assertIn("existing sqlite database", help_text)
        self.assertIn("existing baseline", help_text)
        self.assertIn("opened read-only", help_text)
        self.assertIn("--project-id", result.output)
        self.assertIn("lowercase safe identity", help_text)
        self.assertIn("--baseline-dir", result.output)
        self.assertIn("<project-id>.json", help_text)
        self.assertIn("--update-baseline", result.output)
        self.assertIn("creates or replaces", help_text)
        self.assertIn("no slack", help_text)
        self.assertIn("no ai", help_text)
        self.assertIn("no network", help_text)

    def test_help_does_not_import_quality_or_external_adapters(self):
        previous_quality_checker = sys.modules.pop(
            "agent.quality_checker",
            None,
        )
        external_modules = (
            "agent.slack",
            "agent.slack_alerts",
            "agent.groq_client",
            "agent.github_pr",
            "dotenv",
            "groq",
            "httpx",
            "requests",
        )
        before = {name: sys.modules.get(name) for name in external_modules}
        try:
            result = CliRunner().invoke(cli, ["quality", "--help"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("agent.quality_checker", sys.modules)
            self.assertEqual(
                {name: sys.modules.get(name) for name in external_modules},
                before,
            )
        finally:
            if previous_quality_checker is not None:
                sys.modules["agent.quality_checker"] = previous_quality_checker

    def test_import_is_pure_and_does_not_load_credentials_or_integrations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPOSITORY_ROOT / "agent", temp_root / "agent")
            (temp_root / ".env").write_text(
                "QUALITY_IMPORT_SENTINEL=loaded\n",
                encoding="utf-8",
            )
            script = """
import json
import os
import pathlib
import socket
import sqlite3
import sys
import urllib.request

network_calls = []
sqlite_calls = []
environment_before = dict(os.environ)
urllib.request.urlopen = lambda *a, **k: network_calls.append("urlopen")
socket.create_connection = lambda *a, **k: network_calls.append("connect")
socket.socket = lambda *a, **k: network_calls.append("socket")
sqlite3.connect = lambda *a, **k: sqlite_calls.append("connect")
import agent.quality_checker
print(json.dumps({
    "baseline_exists": pathlib.Path("quality_baselines").exists(),
    "dotenv_imported": "dotenv" in sys.modules,
    "slack_imported": any(
        name in sys.modules for name in ("agent.slack", "agent.slack_alerts")
    ),
    "ai_or_github_imported": any(
        name in sys.modules
        for name in (
            "agent.github_pr",
            "agent.groq_client",
            "groq",
            "httpx",
            "requests",
        )
    ),
    "sentinel": os.environ.get("QUALITY_IMPORT_SENTINEL"),
    "environment_unchanged": environment_before == dict(os.environ),
    "network_calls": network_calls,
    "sqlite_calls": sqlite_calls,
}))
"""
            environment = os.environ.copy()
            environment.pop("QUALITY_IMPORT_SENTINEL", None)
            environment["PYTHONPATH"] = str(temp_root)
            before = _manifest(temp_root)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=temp_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            after = _manifest(temp_root)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["baseline_exists"])
        self.assertFalse(payload["dotenv_imported"])
        self.assertFalse(payload["slack_imported"])
        self.assertFalse(payload["ai_or_github_imported"])
        self.assertIsNone(payload["sentinel"])
        self.assertTrue(payload["environment_unchanged"])
        self.assertEqual(payload["network_calls"], [])
        self.assertEqual(payload["sqlite_calls"], [])
        self.assertEqual(before, after)

    def test_missing_database_is_controlled_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "missing.sqlite"
            baseline_dir = root / "baselines"
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _quality_arguments(project, database, baseline_dir)
            )
            after = _manifest(root)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("SQLite database not found", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        _assert_readonly_firewall_clean(self, firewall, expected_connections=0)

    def test_directory_empty_and_malformed_databases_fail_without_state(self):
        cases = ("directory", "empty", "malformed")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project = _create_project(root)
                database = root / "database.sqlite"
                if case == "directory":
                    database.mkdir()
                elif case == "empty":
                    database.touch()
                else:
                    database.write_bytes(b"not a sqlite database")
                baseline_dir = root / "baselines"
                before = _manifest(root)

                result, firewall = _invoke_default_with_firewall(
                    _quality_arguments(project, database, baseline_dir)
                )
                after = _manifest(root)

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(before, after)
                self.assertFalse(baseline_dir.exists())
                _assert_readonly_firewall_clean(
                    self,
                    firewall,
                    expected_connections=1 if case == "malformed" else 0,
                )

    def test_locked_database_and_inaccessible_baseline_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "baselines"
            _write_baseline(
                baseline_dir,
                PROJECT_ID,
                _metrics_by_table(database),
            )
            before = _manifest(root)

            previous = sys.modules.pop("agent.quality_checker", None)
            try:
                import agent.quality_checker

                with patch(
                    "agent.quality_checker.sqlite3.connect",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    locked = CliRunner().invoke(
                        cli,
                        _quality_arguments(project, database, baseline_dir),
                    )
                with patch(
                    "agent.quality_checker.Path.read_text",
                    side_effect=PermissionError("access denied"),
                ):
                    inaccessible = CliRunner().invoke(
                        cli,
                        _quality_arguments(project, database, baseline_dir),
                    )
            finally:
                sys.modules.pop("agent.quality_checker", None)
                if previous is not None:
                    sys.modules["agent.quality_checker"] = previous
            after = _manifest(root)

        self.assertEqual(locked.exit_code, 1, locked.output)
        self.assertIn("database is locked", locked.output)
        self.assertNotIn("Traceback", locked.output)
        self.assertEqual(inaccessible.exit_code, 1, inaccessible.output)
        self.assertIn("Could not read quality baseline", inaccessible.output)
        self.assertIn("access denied", inaccessible.output)
        self.assertNotIn("Traceback", inaccessible.output)
        self.assertEqual(before, after)

    def test_special_character_database_path_is_opened_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "data % # café.sqlite"
            _create_database(database)
            baseline_dir = root / "baselines"
            _write_baseline(
                baseline_dir,
                PROJECT_ID,
                _metrics_by_table(database),
            )
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _quality_arguments(project, database, baseline_dir)
            )
            after = _manifest(root)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("all metrics within normal range", result.output)
        self.assertEqual(before, after)
        self.assertFalse(any("-journal" in entry[0] for entry in after))
        self.assertFalse(any("-wal" in entry[0] for entry in after))
        self.assertFalse(any("-shm" in entry[0] for entry in after))
        _assert_readonly_firewall_clean(self, firewall)
        connection = firewall["sqlite_connect"].call_args
        self.assertTrue(connection.kwargs["uri"])
        self.assertIn("mode=ro", str(connection.args[0]))
        self.assertIn("%25", str(connection.args[0]))
        self.assertIn("%23", str(connection.args[0]))

    def test_missing_baseline_is_controlled_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "missing-baselines"
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _quality_arguments(project, database, baseline_dir)
            )
            after = _manifest(root)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("No quality baseline exists", result.output)
        self.assertIn("--update-baseline", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        _assert_readonly_firewall_clean(self, firewall)

    def test_unchanged_comparison_preserves_every_byte_and_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "baselines"
            baseline = _write_baseline(
                baseline_dir,
                PROJECT_ID,
                _metrics_by_table(database),
            )
            database_state = _file_state(database)
            baseline_state = _file_state(baseline)
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _quality_arguments(project, database, baseline_dir)
            )
            after = _manifest(root)
            final_database_state = _file_state(database)
            final_baseline_state = _file_state(baseline)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("all metrics within normal range", result.output)
        self.assertIn("All tables passed quality checks", result.output)
        self.assertEqual(before, after)
        self.assertEqual(database_state, final_database_state)
        self.assertEqual(baseline_state, final_baseline_state)
        _assert_readonly_firewall_clean(self, firewall)

    def test_anomalies_render_locally_without_ai_or_notification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "baselines"
            baseline_metrics = _metrics_by_table(database)
            baseline_metrics["orders"]["row_count"] = 100
            baseline = _write_baseline(
                baseline_dir,
                PROJECT_ID,
                baseline_metrics,
            )
            before = _manifest(root)
            database_state = _file_state(database)
            baseline_state = _file_state(baseline)

            result, firewall = _invoke_default_with_firewall(
                _quality_arguments(project, database, baseline_dir)
            )
            after = _manifest(root)
            final_database_state = _file_state(database)
            final_baseline_state = _file_state(baseline)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[CRITICAL] Row count dropped by 97.0%", result.output)
        self.assertIn("Possible data loss or duplication", result.output)
        self.assertIn("Total anomalies found: 1", result.output)
        self.assertNotIn("Claude", result.output)
        self.assertNotIn("Slack", result.output)
        self.assertNotIn("root cause", result.output.casefold())
        self.assertEqual(before, after)
        self.assertEqual(database_state, final_database_state)
        self.assertEqual(baseline_state, final_baseline_state)
        _assert_readonly_firewall_clean(self, firewall)

    def test_project_identities_are_isolated_and_unsafe_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_project = _create_project(root, "first")
            second_project = _create_project(root, "second")
            first_database = root / "first.sqlite"
            second_database = root / "second.sqlite"
            _create_database(first_database, include_second_table=True)
            _create_database(second_database, rows=((1, None),))
            baseline_dir = root / "baselines"

            first_result, first_firewall = _invoke_update_with_firewall(
                _quality_arguments(
                    first_project,
                    first_database,
                    baseline_dir,
                    project_id="project_a",
                    update=True,
                )
            )
            first_path = baseline_dir / "project_a.json"
            first_state = _file_state(first_path)
            second_result, second_firewall = _invoke_update_with_firewall(
                _quality_arguments(
                    second_project,
                    second_database,
                    baseline_dir,
                    project_id="project_b",
                    update=True,
                )
            )
            second_path = baseline_dir / "project_b.json"

            self.assertEqual(first_result.exit_code, 0, first_result.output)
            self.assertEqual(second_result.exit_code, 0, second_result.output)
            self.assertTrue(first_path.is_file())
            self.assertTrue(second_path.is_file())
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(
                set(json.loads(first_path.read_text(encoding="utf-8"))),
                {"orders", "second_table"},
            )
            self.assertEqual(first_state, _file_state(first_path))
            _assert_external_firewall_clean(self, first_firewall)
            _assert_external_firewall_clean(self, second_firewall)

            unsafe_ids = (
                "",
                "../escape",
                "..",
                "a/b",
                r"a\b",
                "C:absolute",
                "bad<name",
                "PROJECT_A",
                "café",
                "CON",
                "con",
                "NUL.txt",
                "trailing.",
                "trailing ",
            )
            before = _manifest(root)
            for unsafe_id in unsafe_ids:
                with self.subTest(project_id=unsafe_id):
                    result, firewall = _invoke_update_with_firewall(
                        _quality_arguments(
                            first_project,
                            first_database,
                            baseline_dir,
                            project_id=unsafe_id,
                            update=True,
                        )
                    )
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertIn("safe baseline filename component", result.output)
                    self.assertNotIn("Traceback", result.output)
                    _assert_external_firewall_clean(self, firewall)
            self.assertEqual(before, _manifest(root))

    def test_symlinked_baseline_directory_is_rejected_without_writing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            target = root / "outside-target"
            target.mkdir()
            redirect = root / "redirect"
            try:
                redirect.symlink_to(target, target_is_directory=True)
            except (NotImplementedError, OSError):
                return
            selected = redirect / "selected-baselines"
            before = _manifest(root)

            result, firewall = _invoke_update_with_firewall(
                _quality_arguments(
                    project,
                    database,
                    selected,
                    update=True,
                )
            )
            after = _manifest(root)
            target_entries = list(target.iterdir())

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("must not contain symlinks or junctions", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        self.assertEqual(target_entries, [])
        _assert_external_firewall_clean(self, firewall)

    def test_symlinked_baseline_file_cannot_redirect_an_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "baselines"
            baseline_dir.mkdir()
            outside_file = root / "outside.json"
            outside_file.write_text("preserve\n", encoding="utf-8")
            selected_file = baseline_dir / f"{PROJECT_ID}.json"
            try:
                selected_file.symlink_to(outside_file)
            except (NotImplementedError, OSError):
                return
            outside_state = _file_state(outside_file)
            before = _manifest(root)

            result, firewall = _invoke_update_with_firewall(
                _quality_arguments(
                    project,
                    database,
                    baseline_dir,
                    update=True,
                )
            )
            after = _manifest(root)
            final_outside_state = _file_state(outside_file)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("must remain inside --baseline-dir", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        self.assertEqual(outside_state, final_outside_state)
        _assert_external_firewall_clean(self, firewall)

    def test_explicit_update_creates_and_deterministically_replaces_one_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "outside" / "selected-baselines"
            database_state = _file_state(database)
            before = _manifest(root)

            first_result, first_firewall = _invoke_update_with_firewall(
                _quality_arguments(
                    project,
                    database,
                    baseline_dir,
                    update=True,
                )
            )
            baseline = baseline_dir / f"{PROJECT_ID}.json"
            first_bytes = baseline.read_bytes()
            first_manifest = _manifest(root)

            second_result, second_firewall = _invoke_update_with_firewall(
                _quality_arguments(
                    project,
                    database,
                    baseline_dir,
                    update=True,
                )
            )
            second_bytes = baseline.read_bytes()
            second_manifest = _manifest(root)
            final_database_state = _file_state(database)
            baseline_is_file = baseline.is_file()
            resolved_baseline = str(baseline.resolve())

        self.assertEqual(first_result.exit_code, 0, first_result.output)
        self.assertEqual(second_result.exit_code, 0, second_result.output)
        self.assertIn(resolved_baseline, first_result.output)
        self.assertIn("Quality baseline written", first_result.output)
        self.assertTrue(baseline_is_file)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(database_state, final_database_state)
        self.assertEqual(
            _new_files(before, first_manifest),
            {
                str(baseline_dir.relative_to(root)).replace("\\", "/") + "/",
                str(baseline.relative_to(root)).replace("\\", "/"),
                "outside/",
            },
        )
        self.assertEqual(_new_files(first_manifest, second_manifest), set())
        _assert_external_firewall_clean(self, first_firewall)
        _assert_external_firewall_clean(self, second_firewall)

    def test_failed_atomic_updates_preserve_prior_baseline_and_clean_temp_files(self):
        failure_targets = (
            ("serialization", "agent.quality_checker.json.dumps"),
            ("temporary file", "agent.quality_checker.tempfile.mkstemp"),
            ("file open", "agent.quality_checker.os.fdopen"),
            ("flush", "agent.quality_checker.os.fsync"),
            ("replacement", "agent.quality_checker.os.replace"),
        )
        for label, target in failure_targets:
            with self.subTest(failure=label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project = _create_project(root)
                database = root / "warehouse.sqlite"
                _create_database(database)
                baseline_dir = root / "baselines"
                baseline = _write_baseline(
                    baseline_dir,
                    PROJECT_ID,
                    _metrics_by_table(database),
                )
                old_state = _file_state(baseline)
                database_state = _file_state(database)

                previous = sys.modules.pop("agent.quality_checker", None)
                try:
                    import agent.quality_checker

                    error = TypeError("injected") if label == "serialization" else OSError("injected")
                    with patch(target, side_effect=error):
                        result = CliRunner().invoke(
                            cli,
                            _quality_arguments(
                                project,
                                database,
                                baseline_dir,
                                update=True,
                            ),
                        )
                finally:
                    sys.modules.pop("agent.quality_checker", None)
                    if previous is not None:
                        sys.modules["agent.quality_checker"] = previous

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Could not write quality baseline", result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(old_state, _file_state(baseline))
                self.assertEqual(database_state, _file_state(database))
                self.assertEqual(
                    list(baseline_dir.glob(f".{baseline.name}.*.tmp")),
                    [],
                )

    def test_atomic_write_and_flush_failures_preserve_prior_baseline(self):
        for failure_method in ("write", "flush"):
            with (
                self.subTest(failure=failure_method),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                project = _create_project(root)
                database = root / "warehouse.sqlite"
                _create_database(database)
                baseline_dir = root / "baselines"
                baseline = _write_baseline(
                    baseline_dir,
                    PROJECT_ID,
                    _metrics_by_table(database),
                )
                old_state = _file_state(baseline)
                database_state = _file_state(database)

                previous = sys.modules.pop("agent.quality_checker", None)
                try:
                    import agent.quality_checker

                    original_fdopen = os.fdopen

                    def failing_fdopen(descriptor, *args, **kwargs):
                        return _FailingAtomicFile(
                            original_fdopen(descriptor, *args, **kwargs),
                            failure_method,
                        )

                    with patch(
                        "agent.quality_checker.os.fdopen",
                        side_effect=failing_fdopen,
                    ):
                        result = CliRunner().invoke(
                            cli,
                            _quality_arguments(
                                project,
                                database,
                                baseline_dir,
                                update=True,
                            ),
                        )
                finally:
                    sys.modules.pop("agent.quality_checker", None)
                    if previous is not None:
                        sys.modules["agent.quality_checker"] = previous

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Could not write quality baseline", result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(old_state, _file_state(baseline))
                self.assertEqual(database_state, _file_state(database))
                self.assertEqual(
                    list(baseline_dir.glob(f".{baseline.name}.*.tmp")),
                    [],
                )

    def test_malformed_baselines_are_controlled_and_never_rewritten(self):
        malformed_documents = (
            "{invalid",
            "[]",
            "{}",
            json.dumps({"orders": "invalid"}),
            json.dumps({"orders": {"table": "orders"}}),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "row_count": "ten",
                    }
                }
            ),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "columns": ["id", "id"],
                    }
                }
            ),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "null_rates": {"missing": 2.0},
                    }
                }
            ),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "unsupported": 1,
                    }
                }
            ),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "row_count": True,
                    }
                }
            ),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "null_rates": {"id": float("nan")},
                    }
                }
            ),
            json.dumps(
                {
                    "orders": {
                        **_baseline_metrics("orders"),
                        "numeric_stats": {
                            "id": {
                                "min": float("inf"),
                                "max": 1.0,
                                "avg": float("-inf"),
                            }
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "orders": _baseline_metrics("orders"),
                    "ORDERS": _baseline_metrics("ORDERS"),
                }
            ),
            (
                '{"orders": '
                + json.dumps(_baseline_metrics("orders"))
                + ', "orders": '
                + json.dumps(_baseline_metrics("orders"))
                + "}"
            ),
        )
        for document in malformed_documents:
            with self.subTest(document=document[:40]), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project = _create_project(root)
                database = root / "warehouse.sqlite"
                _create_database(database)
                baseline_dir = root / "baselines"
                baseline_dir.mkdir()
                baseline = baseline_dir / f"{PROJECT_ID}.json"
                baseline.write_text(document, encoding="utf-8")
                before = _manifest(root)

                result, firewall = _invoke_default_with_firewall(
                    _quality_arguments(project, database, baseline_dir)
                )
                after = _manifest(root)

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("quality baseline", result.output.casefold())
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(before, after)
                _assert_readonly_firewall_clean(self, firewall)

    def test_table_or_column_mismatch_is_a_controlled_comparison_error(self):
        cases = {
            "table": (
                {"other": _baseline_metrics("other")},
                "baseline tables do not match",
            ),
            "column": (
                {"orders": _baseline_metrics("orders")},
                "baseline columns do not match",
            ),
        }
        for case, (baseline_metrics, message) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project = _create_project(root)
                database = root / "warehouse.sqlite"
                _create_database(database)
                baseline_dir = root / "baselines"
                _write_baseline(
                    baseline_dir,
                    PROJECT_ID,
                    baseline_metrics,
                )
                before = _manifest(root)

                result, firewall = _invoke_default_with_firewall(
                    _quality_arguments(project, database, baseline_dir)
                )
                after = _manifest(root)

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(message, result.output.casefold())
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(before, after)
                _assert_readonly_firewall_clean(self, firewall)

    def test_metrics_handle_empty_null_and_quoted_identifiers_deterministically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    'CREATE TABLE "odd table" ("select" INTEGER, "all null" TEXT)'
                )
                connection.executemany(
                    'INSERT INTO "odd table" ("select", "all null") VALUES (?, ?)',
                    ((None, None), (None, None)),
                )
                connection.execute('CREATE TABLE "empty table" ("value" BLOB)')
                connection.execute('CREATE TABLE "blob table" ("value" BLOB)')
                connection.execute(
                    'INSERT INTO "blob table" ("value") VALUES (?)',
                    (b"\x00\x01",),
                )
                weird_table = 'dots.%# "café'
                weird_column = 'select.%# "é'
                quoted_table = '"' + weird_table.replace('"', '""') + '"'
                quoted_column = '"' + weird_column.replace('"', '""') + '"'
                connection.execute(
                    f"CREATE TABLE {quoted_table} ({quoted_column} TEXT)"
                )
                connection.execute(
                    f"INSERT INTO {quoted_table} ({quoted_column}) VALUES (?)",
                    ("deterministic",),
                )
                connection.commit()
            finally:
                connection.close()

            import agent.quality_checker as quality_checker

            metrics = quality_checker.collect_database_metrics(database)

        self.assertEqual(metrics["odd table"]["row_count"], 2)
        self.assertEqual(metrics["odd table"]["null_rates"]["select"], 100.0)
        self.assertEqual(metrics["odd table"]["duplicate_rows"], 1)
        self.assertEqual(metrics["odd table"]["numeric_stats"], {})
        self.assertEqual(metrics["empty table"]["row_count"], 0)
        self.assertEqual(metrics["empty table"]["null_rates"]["value"], 0.0)
        self.assertEqual(metrics["blob table"]["numeric_stats"], {})
        self.assertEqual(metrics[weird_table]["row_count"], 1)
        self.assertEqual(
            metrics[weird_table]["distinct_counts"][weird_column],
            1,
        )

    def test_missing_table_and_non_finite_sqlite_metrics_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE measurements (value REAL)")
                connection.execute(
                    "INSERT INTO measurements VALUES (?)",
                    (float("inf"),),
                )
                connection.commit()
            finally:
                connection.close()
            baseline_dir = root / "baselines"
            before = _manifest(root)

            import agent.quality_checker as quality_checker

            with self.assertRaisesRegex(
                quality_checker.QualityCheckError,
                "not finite",
            ):
                quality_checker.collect_database_metrics(database)
            with self.assertRaisesRegex(
                quality_checker.QualityCheckError,
                "Table not found",
            ):
                quality_checker.get_table_metrics(database, "missing")

            result = CliRunner().invoke(
                cli,
                _quality_arguments(project, database, baseline_dir),
            )
            after = _manifest(root)
            baseline_dir_exists = baseline_dir.exists()

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("not finite", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        self.assertFalse(baseline_dir_exists)

    def test_collection_error_prevents_any_multi_table_baseline_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database, include_second_table=True)
            baseline_dir = root / "baselines"
            before = _manifest(root)

            previous = sys.modules.pop("agent.quality_checker", None)
            try:
                import agent.quality_checker as quality_checker

                original = quality_checker._collect_table_metrics

                def fail_second(cursor, table_name):
                    if table_name == "second_table":
                        raise quality_checker.QualityCheckError("injected metrics failure")
                    return original(cursor, table_name)

                with patch.object(
                    quality_checker,
                    "_collect_table_metrics",
                    side_effect=fail_second,
                ):
                    result = CliRunner().invoke(
                        cli,
                        _quality_arguments(
                            project,
                            database,
                            baseline_dir,
                            update=True,
                        ),
                    )
            finally:
                sys.modules.pop("agent.quality_checker", None)
                if previous is not None:
                    sys.modules["agent.quality_checker"] = previous
            after = _manifest(root)
            baseline_dir_exists = baseline_dir.exists()

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("injected metrics failure", result.output)
        self.assertEqual(before, after)
        self.assertFalse(baseline_dir_exists)

    def test_real_entry_point_covers_comparison_and_update_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            database = root / "warehouse.sqlite"
            _create_database(database)
            database_directory = root / "database-directory"
            database_directory.mkdir()
            empty_database = root / "empty.sqlite"
            empty_database.touch()
            baseline_dir = root / "baselines"
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            missing_database = _run_quality_subprocess(
                root,
                environment,
                project,
                root / "missing.sqlite",
                baseline_dir,
            )
            directory_database = _run_quality_subprocess(
                root,
                environment,
                project,
                database_directory,
                baseline_dir,
            )
            empty_database_result = _run_quality_subprocess(
                root,
                environment,
                project,
                empty_database,
                baseline_dir,
            )
            missing_baseline = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
            )
            invalid_identity = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
                project_id="../escape",
            )
            before_update = _manifest(root)
            update = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
                update=True,
            )
            after_update = _manifest(root)
            baseline = baseline_dir / f"{PROJECT_ID}.json"
            before_compare = _manifest(root)
            compare = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
            )
            after_compare = _manifest(root)
            baseline_is_file = baseline.is_file()

        for completed in (
            missing_database,
            directory_database,
            empty_database_result,
            missing_baseline,
            invalid_identity,
        ):
            self.assertEqual(completed.returncode, 1, _combined(completed))
            self.assertNotIn("Traceback", _combined(completed))
        self.assertEqual(update.returncode, 0, _combined(update))
        self.assertTrue(baseline_is_file)
        self.assertEqual(
            _new_files(before_update, after_update),
            {"baselines/", f"baselines/{PROJECT_ID}.json"},
        )
        self.assertEqual(compare.returncode, 0, _combined(compare))
        self.assertIn("all metrics within normal range", compare.stdout)
        self.assertEqual(before_compare, after_compare)
        for completed in (
            missing_database,
            directory_database,
            empty_database_result,
            missing_baseline,
            invalid_identity,
            update,
            compare,
        ):
            combined = _combined(completed)
            self.assertNotIn("Traceback", combined)
            self.assertNotIn("Slack", combined)
            self.assertNotIn("webhook", combined.casefold())
            self.assertNotIn("Claude", combined)
            self.assertNotIn("Groq", combined)

    def test_real_entry_point_handles_malformed_and_anomalous_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = _create_project(root)
            malformed_database = root / "malformed.sqlite"
            malformed_database.write_bytes(b"not sqlite")
            database = root / "warehouse.sqlite"
            _create_database(database)
            baseline_dir = root / "baselines"
            baseline_dir.mkdir()
            baseline = baseline_dir / f"{PROJECT_ID}.json"
            baseline.write_text("{invalid", encoding="utf-8")
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            malformed_db = _run_quality_subprocess(
                root,
                environment,
                project,
                malformed_database,
                baseline_dir,
            )
            malformed_baseline = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
            )
            initial_update = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
                update=True,
            )
            initial_baseline = baseline.read_bytes()
            initial_baseline_state = _file_state(baseline)
            injected_failure = _run_injected_update_failure_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
            )
            post_failure_baseline_state = _file_state(baseline)
            remaining_temporary_files = list(
                baseline_dir.glob(f".{baseline.name}.*.tmp")
            )

            connection = sqlite3.connect(database)
            try:
                connection.executemany(
                    "INSERT INTO orders VALUES (?, ?)",
                    ((number, float(number)) for number in range(4, 11)),
                )
                connection.commit()
            finally:
                connection.close()
            database_state = _file_state(database)
            baseline_state = _file_state(baseline)

            anomaly = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
            )
            unchanged_database_state = _file_state(database)
            unchanged_baseline_state = _file_state(baseline)
            replacement = _run_quality_subprocess(
                root,
                environment,
                project,
                database,
                baseline_dir,
                update=True,
            )
            replacement_baseline = baseline.read_bytes()
            final_database_state = _file_state(database)

        self.assertEqual(malformed_db.returncode, 1, _combined(malformed_db))
        self.assertEqual(
            malformed_baseline.returncode,
            1,
            _combined(malformed_baseline),
        )
        self.assertEqual(initial_update.returncode, 0, _combined(initial_update))
        self.assertEqual(
            injected_failure.returncode,
            1,
            _combined(injected_failure),
        )
        self.assertIn(
            "Could not write quality baseline",
            _combined(injected_failure),
        )
        self.assertEqual(initial_baseline_state, post_failure_baseline_state)
        self.assertEqual(remaining_temporary_files, [])
        self.assertEqual(anomaly.returncode, 0, _combined(anomaly))
        self.assertIn("Row count spiked", anomaly.stdout)
        self.assertEqual(database_state, unchanged_database_state)
        self.assertEqual(baseline_state, unchanged_baseline_state)
        self.assertEqual(replacement.returncode, 0, _combined(replacement))
        self.assertNotEqual(initial_baseline, replacement_baseline)
        self.assertEqual(database_state, final_database_state)
        for completed in (
            malformed_db,
            malformed_baseline,
            initial_update,
            injected_failure,
            anomaly,
            replacement,
        ):
            combined = _combined(completed)
            self.assertNotIn("Traceback", combined)
            self.assertNotIn("Slack", combined)
            self.assertNotIn("Claude", combined)
            self.assertNotIn("Groq", combined)

def _create_project(root: Path, name: str = "project") -> Path:
    project = root / name
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: safety_test\n", encoding="utf-8")
    return project


def _create_database(
    path: Path,
    *,
    rows=((1, 10.0), (2, 20.0), (3, 30.0)),
    include_second_table: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)", rows)
        if include_second_table:
            connection.execute("CREATE TABLE second_table (id INTEGER)")
            connection.execute("INSERT INTO second_table VALUES (1)")
        connection.commit()
    finally:
        connection.close()


def _metrics_by_table(database: Path) -> dict:
    connection = sqlite3.connect(database)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: _metrics_for_connection(connection, table)
            for table in tables
        }
    finally:
        connection.close()


def _metrics_for_connection(connection, table: str) -> dict:
    quoted_table = '"' + table.replace('"', '""') + '"'
    columns = [
        row[1]
        for row in connection.execute(f"PRAGMA table_info({quoted_table})")
    ]
    row_count = connection.execute(
        f"SELECT COUNT(*) FROM {quoted_table}"
    ).fetchone()[0]
    null_rates = {}
    distinct_counts = {}
    numeric_stats = {}
    for column in columns:
        quoted_column = '"' + column.replace('"', '""') + '"'
        null_count = connection.execute(
            f"SELECT COUNT(*) FROM {quoted_table} "
            f"WHERE {quoted_column} IS NULL"
        ).fetchone()[0]
        null_rates[column] = (
            round(100.0 * null_count / row_count, 2)
            if row_count
            else 0.0
        )
        distinct_counts[column] = connection.execute(
            f"SELECT COUNT(DISTINCT {quoted_column}) FROM {quoted_table}"
        ).fetchone()[0]
        declared_type = next(
            row[2]
            for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            if row[1] == column
        ).upper()
        if any(token in declared_type for token in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")):
            values = connection.execute(
                f"SELECT MIN({quoted_column}), MAX({quoted_column}), "
                f"AVG({quoted_column}) FROM {quoted_table} "
                f"WHERE {quoted_column} IS NOT NULL"
            ).fetchone()
            if values[0] is not None:
                numeric_stats[column] = {
                    "min": round(values[0], 2),
                    "max": round(values[1], 2),
                    "avg": round(values[2], 2),
                }
    return {
        "table": table,
        "row_count": row_count,
        "columns": columns,
        "null_rates": null_rates,
        "duplicate_rows": 0,
        "numeric_stats": numeric_stats,
        "distinct_counts": distinct_counts,
    }


def _baseline_metrics(table: str) -> dict:
    return {
        "table": table,
        "row_count": 1,
        "columns": ["id"],
        "null_rates": {"id": 0.0},
        "duplicate_rows": 0,
        "numeric_stats": {"id": {"min": 1.0, "max": 1.0, "avg": 1.0}},
        "distinct_counts": {"id": 1},
    }


def _write_baseline(
    baseline_dir: Path,
    project_id: str,
    metrics_by_table: dict,
) -> Path:
    baseline_dir.mkdir(parents=True, exist_ok=True)
    path = baseline_dir / f"{project_id}.json"
    path.write_text(
        json.dumps(metrics_by_table, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _quality_arguments(
    project: Path,
    database: Path,
    baseline_dir: Path,
    *,
    project_id: str = PROJECT_ID,
    update: bool = False,
) -> list[str]:
    arguments = [
        "quality",
        "--project",
        str(project),
        "--project-id",
        project_id,
        "--db",
        str(database),
        "--baseline-dir",
        str(baseline_dir),
    ]
    if update:
        arguments.append("--update-baseline")
    return arguments


def _manifest(root: Path) -> tuple[tuple[str, str, int | None, str | None, int], ...]:
    entries = []
    if not root.exists():
        return ()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative + "/", "directory", None, None, path.stat().st_mtime_ns))
        elif path.is_file():
            content = path.read_bytes()
            entries.append(
                (
                    relative,
                    "file",
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    path.stat().st_mtime_ns,
                )
            )
    return tuple(entries)


def _file_state(path: Path) -> tuple[int, str, int]:
    content = path.read_bytes()
    stat = path.stat()
    return len(content), hashlib.sha256(content).hexdigest(), stat.st_mtime_ns


def _new_files(before, after) -> set[str]:
    existing = {entry[0] for entry in before}
    return {entry[0] for entry in after if entry[0] not in existing}


def _adapter_module(module_name: str, *function_names: str) -> types.ModuleType:
    module = types.ModuleType(module_name)
    for function_name in function_names:
        setattr(module, function_name, MagicMock(name=function_name))
    return module


class _FailingAtomicFile:
    def __init__(self, file, failure_method):
        self._file = file
        self._failure_method = failure_method

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._file.close()

    def write(self, value):
        if self._failure_method == "write":
            raise OSError("injected write failure")
        return self._file.write(value)

    def flush(self):
        if self._failure_method == "flush":
            raise OSError("injected flush failure")
        return self._file.flush()

    def fileno(self):
        return self._file.fileno()


def _invoke_default_with_firewall(arguments):
    return _invoke_with_firewall(arguments, allow_baseline_writes=False)


def _invoke_update_with_firewall(arguments):
    return _invoke_with_firewall(arguments, allow_baseline_writes=True)


def _invoke_with_firewall(arguments, allow_baseline_writes):
    slack = _adapter_module("agent.slack", "send_slack_alert")
    slack_alerts = _adapter_module("agent.slack_alerts", "send_slack_alert")
    dotenv = _adapter_module("dotenv", "load_dotenv")
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
    previous_quality_checker = sys.modules.pop("agent.quality_checker", None)
    original_import = builtins.__import__
    original_open = builtins.open
    original_io_open = io.open
    original_connect = sqlite3.connect

    def tracking_import(name, *args, **kwargs):
        imported_modules.append(name)
        return original_import(name, *args, **kwargs)

    def guarded_open(file, mode="r", *args, **kwargs):
        if (
            not allow_baseline_writes
            and any(marker in str(mode) for marker in ("w", "a", "x", "+"))
        ):
            raise AssertionError(f"filesystem write attempted: {file} ({mode})")
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if (
            not allow_baseline_writes
            and any(marker in str(mode) for marker in ("w", "a", "x", "+"))
        ):
            raise AssertionError(f"filesystem write attempted: {file} ({mode})")
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_connect(database, *args, **kwargs):
        database_text = str(database)
        if kwargs.get("uri") is not True or "mode=ro" not in database_text:
            raise AssertionError(
                f"non-read-only SQLite connection attempted: {database_text}"
            )
        return original_connect(database, *args, **kwargs)

    boundary_calls = {
        "slack": slack.send_slack_alert,
        "slack_alerts": slack_alerts.send_slack_alert,
        "dotenv": dotenv.load_dotenv,
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

    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    sys.modules,
                    {
                        "agent.github_app.client": github_client,
                        "agent.github_pr": github,
                        "agent.groq_client": groq,
                        "agent.slack": slack,
                        "agent.slack_alerts": slack_alerts,
                        "dotenv": dotenv,
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
            boundary_calls["sqlite_connect"] = stack.enter_context(
                patch("sqlite3.connect", side_effect=guarded_connect)
            )
            for target in (
                "os.system",
                "subprocess.run",
                "subprocess.Popen",
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "urllib.request.urlopen",
                "socket.create_connection",
                "socket.socket",
            ):
                boundary_calls[target] = stack.enter_context(patch(target))
            if not allow_baseline_writes:
                for target in (
                    "pathlib.Path.write_text",
                    "pathlib.Path.write_bytes",
                    "pathlib.Path.touch",
                    "pathlib.Path.mkdir",
                    "os.mkdir",
                    "os.makedirs",
                    "os.rename",
                    "os.replace",
                ):
                    boundary_calls[target] = stack.enter_context(patch(target))

            result = CliRunner().invoke(cli, arguments)
    finally:
        sys.modules.pop("agent.quality_checker", None)
        if previous_quality_checker is not None:
            sys.modules["agent.quality_checker"] = previous_quality_checker

    boundary_calls["imported_modules"] = imported_modules
    return result, boundary_calls


def _assert_external_firewall_clean(test_case, firewall):
    for boundary_name, boundary in firewall.items():
        if boundary_name in {"imported_modules", "sqlite_connect"}:
            continue
        with test_case.subTest(boundary=boundary_name):
            boundary.assert_not_called()
    test_case.assertFalse(
        any(
            name == "dotenv"
            or name in ("agent.slack", "agent.slack_alerts")
            or name.startswith("agent.groq")
            or name.startswith("agent.github")
            for name in firewall["imported_modules"]
        ),
        firewall["imported_modules"],
    )


def _assert_readonly_firewall_clean(
    test_case,
    firewall,
    *,
    expected_connections=1,
):
    _assert_external_firewall_clean(test_case, firewall)
    test_case.assertEqual(
        firewall["sqlite_connect"].call_count,
        expected_connections,
    )


def _run_quality_subprocess(
    working_directory: Path,
    environment: dict,
    project: Path,
    database: Path,
    baseline_dir: Path,
    *,
    project_id: str = PROJECT_ID,
    update: bool = False,
):
    arguments = [
        sys.executable,
        "-B",
        str(MAIN_PATH),
        "quality",
        "--project",
        str(project),
        "--project-id",
        project_id,
        "--db",
        str(database),
        "--baseline-dir",
        str(baseline_dir),
    ]
    if update:
        arguments.append("--update-baseline")
    return subprocess.run(
        arguments,
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_injected_update_failure_subprocess(
    working_directory: Path,
    environment: dict,
    project: Path,
    database: Path,
    baseline_dir: Path,
):
    arguments = [
        "quality",
        "--project",
        str(project),
        "--project-id",
        PROJECT_ID,
        "--db",
        str(database),
        "--baseline-dir",
        str(baseline_dir),
        "--update-baseline",
    ]
    script = f"""
import sys
import agent.quality_checker as quality_checker
from agent.cli import cli

def fail_replace(*args, **kwargs):
    raise OSError("injected replacement failure")

quality_checker.os.replace = fail_replace
sys.argv = ["main.py", *{json.dumps(arguments)}]
cli()
"""
    subprocess_environment = environment.copy()
    existing_python_path = subprocess_environment.get("PYTHONPATH")
    subprocess_environment["PYTHONPATH"] = os.pathsep.join(
        path
        for path in (str(REPOSITORY_ROOT), existing_python_path)
        if path
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=working_directory,
        env=subprocess_environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _combined(process) -> str:
    return process.stdout + process.stderr


if __name__ == "__main__":
    unittest.main()
