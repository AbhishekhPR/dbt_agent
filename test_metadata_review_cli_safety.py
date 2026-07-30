import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


REPO_ROOT = Path(__file__).resolve().parent
MAIN_PATH = REPO_ROOT / "main.py"


def _manifest(model_name="stg_orders", sql="select order_id from raw_orders"):
    unique_id = f"model.safety.{model_name}"
    return {
        "metadata": {"project_name": "safety", "dbt_version": "1.8.0"},
        "nodes": {
            unique_id: {
                "resource_type": "model",
                "name": model_name,
                "unique_id": unique_id,
                "original_file_path": f"models/{model_name}.sql",
                "raw_code": sql,
                "columns": {"order_id": {"name": "order_id"}},
            }
        },
    }


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_metadata_db(path, *, high_drift=True):
    from agent.metadata_store import (
        ModelMetricRecord,
        ScanRunRecord,
        insert_model_metrics,
        insert_scan_run,
    )

    path = Path(path)
    previous_id = insert_scan_run(
        path,
        ScanRunRecord(
            project_name="safety",
            model_name="stg_orders",
            risk_level="LOW",
            safe_to_merge=True,
            scan_id="scan-previous",
            timestamp="2026-07-01T00:00:00+00:00",
        ),
    )
    insert_model_metrics(
        path,
        ModelMetricRecord(
            scan_id=previous_id,
            project_name="safety",
            model_name="stg_orders",
            row_count=100,
            null_count=0,
            duplicate_count=0,
            freshness_timestamp="2026-07-01T00:00:00+00:00",
            schema_column_count=2,
            timestamp="2026-07-01T00:00:00+00:00",
        ),
    )
    current_id = insert_scan_run(
        path,
        ScanRunRecord(
            project_name="safety",
            model_name="stg_orders",
            risk_level="HIGH" if high_drift else "LOW",
            safe_to_merge=not high_drift,
            scan_id="scan-current",
            timestamp="2026-07-02T00:00:00+00:00",
        ),
    )
    insert_model_metrics(
        path,
        ModelMetricRecord(
            scan_id=current_id,
            project_name="safety",
            model_name="stg_orders",
            row_count=25 if high_drift else 100,
            null_count=10 if high_drift else 0,
            duplicate_count=5 if high_drift else 0,
            freshness_timestamp="2026-06-30T00:00:00+00:00",
            schema_column_count=3 if high_drift else 2,
            timestamp="2026-07-02T00:00:00+00:00",
        ),
    )
    return path


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


