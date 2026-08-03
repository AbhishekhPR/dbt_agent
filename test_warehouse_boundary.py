import sqlite3
import unittest


class WarehouseBoundaryTests(unittest.TestCase):
    def test_sqlite_adapter_reads_allowlisted_metadata(self):
        from agent.warehouse import SQLiteWarehouseAdapter

        conn = sqlite3.connect(":memory:")
        conn.execute("create table metrics (id integer, value integer)")
        conn.executemany("insert into metrics values (?, ?)", [(1, 10), (2, None), (2, 12)])
        adapter = SQLiteWarehouseAdapter(conn, allowlist={"metrics"})
        result = adapter.observe("metrics", key_columns=["id"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["distinct_key_count"], 2)
        self.assertEqual(result["duplicate_key_count"], 1)
        self.assertIn("null_rates", result)

    def test_allowlist_and_identifier_validation_are_enforced(self):
        from agent.warehouse import SQLiteWarehouseAdapter, WarehouseSafetyError

        conn = sqlite3.connect(":memory:")
        conn.execute("create table metrics (id integer)")
        adapter = SQLiteWarehouseAdapter(conn, allowlist={"metrics"})
        with self.assertRaises(WarehouseSafetyError):
            adapter.observe("other", key_columns=["id"])
        with self.assertRaises(WarehouseSafetyError):
            adapter.observe("metrics; drop table metrics", key_columns=["id"])

    def test_missing_table_is_not_evaluated_not_synthetic(self):
        from agent.warehouse import SQLiteWarehouseAdapter

        adapter = SQLiteWarehouseAdapter(sqlite3.connect(":memory:"), allowlist={"metrics"})
        result = adapter.observe("metrics", key_columns=["id"])
        self.assertEqual(result["status"], "NOT EVALUATED")
        self.assertIn("missing", result["reason"].lower())

    def test_postgres_adapter_is_read_only_and_credentials_explicit(self):
        from agent.warehouse import PostgresWarehouseAdapter, WarehouseCredentialsError

        adapter = PostgresWarehouseAdapter(dsn=None, allowlist={"metrics"})
        with self.assertRaises(WarehouseCredentialsError):
            adapter.observe("metrics", key_columns=["id"])


if __name__ == "__main__":
    unittest.main()
