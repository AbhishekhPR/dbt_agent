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
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent.cli import cli


REPOSITORY_ROOT = Path(__file__).resolve().parent
MAIN_PATH = REPOSITORY_ROOT / "main.py"
PROJECT_NAME = "orders_project"


class DiffCliSafetyTests(unittest.TestCase):
    def test_help_discloses_read_only_comparison_and_explicit_snapshot_update(self):
        result = CliRunner().invoke(cli, ["diff", "--help"])
        help_text = result.output.casefold()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("local", help_text)
        self.assertIn("read-only", help_text)
        self.assertIn("existing", help_text)
        self.assertIn("no notifications", help_text)
        self.assertIn("--snapshot-dir", result.output)
        self.assertIn("persisted schema snapshots", help_text)
        self.assertIn("<project>.json", help_text)
        self.assertIn("--update-snapshot", result.output)
        self.assertIn("creates or replaces", help_text)

    def test_import_is_pure_and_does_not_load_credentials_or_slack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPOSITORY_ROOT / "agent", temp_root / "agent")
            (temp_root / ".env").write_text(
                "DIFF_IMPORT_SENTINEL=loaded\n",
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
urllib.request.urlopen = lambda *a, **k: network_calls.append("urlopen")
socket.create_connection = lambda *a, **k: network_calls.append("connect")
socket.socket = lambda *a, **k: network_calls.append("socket")
sqlite3.connect = lambda *a, **k: sqlite_calls.append("connect")
import agent.schema_diff
print(json.dumps({
    "snapshot_exists": pathlib.Path("schema_snapshots").exists(),
    "dotenv_imported": "dotenv" in sys.modules,
    "slack_imported": "agent.slack" in sys.modules,
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
    "sentinel": os.environ.get("DIFF_IMPORT_SENTINEL"),
    "network_calls": network_calls,
    "sqlite_calls": sqlite_calls,
}))
"""
            environment = os.environ.copy()
            environment.pop("DIFF_IMPORT_SENTINEL", None)
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
            self.assertFalse(payload["snapshot_exists"])
            self.assertFalse(payload["dotenv_imported"])
            self.assertFalse(payload["slack_imported"])
            self.assertFalse(payload["ai_or_github_imported"])
            self.assertIsNone(payload["sentinel"])
            self.assertEqual(payload["network_calls"], [])
            self.assertEqual(payload["sqlite_calls"], [])
            self.assertEqual(before, after)

    def test_missing_database_is_a_controlled_error_without_creating_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "missing.sqlite"
            snapshot_dir = root / "snapshots"
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _diff_arguments(database, snapshot_dir)
            )
            after = _manifest(root)
            database_exists = database.exists()
            snapshot_dir_exists = snapshot_dir.exists()

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("SQLite database not found", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        self.assertFalse(database_exists)
        self.assertFalse(snapshot_dir_exists)
        _assert_external_firewall_clean(self, firewall)
        firewall["sqlite_connect"].assert_not_called()

    def test_invalid_database_inputs_fail_cleanly_without_modification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory_database = root / "database-directory"
            directory_database.mkdir()
            malformed_database = root / "not-sqlite.db"
            malformed_database.write_text("not a SQLite database", encoding="utf-8")
            snapshot_dir = root / "snapshots"

            for database, expected_message in (
                (directory_database, "SQLite database is not a file"),
                (malformed_database, "Could not read SQLite database"),
            ):
                with self.subTest(database=database.name):
                    before = _manifest(root)
                    result = CliRunner().invoke(
                        cli,
                        _diff_arguments(database, snapshot_dir),
                    )
                    after = _manifest(root)

                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertIn(expected_message, result.output)
                    self.assertNotIn("Traceback", result.output)
                    self.assertEqual(before, after)
                    self.assertFalse(snapshot_dir.exists())

    def test_empty_database_is_rejected_without_creating_sidecars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "empty.sqlite"
            database.touch()
            snapshot_dir = root / "snapshots"
            before = _manifest(root)

            result = CliRunner().invoke(
                cli,
                _diff_arguments(database, snapshot_dir),
            )
            after = _manifest(root)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("SQLite database is empty", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)

    def test_read_only_uri_escapes_special_and_non_ascii_database_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse # percent% 日本.sqlite"
            snapshot_dir = root / "snapshots"
            _create_database(database)
            _write_snapshot(snapshot_dir, PROJECT_NAME, _current_schema())
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _diff_arguments(database, snapshot_dir)
            )
            after = _manifest(root)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(before, after)
        connection_uri = firewall["sqlite_connect"].call_args.args[0]
        self.assertEqual(
            connection_uri,
            f"{database.resolve().as_uri()}?mode=ro",
        )
        self.assertIn("%20", connection_uri)
        self.assertIn("%23", connection_uri)
        self.assertIn("%25", connection_uri)
        self.assertIn("%E6%97%A5", connection_uri.upper())
        self.assertTrue(firewall["sqlite_connect"].call_args.kwargs["uri"])
        self.assertFalse(
            any(
                entry[0].endswith(("-journal", "-wal", "-shm"))
                for entry in after
            ),
            after,
        )
        _assert_readonly_firewall_clean(self, firewall)

    def test_relative_and_supported_symlink_database_paths_are_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            snapshot_dir = root / "snapshots"
            _create_database(database)
            _write_snapshot(snapshot_dir, PROJECT_NAME, _current_schema())
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            relative_before = _manifest(root)
            relative = _run_diff_subprocess(
                root,
                environment,
                Path("warehouse.sqlite"),
                Path("snapshots"),
            )
            relative_after = _manifest(root)

            symlink = root / "linked-warehouse.sqlite"
            try:
                symlink.symlink_to(database)
            except OSError:
                symlink_supported = False
                symlink_result = None
                symlink_before = None
                symlink_after = None
            else:
                symlink_supported = True
                symlink_before = _manifest(root)
                symlink_result = CliRunner().invoke(
                    cli,
                    _diff_arguments(symlink, snapshot_dir),
                )
                symlink_after = _manifest(root)

        self.assertEqual(relative.returncode, 0, _combined(relative))
        self.assertEqual(relative_before, relative_after)
        self.assertNotIn("Traceback", _combined(relative))
        if symlink_supported:
            self.assertEqual(
                symlink_result.exit_code,
                0,
                symlink_result.output,
            )
            self.assertEqual(symlink_before, symlink_after)
            self.assertNotIn("Traceback", symlink_result.output)

    def test_unreadable_database_error_is_controlled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            _create_database(database)
            snapshot_dir = root / "snapshots"

            with patch(
                "sqlite3.connect",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ):
                result = CliRunner().invoke(
                    cli,
                    _diff_arguments(database, snapshot_dir),
                )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Could not read SQLite database", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_locked_and_permission_denied_database_errors_are_controlled(self):
        for detail in (
            "database is locked",
            "permission denied",
        ):
            with self.subTest(detail=detail):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    database = root / "warehouse.sqlite"
                    snapshot_dir = root / "snapshots"
                    _create_database(database)
                    before = _manifest(root)

                    with patch(
                        "sqlite3.connect",
                        side_effect=sqlite3.OperationalError(detail),
                    ):
                        result = CliRunner().invoke(
                            cli,
                            _diff_arguments(database, snapshot_dir),
                        )
                    after = _manifest(root)

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Could not read SQLite database", result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(before, after)

    def test_missing_snapshot_is_controlled_and_default_remains_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            snapshot_dir = root / "absent-snapshots"
            _create_database(database)
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _diff_arguments(database, snapshot_dir)
            )
            after = _manifest(root)
            snapshot_dir_exists = snapshot_dir.exists()

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(
            "No schema snapshot exists. Run again with --update-snapshot to create one.",
            result.output,
        )
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        self.assertFalse(snapshot_dir_exists)
        _assert_readonly_firewall_clean(self, firewall)

    def test_unchanged_comparison_preserves_database_and_snapshot_exactly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            snapshot_dir = root / "snapshots"
            _create_database(database)
            snapshot = _write_snapshot(
                snapshot_dir,
                PROJECT_NAME,
                _current_schema(),
            )
            before = _manifest(root)
            snapshot_stat = snapshot.stat()

            result, firewall = _invoke_default_with_firewall(
                _diff_arguments(database, snapshot_dir)
            )
            after = _manifest(root)
            snapshot_mtime_after = snapshot.stat().st_mtime_ns

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No schema changes detected", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        self.assertEqual(snapshot_stat.st_mtime_ns, snapshot_mtime_after)
        _assert_readonly_firewall_clean(self, firewall)

    def test_changed_comparison_renders_all_changes_without_mutation_or_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            snapshot_dir = root / "snapshots"
            _create_changed_database(database)
            _write_snapshot(snapshot_dir, PROJECT_NAME, _previous_schema())
            before = _manifest(root)

            result, firewall = _invoke_default_with_firewall(
                _diff_arguments(database, snapshot_dir)
            )
            after = _manifest(root)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("New table detected: 'added_table'", result.output)
        self.assertIn("Column 'legacy' dropped from 'orders'", result.output)
        self.assertIn("Possible rename in 'orders'", result.output)
        self.assertIn("Type changed on 'orders.amount'", result.output)
        self.assertIn("New column 'new_column' added to 'orders'", result.output)
        self.assertIn("Table 'retired_table' no longer exists", result.output)
        self.assertIn("[CRITICAL]", result.output)
        self.assertIn("[HIGH]", result.output)
        self.assertNotIn("Slack", result.output)
        self.assertNotIn("webhook", result.output.casefold())
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, after)
        _assert_readonly_firewall_clean(self, firewall)

    def test_malformed_snapshots_fail_cleanly_and_remain_unchanged(self):
        malformed_snapshots = (
            ("invalid JSON", "{not-json", "not valid JSON"),
            ("wrong top-level type", "[]", "must contain a JSON object"),
            (
                "table columns are not a list",
                json.dumps({"orders": {}}),
                "invalid structure",
            ),
            (
                "column is not an object",
                json.dumps({"orders": ["id"]}),
                "invalid structure",
            ),
            (
                "column name is missing",
                json.dumps({"orders": [{"type": "INTEGER"}]}),
                "invalid structure",
            ),
            (
                "column type is missing",
                json.dumps({"orders": [{"name": "id"}]}),
                "invalid structure",
            ),
            (
                "duplicate column",
                json.dumps(
                    {
                        "orders": [
                            {"name": "id", "type": "INTEGER"},
                            {"name": "ID", "type": "TEXT"},
                        ]
                    }
                ),
                "invalid structure",
            ),
        )

        for label, contents, expected_message in malformed_snapshots:
            with self.subTest(snapshot=label):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    database = root / "warehouse.sqlite"
                    snapshot_dir = root / "snapshots"
                    _create_database(database)
                    snapshot_dir.mkdir()
                    snapshot = snapshot_dir / f"{PROJECT_NAME}.json"
                    snapshot.write_text(contents, encoding="utf-8")
                    before = _manifest(root)

                    result = CliRunner().invoke(
                        cli,
                        _diff_arguments(database, snapshot_dir),
                    )
                    after = _manifest(root)

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(expected_message, result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(before, after)

    def test_unreadable_snapshot_metadata_is_a_controlled_error(self):
        from agent.schema_diff import (
            SchemaDiffError,
            load_snapshot,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_dir = Path(temp_dir) / "snapshots"
            with patch.object(
                Path,
                "exists",
                side_effect=PermissionError("permission denied"),
            ):
                try:
                    load_snapshot(PROJECT_NAME, snapshot_dir)
                except Exception as error:
                    observed_error = error
                else:
                    observed_error = None

        self.assertIsInstance(observed_error, SchemaDiffError)
        self.assertIn(
            "Could not access schema snapshot",
            str(observed_error),
        )

    def test_snapshot_identity_is_deterministic_and_cannot_escape_directory(self):
        from agent.schema_diff import SchemaDiffError, snapshot_path

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_dir = Path(temp_dir) / "snapshots"
            compatible = snapshot_path("orders_project", snapshot_dir)
            second_project = snapshot_path("billing-project", snapshot_dir)
            same_identity = snapshot_path("orders_project", snapshot_dir)
            separate_storage = snapshot_path(
                "orders_project",
                Path(temp_dir) / "other-snapshots",
            )

            self.assertEqual(
                compatible,
                snapshot_dir.resolve() / "orders_project.json",
            )
            self.assertEqual(
                second_project,
                snapshot_dir.resolve() / "billing-project.json",
            )
            self.assertNotEqual(compatible, second_project)
            self.assertEqual(compatible, same_identity)
            self.assertNotEqual(compatible, separate_storage)

            for unsafe_name in (
                "",
                ".",
                "..",
                "orders..archive",
                "../escape",
                r"..\escape",
                "/absolute",
                r"C:\absolute",
                "bad?name",
                "bad:name",
                "trailing.",
                "trailing ",
                "CON",
                "LPT1",
            ):
                with self.subTest(project=unsafe_name):
                    with self.assertRaises(SchemaDiffError):
                        snapshot_path(unsafe_name, snapshot_dir)

    def test_explicit_update_creates_and_deterministically_replaces_only_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "warehouse.sqlite"
            snapshot_dir = root / "external-state" / "snapshots"
            _create_database(database)
            database_before = _file_state(database)
            before = _manifest(root)

            first, first_firewall = _invoke_update_with_firewall(
                _diff_arguments(database, snapshot_dir, update=True)
            )
            self.assertEqual(first.exit_code, 0, first.output)
            snapshot = snapshot_dir / f"{PROJECT_NAME}.json"
            first_bytes = snapshot.read_bytes()
            first_manifest = _manifest(root)

            _write_snapshot(
                snapshot_dir,
                PROJECT_NAME,
                {"obsolete": [{"name": "value", "type": "TEXT"}]},
            )
            second, second_firewall = _invoke_update_with_firewall(
                _diff_arguments(database, snapshot_dir, update=True)
            )
            second_bytes = snapshot.read_bytes()
            after = _manifest(root)
            database_after = _file_state(database)
            snapshot_path = str(snapshot.resolve())

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertIn(snapshot_path, first.output)
        self.assertIn(snapshot_path, second.output)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(json.loads(second_bytes), _current_schema())
        self.assertEqual(database_before, database_after)
        self.assertEqual(
            _manifest_delta(before, first_manifest),
            (
                ("external-state", "directory"),
                ("external-state/snapshots", "directory"),
                (f"external-state/snapshots/{PROJECT_NAME}.json", "file"),
            ),
        )
        self.assertEqual(
            {entry[0] for entry in after},
            {entry[0] for entry in first_manifest},
        )
        _assert_readonly_firewall_clean(self, first_firewall)
        _assert_readonly_firewall_clean(self, second_firewall)

    def test_serialization_failure_preserves_snapshot_and_cleans_temporary_file(self):
        _assert_failed_snapshot_update(
            self,
            {"orders": [{"name": object(), "type": "TEXT"}]},
        )

    def test_temporary_write_failure_preserves_snapshot_and_cleans_temporary_file(self):
        _assert_failed_snapshot_update(
            self,
            _current_schema(),
            failure=patch(
                "agent.schema_diff.os.fsync",
                side_effect=OSError("simulated write failure"),
            ),
        )

    def test_replacement_failure_preserves_snapshot_and_cleans_temporary_file(self):
        _assert_failed_snapshot_update(
            self,
            _current_schema(),
            failure=patch(
                "agent.schema_diff.os.replace",
                side_effect=OSError("simulated replacement failure"),
            ),
        )

    def test_real_entry_point_update_failure_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            working_directory = root / "working"
            working_directory.mkdir()
            database = root / "warehouse.sqlite"
            snapshot_dir = root / "snapshots"
            _create_database(database)
            snapshot = _write_snapshot(
                snapshot_dir,
                PROJECT_NAME,
                _previous_schema(),
            )
            snapshot_before = _file_state(snapshot)
            database_before = _file_state(database)
            environment = os.environ.copy()
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = os.pathsep.join(
                path
                for path in (str(REPOSITORY_ROOT), existing_pythonpath)
                if path
            )
            script = """
import runpy
import sys
from unittest.mock import patch

main_path, database, snapshot_dir = sys.argv[1:]
sys.argv = [
    main_path,
    "diff",
    "--project",
    "orders_project",
    "--db",
    database,
    "--snapshot-dir",
    snapshot_dir,
    "--update-snapshot",
]
with patch(
    "agent.schema_diff.os.replace",
    side_effect=OSError("simulated replacement failure"),
):
    runpy.run_path(main_path, run_name="__main__")
"""
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    script,
                    str(MAIN_PATH),
                    str(database),
                    str(snapshot_dir),
                ],
                cwd=working_directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            snapshot_after = _file_state(snapshot)
            database_after = _file_state(database)
            temporary_files = tuple(snapshot_dir.glob(".*.tmp"))
            snapshot_contents = json.loads(snapshot.read_bytes())

        self.assertEqual(completed.returncode, 1, _combined(completed))
        self.assertIn("Could not write schema snapshot", _combined(completed))
        self.assertNotIn("Traceback", _combined(completed))
        self.assertEqual(snapshot_before, snapshot_after)
        self.assertEqual(database_before, database_after)
        self.assertEqual(temporary_files, ())
        self.assertEqual(snapshot_contents, _previous_schema())

    def test_real_entry_point_covers_read_only_and_explicit_update_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            working_directory = root / "working"
            working_directory.mkdir()
            database = root / "warehouse.sqlite"
            changed_database = root / "changed-warehouse.sqlite"
            missing_database = root / "missing.sqlite"
            directory_database = root / "database-directory"
            malformed_database = root / "malformed.sqlite"
            snapshot_dir = root / "snapshots"
            changed_snapshot_dir = root / "changed-snapshots"
            missing_snapshot_dir = root / "missing-snapshots"
            malformed_snapshot_dir = root / "malformed-snapshots"
            _create_database(database)
            _create_changed_database(changed_database)
            directory_database.mkdir()
            malformed_database.write_text(
                "not a SQLite database",
                encoding="utf-8",
            )
            _write_snapshot(snapshot_dir, PROJECT_NAME, _current_schema())
            _write_snapshot(
                changed_snapshot_dir,
                PROJECT_NAME,
                _previous_schema(),
            )
            malformed_snapshot_dir.mkdir()
            (
                malformed_snapshot_dir / f"{PROJECT_NAME}.json"
            ).write_text("{not-json", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "GROQ_API_KEY": "",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SLACK_WEBHOOK_URL": "",
                }
            )

            unchanged_before = _manifest(root)
            unchanged = _run_diff_subprocess(
                working_directory,
                environment,
                database,
                snapshot_dir,
            )
            unchanged_after = _manifest(root)

            missing_db_before = _manifest(root)
            missing_db = _run_diff_subprocess(
                working_directory,
                environment,
                missing_database,
                snapshot_dir,
            )
            missing_db_after = _manifest(root)

            directory_db_before = _manifest(root)
            directory_db = _run_diff_subprocess(
                working_directory,
                environment,
                directory_database,
                snapshot_dir,
            )
            directory_db_after = _manifest(root)

            malformed_db_before = _manifest(root)
            malformed_db = _run_diff_subprocess(
                working_directory,
                environment,
                malformed_database,
                snapshot_dir,
            )
            malformed_db_after = _manifest(root)

            missing_snapshot_before = _manifest(root)
            missing_snapshot = _run_diff_subprocess(
                working_directory,
                environment,
                database,
                missing_snapshot_dir,
            )
            missing_snapshot_after = _manifest(root)

            malformed_snapshot_before = _manifest(root)
            malformed_snapshot = _run_diff_subprocess(
                working_directory,
                environment,
                database,
                malformed_snapshot_dir,
            )
            malformed_snapshot_after = _manifest(root)

            changed_before = _manifest(root)
            changed = _run_diff_subprocess(
                working_directory,
                environment,
                changed_database,
                changed_snapshot_dir,
            )
            changed_after = _manifest(root)

            update_snapshot_dir = root / "explicit-update"
            update_before = _manifest(root)
            update = _run_diff_subprocess(
                working_directory,
                environment,
                database,
                update_snapshot_dir,
                update=True,
            )
            update_after = _manifest(root)
            update_snapshot = update_snapshot_dir / f"{PROJECT_NAME}.json"
            _write_snapshot(
                update_snapshot_dir,
                PROJECT_NAME,
                _previous_schema(),
            )
            replacement_before = _file_states_except(root, update_snapshot)
            replacement_database_before = _file_state(database)
            replacement = _run_diff_subprocess(
                working_directory,
                environment,
                database,
                update_snapshot_dir,
                update=True,
            )
            replacement_after = _file_states_except(root, update_snapshot)
            replacement_database_after = _file_state(database)
            replacement_payload = json.loads(update_snapshot.read_bytes())
            missing_database_exists = missing_database.exists()
            missing_snapshot_exists = missing_snapshot_dir.exists()

        self.assertEqual(unchanged.returncode, 0, _combined(unchanged))
        self.assertEqual(unchanged_before, unchanged_after)
        self.assertEqual(missing_db.returncode, 1, _combined(missing_db))
        self.assertEqual(missing_db_before, missing_db_after)
        self.assertFalse(missing_database_exists)
        self.assertEqual(directory_db.returncode, 1, _combined(directory_db))
        self.assertEqual(directory_db_before, directory_db_after)
        self.assertIn(
            "SQLite database is not a file",
            _combined(directory_db),
        )
        self.assertEqual(malformed_db.returncode, 1, _combined(malformed_db))
        self.assertEqual(malformed_db_before, malformed_db_after)
        self.assertIn(
            "Could not read SQLite database",
            _combined(malformed_db),
        )
        self.assertEqual(
            missing_snapshot.returncode,
            1,
            _combined(missing_snapshot),
        )
        self.assertEqual(missing_snapshot_before, missing_snapshot_after)
        self.assertFalse(missing_snapshot_exists)
        self.assertEqual(
            malformed_snapshot.returncode,
            1,
            _combined(malformed_snapshot),
        )
        self.assertEqual(
            malformed_snapshot_before,
            malformed_snapshot_after,
        )
        self.assertIn(
            "Schema snapshot is not valid JSON",
            _combined(malformed_snapshot),
        )
        self.assertEqual(changed.returncode, 0, _combined(changed))
        self.assertEqual(changed_before, changed_after)
        self.assertIn(
            "Column 'legacy' dropped from 'orders'",
            _combined(changed),
        )
        self.assertEqual(update.returncode, 0, _combined(update))
        self.assertEqual(
            _manifest_delta(update_before, update_after),
            (
                ("explicit-update", "directory"),
                (f"explicit-update/{PROJECT_NAME}.json", "file"),
            ),
        )
        self.assertEqual(replacement.returncode, 0, _combined(replacement))
        self.assertEqual(replacement_before, replacement_after)
        self.assertEqual(
            replacement_database_before,
            replacement_database_after,
        )
        self.assertEqual(replacement_payload, _current_schema())
        for process in (
            unchanged,
            missing_db,
            directory_db,
            malformed_db,
            missing_snapshot,
            malformed_snapshot,
            changed,
            update,
            replacement,
        ):
            combined = _combined(process)
            self.assertNotIn("Traceback", combined)
            self.assertNotIn("slack", combined.casefold())
            self.assertNotIn("webhook", combined.casefold())
            self.assertNotIn("network", combined.casefold())


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE orders (id INTEGER, amount REAL)"
        )
        connection.commit()
    finally:
        connection.close()


def _create_changed_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE orders "
            "(id INTEGER, amount REAL, new_column TEXT)"
        )
        connection.execute("CREATE TABLE added_table (id INTEGER)")
        connection.commit()
    finally:
        connection.close()


def _current_schema() -> dict:
    return {
        "orders": [
            {"name": "id", "type": "INTEGER"},
            {"name": "amount", "type": "REAL"},
        ]
    }


def _previous_schema() -> dict:
    return {
        "orders": [
            {"name": "id", "type": "INTEGER"},
            {"name": "legacy", "type": "TEXT"},
            {"name": "amount", "type": "TEXT"},
        ],
        "retired_table": [{"name": "id", "type": "INTEGER"}],
    }


def _write_snapshot(snapshot_dir: Path, project: str, schema: dict) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / f"{project}.json"
    snapshot.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return snapshot


def _assert_failed_snapshot_update(
    test_case: unittest.TestCase,
    schema: dict,
    *,
    failure=None,
) -> None:
    from agent.schema_diff import SchemaDiffError, save_snapshot

    with tempfile.TemporaryDirectory() as temp_dir:
        snapshot_dir = Path(temp_dir) / "snapshots"
        snapshot = _write_snapshot(
            snapshot_dir,
            PROJECT_NAME,
            _previous_schema(),
        )
        before = _file_state(snapshot)
        failure_context = failure if failure is not None else nullcontext()

        with failure_context:
            with test_case.assertRaises(SchemaDiffError):
                save_snapshot(PROJECT_NAME, schema, snapshot_dir)

        test_case.assertEqual(before, _file_state(snapshot))
        test_case.assertEqual(tuple(snapshot_dir.glob(".*.tmp")), ())


def _diff_arguments(
    database: Path,
    snapshot_dir: Path,
    *,
    project: str = PROJECT_NAME,
    update: bool = False,
) -> list[str]:
    arguments = [
        "diff",
        "--project",
        project,
        "--db",
        str(database),
        "--snapshot-dir",
        str(snapshot_dir),
    ]
    if update:
        arguments.append("--update-snapshot")
    return arguments


def _manifest(root: Path) -> tuple[tuple[str, str, int | None, str | None, int], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        if path.is_dir():
            entries.append((relative, "directory", None, None, stat.st_mtime_ns))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(
                (relative, "file", stat.st_size, digest, stat.st_mtime_ns)
            )
    return tuple(entries)


def _file_state(path: Path) -> tuple[int, str, int]:
    stat = path.stat()
    return (
        stat.st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_mtime_ns,
    )


def _file_states_except(root: Path, excluded: Path) -> dict[str, tuple[int, str, int]]:
    excluded_path = excluded.resolve()
    return {
        path.relative_to(root).as_posix(): _file_state(path)
        for path in root.rglob("*")
        if path.is_file() and path.resolve() != excluded_path
    }


def _manifest_delta(before, after) -> tuple[tuple[str, str], ...]:
    existing = {entry[0] for entry in before}
    return tuple((entry[0], entry[1]) for entry in after if entry[0] not in existing)


def _adapter_module(module_name: str, *function_names: str) -> types.ModuleType:
    module = types.ModuleType(module_name)
    for function_name in function_names:
        setattr(module, function_name, MagicMock(name=function_name))
    return module


def _invoke_default_with_firewall(arguments):
    return _invoke_with_firewall(arguments, allow_snapshot_writes=False)


def _invoke_update_with_firewall(arguments):
    return _invoke_with_firewall(arguments, allow_snapshot_writes=True)


def _invoke_with_firewall(arguments, allow_snapshot_writes):
    slack = _adapter_module("agent.slack", "send_slack_alert")
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
    previous_schema_diff = sys.modules.pop("agent.schema_diff", None)
    original_import = builtins.__import__
    original_open = builtins.open
    original_io_open = io.open
    original_connect = sqlite3.connect

    def tracking_import(name, *args, **kwargs):
        imported_modules.append(name)
        return original_import(name, *args, **kwargs)

    def guarded_open(file, mode="r", *args, **kwargs):
        if (
            not allow_snapshot_writes
            and any(marker in str(mode) for marker in ("w", "a", "x", "+"))
        ):
            raise AssertionError(f"filesystem write attempted: {file} ({mode})")
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if (
            not allow_snapshot_writes
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
            if not allow_snapshot_writes:
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
        sys.modules.pop("agent.schema_diff", None)
        if previous_schema_diff is not None:
            sys.modules["agent.schema_diff"] = previous_schema_diff

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
            or name == "agent.slack"
            or name.startswith("agent.groq")
            or name.startswith("agent.github")
            for name in firewall["imported_modules"]
        ),
        firewall["imported_modules"],
    )


def _assert_readonly_firewall_clean(test_case, firewall):
    _assert_external_firewall_clean(test_case, firewall)
    firewall["sqlite_connect"].assert_called_once()


def _run_diff_subprocess(
    working_directory: Path,
    environment: dict,
    database: Path,
    snapshot_dir: Path,
    *,
    update: bool = False,
):
    arguments = [
        sys.executable,
        "-B",
        str(MAIN_PATH),
        "diff",
        "--project",
        PROJECT_NAME,
        "--db",
        str(database),
        "--snapshot-dir",
        str(snapshot_dir),
    ]
    if update:
        arguments.append("--update-snapshot")
    return subprocess.run(
        arguments,
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _combined(process) -> str:
    return f"{process.stdout}\n{process.stderr}"


if __name__ == "__main__":
    unittest.main()