def _tree_manifest(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): _snapshot(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _drift_count(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM relium_metric_drifts"
        ).fetchone()[0]
    finally:
        connection.close()


def _run_main(args, cwd):
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(MAIN_PATH), *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _review_args(manifest_path, root):
    return [
        "review-deployment",
        "--dbt-manifest",
        str(manifest_path),
        "--changed-model",
        "stg_orders",
        "--history-path",
        str(Path(root) / "history.json"),
        "--outcomes-path",
        str(Path(root) / "outcomes.json"),
    ]


def _backtest_args(manifest_path, baseline_path, root):
    return [
        "backtest-deployment",
        "--dbt-manifest",
        str(manifest_path),
        "--baseline-manifest",
        str(baseline_path),
        "--changed-model",
        "stg_orders",
        "--history-path",
        str(Path(root) / "history.json"),
    ]


class MetadataReadSafetyTests(unittest.TestCase):
    def test_missing_metadata_database_is_not_created(self):
        from agent.metadata_store import MetadataStoreError, fetch_recent_model_metrics

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nested" / "missing.db"

            with self.assertRaisesRegex(MetadataStoreError, "does not exist"):
                fetch_recent_model_metrics(missing)

            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

    def test_reads_use_escaped_read_only_sqlite_uris(self):
        from agent import metadata_store

        cases = [
            ("metadata space.db", "%20"),
            ("metadata#hash.db", "%23"),
            ("metadata%percent.db", "%25"),
            ("métadonnées.db", "%C3%A9"),
        ]
        for name, escaped_fragment in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                db_path = _seed_metadata_db(Path(tmp) / name)
                real_connect = sqlite3.connect
                calls = []

                def connect(database, *args, **kwargs):
                    calls.append((database, dict(kwargs)))
                    return real_connect(database, *args, **kwargs)

                with patch.object(
                    metadata_store,
                    "initialize_metadata_db",
                    side_effect=AssertionError("read attempted initialization"),
                ), patch.object(metadata_store.sqlite3, "connect", side_effect=connect):
                    rows = metadata_store.fetch_recent_model_metrics(
                        db_path,
                        project_name="safety",
                        model_name="stg_orders",
                    )

                self.assertEqual(len(rows), 2)
                self.assertTrue(calls)
                self.assertTrue(all(call[1].get("uri") is True for call in calls))
                self.assertTrue(all("mode=ro" in str(call[0]) for call in calls))
                self.assertTrue(all(str(call[0]).startswith("file:") for call in calls))
                self.assertTrue(
                    all(escaped_fragment in str(call[0]) for call in calls)
                )

    def test_relative_metadata_path_is_resolved_read_only(self):
        from agent.metadata_store import fetch_recent_model_metrics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = _seed_metadata_db(root / "relative metadata.db")
            before = _snapshot(db_path)
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                rows = fetch_recent_model_metrics(
                    Path("relative metadata.db"),
                    project_name="safety",
                    model_name="stg_orders",
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(len(rows), 2)
            self.assertEqual(_snapshot(db_path), before)

    def test_missing_tables_and_locked_database_fail_cleanly(self):
        from agent.metadata_store import MetadataStoreError, fetch_recent_model_metrics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_tables = root / "missing-tables.db"
            connection = sqlite3.connect(missing_tables)
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(MetadataStoreError, "no such table"):
                fetch_recent_model_metrics(missing_tables)

            locked = _seed_metadata_db(root / "locked.db")
            locker = sqlite3.connect(locked, timeout=0)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                with self.assertRaisesRegex(MetadataStoreError, "locked"):
                    fetch_recent_model_metrics(locked)
            finally:
                locker.rollback()
                locker.close()

    def test_invalid_metadata_inputs_fail_without_mutation(self):
        from agent.metadata_store import MetadataStoreError, fetch_recent_model_metrics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_inputs = [
                root,
                root / "empty.db",
                root / "not-sqlite.db",
            ]
            invalid_inputs[1].write_bytes(b"")
            invalid_inputs[2].write_text("not sqlite", encoding="utf-8")

            before = _tree_manifest(root)
            for path in invalid_inputs:
                with self.subTest(path=path), self.assertRaises(MetadataStoreError):
                    fetch_recent_model_metrics(path)

            self.assertEqual(_tree_manifest(root), before)
            self.assertFalse(any(root.glob("*.db-journal")))
            self.assertFalse(any(root.glob("*.db-wal")))
            self.assertFalse(any(root.glob("*.db-shm")))

    def test_drift_comparison_does_not_record_or_modify_database(self):
        from agent.metadata_drift import compare_last_run

        with tempfile.TemporaryDirectory() as tmp:
            db_path = _seed_metadata_db(Path(tmp) / "metadata.db")
            before = _snapshot(db_path)
            before_count = _drift_count(db_path)

            result = compare_last_run(
                db_path=db_path,
                project_name="safety",
                model_name="stg_orders",
            )

            self.assertEqual(result["drift_level"], "HIGH")
            self.assertEqual(result["row_count_change_pct"], -75.0)
            self.assertEqual(_drift_count(db_path), before_count)
            self.assertEqual(_snapshot(db_path), before)
            self.assertFalse(Path(f"{db_path}-journal").exists())
            self.assertFalse(Path(f"{db_path}-wal").exists())
            self.assertFalse(Path(f"{db_path}-shm").exists())

    def test_drift_comparison_does_not_require_write_only_drift_table(self):
        from agent.metadata_drift import compare_last_run

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics-only.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE relium_model_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        row_count INTEGER,
                        null_count INTEGER,
                        duplicate_count INTEGER,
                        freshness_timestamp TEXT,
                        schema_column_count INTEGER
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO relium_model_metrics (
                        scan_id, timestamp, project_name, model_name,
                        row_count, null_count, duplicate_count,
                        freshness_timestamp, schema_column_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "previous",
                            "2026-07-01T00:00:00+00:00",
                            "safety",
                            "stg_orders",
                            100,
                            0,
                            0,
                            "2026-07-01T00:00:00+00:00",
                            2,
                        ),
                        (
                            "current",
                            "2026-07-02T00:00:00+00:00",
                            "safety",
                            "stg_orders",
                            25,
                            10,
                            5,
                            "2026-06-30T00:00:00+00:00",
                            3,
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            before = _snapshot(db_path)

            result = compare_last_run(
                db_path,
                project_name="safety",
                model_name="stg_orders",
            )

            self.assertEqual(result["drift_level"], "HIGH")
            self.assertEqual(_snapshot(db_path), before)


class MetadataCliSafetyTests(unittest.TestCase):
    def test_compare_last_run_requires_explicit_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_main(["compare-last-run"], tmp)

        self.assertEqual(result.returncode, 2)
        self.assertIn("Missing option '--db'", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_compare_last_run_normalizes_invalid_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "missing.db", root / "empty.db", root / "bad.db", root]
            paths[1].write_bytes(b"")
            paths[2].write_text("not sqlite", encoding="utf-8")

            for path in paths:
                with self.subTest(path=path):
                    result = _run_main(
                        ["compare-last-run", "--db", str(path)],
                        root,
                    )
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("Error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

            self.assertFalse(paths[0].exists())

    def test_compare_last_run_real_entry_point_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = _seed_metadata_db(root / "metadata # % ü.db")
            before = _tree_manifest(root)
            before_count = _drift_count(db_path)

            result = _run_main(
                [
                    "compare-last-run",
                    "--db",
                    str(db_path),
                    "--project",
                    "safety",
                    "--model",
                    "stg_orders",
                ],
                root,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Metadata Drift: HIGH", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(_drift_count(db_path), before_count)
            self.assertEqual(_tree_manifest(root), before)

    def test_compare_last_run_insufficient_history_is_controlled(self):
        from agent.metadata_store import initialize_metadata_db

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = initialize_metadata_db(root / "metadata.db")
            before = _snapshot(db_path)

            result = _run_main(
                ["compare-last-run", "--db", str(db_path)],
                root,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(_snapshot(db_path), before)


class DeploymentCommandSafetyTests(unittest.TestCase):
    def test_default_review_is_local_read_only_and_skips_metadata(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            before = _tree_manifest(root)
            runner = CliRunner()

            with patch(
                "agent.metadata_store.sqlite3.connect",
                side_effect=AssertionError("unexpected SQLite access"),
            ), patch("dotenv.load_dotenv") as dotenv_load, patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("unexpected HTTP access"),
            ), patch(
                "socket.create_connection",
                side_effect=AssertionError("unexpected socket access"),
            ), patch(
                "subprocess.run",
                side_effect=AssertionError("unexpected subprocess"),
            ), patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected subprocess"),
            ), patch(
                "pathlib.Path.write_text",
                side_effect=AssertionError("unexpected file write"),
            ):
                result = runner.invoke(cli, _review_args(manifest, root))

            self.assertEqual(result.exit_code, 0, result.output)
            dotenv_load.assert_not_called()
            self.assertIn("Metadata Drift: unavailable", result.output)
            self.assertEqual(_tree_manifest(root), before)
            self.assertFalse((root / "history.json").exists())
            self.assertFalse((root / "outcomes.json").exists())

    def test_review_uses_explicit_metadata_database_read_only(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            db_path = _seed_metadata_db(root / "metadata.db")
            before = _tree_manifest(root)
            before_count = _drift_count(db_path)

            result = CliRunner().invoke(
                cli,
                [
                    *_review_args(manifest, root),
                    "--metadata-db",
                    str(db_path),
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Metadata Drift: evaluated", result.output)
            self.assertEqual(_drift_count(db_path), before_count)
            self.assertEqual(_tree_manifest(root), before)

    def test_review_invalid_explicit_metadata_database_is_controlled(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            invalid = root / "invalid.db"
            invalid.write_text("not sqlite", encoding="utf-8")

            result = CliRunner().invoke(
                cli,
                [*_review_args(manifest, root), "--metadata-db", str(invalid)],
            )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("Error:", result.output)
            self.assertNotIn("Traceback", result.output)

    def test_insufficient_explicit_metadata_is_unavailable_for_review_and_backtest(self):
        from agent.cli import cli
        from agent.metadata_store import initialize_metadata_db

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())
            db_path = initialize_metadata_db(root / "metadata.db")
            before = _snapshot(db_path)

            review = CliRunner().invoke(
                cli,
                [*_review_args(manifest, root), "--metadata-db", str(db_path)],
            )
            backtest = CliRunner().invoke(
                cli,
                [
                    *_backtest_args(manifest, baseline, root),
                    "--metadata-db",
                    str(db_path),
                ],
            )

            self.assertEqual(review.exit_code, 0, review.output)
            self.assertEqual(backtest.exit_code, 0, backtest.output)
            self.assertIn("Metadata Drift: unavailable", review.output)
            self.assertIn("Metadata Drift: unavailable", backtest.output)
            self.assertEqual(_snapshot(db_path), before)

    def test_default_backtest_is_local_read_only_and_skips_metadata(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())
            before = _tree_manifest(root)

            with patch(
                "agent.metadata_store.sqlite3.connect",
                side_effect=AssertionError("unexpected SQLite access"),
            ), patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("unexpected HTTP access"),
            ), patch(
                "socket.create_connection",
                side_effect=AssertionError("unexpected socket access"),
            ), patch(
                "subprocess.run",
                side_effect=AssertionError("unexpected subprocess"),
            ), patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected subprocess"),
            ), patch(
                "pathlib.Path.write_text",
                side_effect=AssertionError("unexpected file write"),
            ):
                result = CliRunner().invoke(
                    cli,
                    _backtest_args(manifest, baseline, root),
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Metadata Drift: unavailable", result.output)
            self.assertEqual(_tree_manifest(root), before)
            self.assertFalse((root / "history.json").exists())

    def test_backtest_uses_explicit_metadata_database_read_only(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())
            db_path = _seed_metadata_db(root / "metadata.db")
            before = _tree_manifest(root)
            before_count = _drift_count(db_path)

            result = CliRunner().invoke(
                cli,
                [
                    *_backtest_args(manifest, baseline, root),
                    "--metadata-db",
                    str(db_path),
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Metadata Drift: evaluated", result.output)
            self.assertEqual(_drift_count(db_path), before_count)
            self.assertEqual(_tree_manifest(root), before)

    def test_backtest_invalid_explicit_metadata_database_is_controlled(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())
            invalid = root / "invalid.db"
            invalid.write_text("not sqlite", encoding="utf-8")

            result = CliRunner().invoke(
                cli,
                [
                    *_backtest_args(manifest, baseline, root),
                    "--metadata-db",
                    str(invalid),
                ],
            )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("Error:", result.output)
            self.assertNotIn("Traceback", result.output)

    def test_explicit_review_writes_are_isolated(self):
        from agent.cli import cli
        from agent.deployment_history import DeploymentHistoryStore

        baseline_snapshot = {
            "snapshot_id": "baseline",
            "deployment_id": "baseline",
            "created_at": "2026-07-01T00:00:00+00:00",
            "changed_models": ["stg_orders"],
            "semantic_context": {},
            "decision": {"decision": "ALLOW", "health": 100},
            "incident_summary": {"decision": "ALLOW"},
            "metadata": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            history = root / "state" / "history.json"
            DeploymentHistoryStore(history).save_snapshot(baseline_snapshot)
            output = root / "reports" / "review.txt"
            db_path = _seed_metadata_db(root / "metadata.db", high_drift=False)
            db_before = _snapshot(db_path)

            result = CliRunner().invoke(
                cli,
                [
                    *_review_args(manifest, root),
                    "--history-path",
                    str(history),
                    "--metadata-db",
                    str(db_path),
                    "--auto-record",
                    "--allow-blocked-recording",
                    "--output",
                    str(output),
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(output.is_file())
            self.assertEqual(_snapshot(db_path), db_before)
            self.assertGreaterEqual(
                len(DeploymentHistoryStore(history).list_snapshots()),
                2,
            )
            changed = set(_tree_manifest(root)) - {
                "manifest.json",
                "metadata.db",
                "state/history.json",
            }
            self.assertEqual(changed, {"reports/review.txt"})

    def test_invalid_output_destination_is_controlled(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())

            for args in [
                [*_review_args(manifest, root), "--output", str(root)],
                [*_backtest_args(manifest, baseline, root), "--output", str(root)],
            ]:
                with self.subTest(command=args[0]):
                    result = CliRunner().invoke(cli, args)
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertIn("Error:", result.output)
                    self.assertNotIn("Traceback", result.output)

    def test_failed_report_replacement_preserves_existing_output(self):
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            output = root / "review.txt"
            output.write_text("previous report", encoding="utf-8")

            with patch(
                "pathlib.Path.replace",
                side_effect=OSError("replacement failed"),
            ):
                result = CliRunner().invoke(
                    cli,
                    [*_review_args(manifest, root), "--output", str(output)],
                )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("Error:", result.output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report")
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["manifest.json", "review.txt"],
            )

    def test_auto_record_precedes_report_write_without_metadata_mutation(self):
        from agent.cli import cli
        from agent.deployment_history import DeploymentHistoryStore

        baseline_snapshot = {
            "snapshot_id": "baseline",
            "deployment_id": "baseline",
            "created_at": "2026-07-01T00:00:00+00:00",
            "changed_models": ["stg_orders"],
            "semantic_context": {},
            "decision": {"decision": "ALLOW", "health": 100},
            "incident_summary": {"decision": "ALLOW"},
            "metadata": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            history = root / "history.json"
            DeploymentHistoryStore(history).save_snapshot(baseline_snapshot)
            db_path = _seed_metadata_db(root / "metadata.db", high_drift=False)
            db_before = _snapshot(db_path)
            output = root / "review.txt"
            output.write_text("previous report", encoding="utf-8")
            real_replace = Path.replace

            def replace(source, target):
                if Path(target) == output:
                    raise OSError("report replacement failed")
                return real_replace(source, target)

            with patch("pathlib.Path.replace", autospec=True, side_effect=replace):
                result = CliRunner().invoke(
                    cli,
                    [
                        *_review_args(manifest, root),
                        "--history-path",
                        str(history),
                        "--metadata-db",
                        str(db_path),
                        "--auto-record",
                        "--allow-blocked-recording",
                        "--output",
                        str(output),
                    ],
                )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report")
            self.assertGreaterEqual(
                len(DeploymentHistoryStore(history).list_snapshots()),
                2,
            )
            self.assertEqual(_snapshot(db_path), db_before)

    def test_explicit_metadata_never_resolves_to_ignored_global_database(self):
        from agent import metadata_store
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())
            db_path = _seed_metadata_db(root / "metadata.db")
            real_connect = sqlite3.connect
            opened = []

            def connect(database, *args, **kwargs):
                opened.append(str(database))
                return real_connect(database, *args, **kwargs)

            with patch.object(metadata_store.sqlite3, "connect", side_effect=connect):
                compare = CliRunner().invoke(
                    cli,
                    ["compare-last-run", "--db", str(db_path)],
                )
                review = CliRunner().invoke(
                    cli,
                    [*_review_args(manifest, root), "--metadata-db", str(db_path)],
                )
                backtest = CliRunner().invoke(
                    cli,
                    [
                        *_backtest_args(manifest, baseline, root),
                        "--metadata-db",
                        str(db_path),
                    ],
                )

            self.assertEqual(compare.exit_code, 0, compare.output)
            self.assertEqual(review.exit_code, 0, review.output)
            self.assertEqual(backtest.exit_code, 0, backtest.output)
            self.assertTrue(opened)
            expected_uri = db_path.resolve().as_uri()
            ignored_uri = (REPO_ROOT / "relium_metadata.db").resolve().as_uri()
            self.assertTrue(all(value.startswith(expected_uri) for value in opened))
            self.assertTrue(all(not value.startswith(ignored_uri) for value in opened))

    def test_real_review_and_backtest_subprocesses_do_not_mutate_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_json(root / "manifest.json", _manifest())
            baseline = _write_json(root / "baseline.json", _manifest())
            before = _tree_manifest(root)

            review = _run_main(_review_args(manifest, root), root)
            backtest = _run_main(_backtest_args(manifest, baseline, root), root)

            self.assertEqual(review.returncode, 0, review.stdout + review.stderr)
            self.assertEqual(backtest.returncode, 0, backtest.stdout + backtest.stderr)
            self.assertNotIn("Traceback", review.stderr + backtest.stderr)
            self.assertEqual(_tree_manifest(root), before)

            db_path = _seed_metadata_db(root / "metadata.db")
            with_metadata = _tree_manifest(root)
            review_with_metadata = _run_main(
                [*_review_args(manifest, root), "--metadata-db", str(db_path)],
                root,
            )
            backtest_with_metadata = _run_main(
                [
                    *_backtest_args(manifest, baseline, root),
                    "--metadata-db",
                    str(db_path),
                ],
                root,
            )

            self.assertEqual(
                review_with_metadata.returncode,
                0,
                review_with_metadata.stdout + review_with_metadata.stderr,
            )
            self.assertEqual(
                backtest_with_metadata.returncode,
                0,
                backtest_with_metadata.stdout + backtest_with_metadata.stderr,
            )
            self.assertNotIn(
                "Traceback",
                review_with_metadata.stderr + backtest_with_metadata.stderr,
            )
            self.assertEqual(_tree_manifest(root), with_metadata)


class ImportPurityTests(unittest.TestCase):
    def test_imports_do_not_load_dotenv_or_open_external_boundaries(self):
        script = r"""
import importlib.abc
import os
import socket
import sqlite3
import subprocess
import sys
import urllib.request

class BlockDotenv(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "dotenv" or fullname.startswith("dotenv."):
            raise AssertionError("dotenv imported")
        return None

def fail(*args, **kwargs):
    raise AssertionError("side-effect boundary reached")

sys.meta_path.insert(0, BlockDotenv())
sqlite3.connect = fail
socket.create_connection = fail
subprocess.run = fail
subprocess.Popen = fail
urllib.request.urlopen = fail
os.system = fail
before = dict(os.environ)
import agent.blast_radius
import agent.metadata_store
import agent.metadata_drift
import agent.pr_analysis
import agent.deployment_lifecycle
import agent.deployment_review_service
import agent.backtest
assert os.environ == before
blocked = ("agent.slack", "agent.slack_alerts", "groq", "github", "requests", "httpx")
assert not any(
    name == prefix or name.startswith(prefix + ".")
    for name in sys.modules
    for prefix in blocked
), sorted(name for name in sys.modules if name.startswith(blocked))
"""
        with tempfile.TemporaryDirectory() as tmp:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(REPO_ROOT)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=tmp,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_help_commands_do_not_reach_external_boundaries(self):
        script = r"""
import importlib.abc
import os
import socket
import sqlite3
import subprocess
import sys
import urllib.request
from click.testing import CliRunner

class BlockDotenv(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "dotenv" or fullname.startswith("dotenv."):
            raise AssertionError("dotenv imported")
        return None

def fail(*args, **kwargs):
    raise AssertionError("side-effect boundary reached")

sys.meta_path.insert(0, BlockDotenv())
sqlite3.connect = fail
socket.create_connection = fail
subprocess.run = fail
subprocess.Popen = fail
urllib.request.urlopen = fail
os.system = fail
before = dict(os.environ)
from agent.cli import cli
for command in ("compare-last-run", "review-deployment", "backtest-deployment"):
    result = CliRunner().invoke(cli, [command, "--help"])
    assert result.exit_code == 0, result.output
assert os.environ == before
"""
        with tempfile.TemporaryDirectory() as tmp:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(REPO_ROOT)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=tmp,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HelpContractTests(unittest.TestCase):
    def test_help_discloses_read_only_and_explicit_writes(self):
        from agent.cli import cli

        runner = CliRunner()
        compare = runner.invoke(cli, ["compare-last-run", "--help"])
        review = runner.invoke(cli, ["review-deployment", "--help"])
        backtest = runner.invoke(cli, ["backtest-deployment", "--help"])

        self.assertEqual(compare.exit_code, 0, compare.output)
        self.assertIn("--db", compare.output)
        self.assertIn("read-only", compare.output.lower())
        self.assertIn("does not record drift", compare.output.lower())

        for result in (review, backtest):
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("--metadata-db", result.output)
            self.assertIn("read-only", result.output.lower())

        self.assertIn("updates deployment history", review.output.lower())
        self.assertIn("writes the rendered review", review.output.lower())
        self.assertIn("writes the rendered backtest", backtest.output.lower())


if __name__ == "__main__":
    unittest.main()
