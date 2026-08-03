import unittest


class SqlDetectorRegistryTests(unittest.TestCase):
    def test_registry_documents_supported_detectors(self):
        from agent.sql_detectors import detector_registry

        registry = detector_registry()
        for detector_id in {
            "B05_CROSS_JOIN", "B08_DUPLICATE_GENERATING_JOIN",
            "B09_GRAIN_CHANGING_AGGREGATION", "B10_MISSING_DEDUPLICATION",
            "B11_UNSAFE_INCREMENTAL_WATERMARK", "C06_LEFT_TO_INNER_JOIN",
        }:
            self.assertIn(detector_id, registry)
            self.assertTrue(registry[detector_id]["owner"])
            self.assertTrue(registry[detector_id]["supported_dialects"])
            self.assertTrue(registry[detector_id]["limitations"])

    def test_cross_join_and_implicit_cartesian_are_findings(self):
        from agent.sql_detectors import run_sql_detectors
        findings = run_sql_detectors("select * from orders cross join parameters", model_name="orders")
        self.assertEqual(findings[0]["finding_type"], "B05_CROSS_JOIN")
        implicit = run_sql_detectors("select * from orders o, customers c", model_name="orders")
        self.assertEqual(implicit[0]["finding_type"], "B05_CROSS_JOIN")

    def test_approved_one_row_parameter_table_is_safe(self):
        from agent.sql_detectors import run_sql_detectors
        findings = run_sql_detectors("select * from orders cross join parameters", model_name="orders", metadata={"approved_cartesian_relations": ["parameters"]})
        self.assertEqual(findings, [])

    def test_duplicate_join_uses_grain_and_uniqueness_metadata(self):
        from agent.sql_detectors import run_sql_detectors
        findings = run_sql_detectors("select o.id, c.status from orders o join customers c on o.customer_id = c.id", model_name="orders", metadata={"declared_grain": ["o.id"], "join_keys": {"customers": ["id"]}, "unique_keys": {"customers": []}, "relationships": {"customers": "many-to-one"}})
        self.assertEqual(findings[0]["finding_type"], "B08_DUPLICATE_GENERATING_JOIN")

    def test_base_head_structural_detectors(self):
        from agent.sql_detectors import run_sql_detectors
        findings = run_sql_detectors("select customer_id, sum(amount) from sales group by customer_id", model_name="sales", base_sql="select customer_id, region, sum(amount) from sales group by customer_id, region", metadata={"declared_grain": ["customer_id", "region"]})
        self.assertEqual(findings[0]["finding_type"], "B09_GRAIN_CHANGING_AGGREGATION")
        findings = run_sql_detectors("select * from events", model_name="events", base_sql="select * from (select *, row_number() over (partition by id order by updated_at desc) as rn from events) x where rn = 1", metadata={"declared_grain": ["id"]})
        self.assertEqual(findings[0]["finding_type"], "B10_MISSING_DEDUPLICATION")
        findings = run_sql_detectors("select * from events where updated_at >= (select max(updated_at) from target)", model_name="events", base_sql="select * from events where updated_at >= (select max(updated_at) from target) - interval '2' day", metadata={"incremental": True, "required_lookback_days": 2})
        self.assertEqual(findings[0]["finding_type"], "B11_UNSAFE_INCREMENTAL_WATERMARK")
        findings = run_sql_detectors("select * from customers c join orders o on c.id = o.customer_id", model_name="customers", base_sql="select * from customers c left join orders o on c.id = o.customer_id")
        self.assertEqual(findings[0]["finding_type"], "C06_LEFT_TO_INNER_JOIN")

    def test_safe_equivalent_rewrite_has_no_finding(self):
        from agent.sql_detectors import run_sql_detectors
        findings = run_sql_detectors("select * from customers c left join orders o on c.id = o.customer_id", model_name="customers", base_sql="select * from customers c left join orders o on c.id = o.customer_id")
        self.assertEqual(findings, [])

    def test_unsupported_dialect_is_explicitly_unsupported(self):
        from agent.sql_detectors import run_sql_detectors

        findings = run_sql_detectors(
            "select * from orders cross join parameters",
            model_name="orders",
            dialect="unknown-dialect",
        )
        self.assertEqual(findings[0]["status"], "UNSUPPORTED")
        self.assertNotEqual(findings[0]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
