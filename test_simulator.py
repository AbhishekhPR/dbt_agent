import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


def _create_orders_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE raw_orders (
            order_id INTEGER,
            customer_id INTEGER,
            status TEXT,
            revenue REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO raw_orders VALUES (?, ?, ?, ?)",
        [
            (1, 10, "completed", 25.0),
            (2, 11, "pending", 15.0),
            (3, 12, "completed", 40.0),
            (4, 13, "cancelled", 8.0),
            (5, 14, "completed", 19.0),
            (6, 15, "pending", 21.0),
        ],
    )
    conn.commit()
    conn.close()


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.db_path = self.tmp_path / "warehouse.db"
        self.baseline_path = self.tmp_path / "quality_baselines"
        self.baseline_path.mkdir()
        _create_orders_db(self.db_path)

        self.baseline = {
            "table": "raw_orders",
            "row_count": 6,
            "columns": ["order_id", "customer_id", "status", "revenue"],
            "null_rates": {
                "order_id": 0.0,
                "customer_id": 0.0,
                "status": 0.0,
                "revenue": 0.0,
            },
            "duplicate_rows": 0,
            "duplicate_rate": 0,
            "distinct_counts": {
                "order_id": 6,
                "customer_id": 6,
                "status": 3,
                "revenue": 6,
            },
        }
        self.baseline_file = self.baseline_path / "raw_orders.json"
        self.baseline_file.write_text(json.dumps(self.baseline, indent=2))

    def tearDown(self):
        self.tmpdir.cleanup()

    def _row_count(self):
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM raw_orders").fetchone()[0]
        conn.close()
        return count

    def test_run_simulation_restores_database_and_baseline_by_default(self):
        from agent import quality_checker
        from agent.quality_checker import get_table_metrics
        from agent.simulator import run_simulation

        expected_baseline = get_table_metrics(str(self.db_path), "raw_orders")
        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.simulator.run_quality_check"
        ) as quality:
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "duplicate_explosion",
            )

        quality.assert_called_once_with("test_project", str(self.db_path))
        self.assertEqual(self._row_count(), 6)
        self.assertEqual(json.loads(self.baseline_file.read_text()), expected_baseline)
        self.assertFalse(Path(str(self.db_path) + ".sim_backup").exists())
        self.assertFalse(self.baseline_file.with_suffix(".json.sim_backup").exists())

    def test_duplicate_simulation_restores_to_clean_synced_baseline(self):
        from agent import quality_checker
        from agent.simulator import run_simulation

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO raw_orders SELECT * FROM raw_orders LIMIT 2")
        conn.commit()
        conn.close()

        stale_baseline = dict(self.baseline)
        stale_baseline["duplicate_rows"] = 0
        stale_baseline["duplicate_rate"] = 0
        self.baseline_file.write_text(json.dumps(stale_baseline, indent=2))

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.quality_checker.send_slack_alert"
        ), patch(
            "agent.quality_checker.create_incident_report",
            return_value="incident.md",
        ), patch(
            "agent.quality_checker.record_table_metrics"
        ), patch(
            "agent.simulator.run_quality_check",
            side_effect=quality_checker.run_quality_check,
        ):
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "duplicate_explosion",
            )

            with patch("builtins.print") as printed:
                quality_checker.run_quality_check("test_project", str(self.db_path))

        output = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("all metrics within normal range", output)
        self.assertNotIn("Duplicate rows increased", output)

    def test_run_simulation_no_restore_leaves_simulated_changes_and_backups(self):
        from agent import quality_checker
        from agent.simulator import run_simulation

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.simulator.run_quality_check"
        ):
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "duplicate_explosion",
                restore_after=False,
            )

        updated_baseline = json.loads(self.baseline_file.read_text())
        self.assertEqual(self._row_count(), 11)
        self.assertEqual(updated_baseline["row_count"], 11)
        self.assertEqual(updated_baseline["duplicate_rows"], 0)
        self.assertEqual(updated_baseline["duplicate_rate"], 0)
        self.assertTrue(Path(str(self.db_path) + ".sim_backup").exists())
        self.assertTrue(self.baseline_file.with_suffix(".json.sim_backup").exists())

    def test_cli_simulate_passes_options_to_simulator(self):
        from agent.cli import cli

        runner = CliRunner()
        with patch("agent.cli.run_simulation") as simulation:
            result = runner.invoke(
                cli,
                [
                    "simulate",
                    "--project",
                    "test_project",
                    "--db",
                    "test_project/db/test.db",
                    "--table",
                    "raw_orders",
                    "--type",
                    "row_count_drop",
                    "--no-restore",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        simulation.assert_called_once_with(
            "test_project",
            "test_project/db/test.db",
            "raw_orders",
            "row_count_drop",
            restore_after=False,
            sync_baseline=True,
        )

    def test_cli_simulate_supports_no_sync_baseline(self):
        from agent.cli import cli

        runner = CliRunner()
        with patch("agent.cli.run_simulation") as simulation:
            result = runner.invoke(
                cli,
                [
                    "simulate",
                    "--project",
                    "test_project",
                    "--db",
                    "test_project/db/test.db",
                    "--table",
                    "raw_orders",
                    "--type",
                    "duplicate_explosion",
                    "--no-sync-baseline",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        simulation.assert_called_once_with(
            "test_project",
            "test_project/db/test.db",
            "raw_orders",
            "duplicate_explosion",
            restore_after=True,
            sync_baseline=False,
        )

    def test_row_count_drop_simulation_raises_baseline_without_changing_rows(self):
        from agent import quality_checker
        from agent.simulator import run_simulation

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.simulator.run_quality_check"
        ):
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "row_count_drop",
                restore_after=False,
            )

        updated_baseline = json.loads(self.baseline_file.read_text())
        self.assertEqual(self._row_count(), 6)
        self.assertEqual(updated_baseline["row_count"], 200)

    def test_row_count_drop_neutralizes_existing_duplicate_baseline(self):
        from agent import quality_checker
        from agent.simulator import run_simulation

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO raw_orders SELECT * FROM raw_orders LIMIT 2")
        conn.commit()
        conn.close()

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.simulator.run_quality_check"
        ):
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "row_count_drop",
                restore_after=False,
            )

        updated_baseline = json.loads(self.baseline_file.read_text())
        self.assertEqual(updated_baseline["row_count"], 200)
        self.assertEqual(updated_baseline["duplicate_rows"], 2)
        self.assertEqual(updated_baseline["duplicate_rate"], 25.0)

    def test_null_explosion_simulation_nulls_status_column(self):
        from agent import quality_checker
        from agent.simulator import run_simulation

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.simulator.run_quality_check"
        ):
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "null_explosion",
                restore_after=False,
            )

        conn = sqlite3.connect(self.db_path)
        null_count = conn.execute(
            "SELECT COUNT(*) FROM raw_orders WHERE status IS NULL"
        ).fetchone()[0]
        conn.close()
        updated_baseline = json.loads(self.baseline_file.read_text())
        self.assertEqual(null_count, 3)
        self.assertEqual(updated_baseline["null_rates"]["status"], 0.0)

    def test_cardinality_explosion_simulation_makes_status_unique(self):
        from agent import quality_checker
        from agent.simulator import run_simulation

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.simulator.run_quality_check"
        ):
            run_simulation(
                "test_project",
                str(self.db_path),
                "raw_orders",
                "cardinality_explosion",
                restore_after=False,
            )

        conn = sqlite3.connect(self.db_path)
        distinct_count = conn.execute(
            "SELECT COUNT(DISTINCT status) FROM raw_orders"
        ).fetchone()[0]
        conn.close()
        updated_baseline = json.loads(self.baseline_file.read_text())
        self.assertEqual(distinct_count, 6)
        self.assertEqual(updated_baseline["distinct_counts"]["status"], 1)


if __name__ == "__main__":
    unittest.main()
