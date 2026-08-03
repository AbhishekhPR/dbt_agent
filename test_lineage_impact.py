import hashlib
import sqlite3
import unittest


class LineageImpactTests(unittest.TestCase):
    def test_lineage_completeness_and_kpi_impact_are_explicit(self):
        from agent.lineage_impact import build_lineage_record

        record = build_lineage_record(
            model="stg_orders",
            upstream_models=["raw_orders"], downstream_models=["mart_revenue"],
            columns={"order_id": ["raw_orders.order_id"]}, kpis=["revenue"],
            manifest_hash="head", expected_commit="abc", manifest_commit="abc",
        )
        self.assertEqual(record["completeness"]["model"], "complete")
        self.assertEqual(record["completeness"]["column"], "complete")
        self.assertEqual(record["affected_kpis"], ["revenue"])

    def test_incomplete_column_lineage_is_disclosed_not_claimed_exhaustive(self):
        from agent.lineage_impact import build_lineage_record

        record = build_lineage_record(model="model", upstream_models=[], downstream_models=["mart"], columns={}, kpis=[], manifest_hash="h", expected_commit="abc", manifest_commit="abc")
        self.assertEqual(record["completeness"]["column"], "incomplete")
        self.assertFalse(record["claims_exhaustive_impact"])

    def test_manifest_commit_mismatch_is_rejected(self):
        from agent.lineage_impact import ManifestBindingError, build_lineage_record

        with self.assertRaises(ManifestBindingError):
            build_lineage_record(model="model", upstream_models=[], downstream_models=[], columns={}, kpis=[], manifest_hash="h", expected_commit="abc", manifest_commit="def")

    def test_lineage_persists_tenant_scoped(self):
        from agent.lineage_impact import persist_lineage
        from agent.sqlite_lifecycle_store import SQLiteLifecycleStore

        store = SQLiteLifecycleStore(sqlite3.connect(":memory:")); store.ensure_schema(); store.ensure_tenant("org", "repo", "prod")
        record = {"model": "m", "deployment_id": "dep", "completeness": {"model": "complete"}}
        saved = persist_lineage(store, "org", "repo", "prod", record)
        self.assertTrue(saved["lineage_id"])
        self.assertEqual(store.list_lineage("org", "repo", "prod")[0]["model"], "m")


if __name__ == "__main__":
    unittest.main()
