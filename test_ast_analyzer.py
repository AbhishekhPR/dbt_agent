import copy
import json
import unittest

from agent.ast_analyzer import run_ast_analysis


class DivisionByZeroRiskTests(unittest.TestCase):
    def test_unguarded_division_is_flagged(self):
        sql = "SELECT revenue / orders_count AS avg_order_value FROM fct_orders"
        report = run_ast_analysis(sql, "fct_orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertIn("DIVISION_BY_ZERO", rules)
        self.assertEqual(report["overall_risk"], "high")

    def test_nullif_guarded_division_is_not_flagged(self):
        sql = "SELECT revenue / NULLIF(orders_count, 0) AS avg_order_value FROM fct_orders"
        report = run_ast_analysis(sql, "fct_orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("DIVISION_BY_ZERO", rules)

    def test_case_when_zero_guard_is_not_flagged(self):
        sql = (
            "SELECT CASE WHEN orders_count = 0 THEN NULL "
            "ELSE revenue / orders_count END AS avg_order_value FROM fct_orders"
        )
        report = run_ast_analysis(sql, "fct_orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("DIVISION_BY_ZERO", rules)

    def test_division_by_nonzero_literal_is_not_flagged(self):
        sql = "SELECT total_minutes / 60 AS total_hours FROM sessions"
        report = run_ast_analysis(sql, "sessions")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("DIVISION_BY_ZERO", rules)


class HardcodedDateFilterRiskTests(unittest.TestCase):
    def test_hardcoded_date_literal_in_where_is_flagged(self):
        sql = "SELECT id FROM orders WHERE order_date >= '2024-01-01'"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertIn("HARDCODED_DATE_FILTER", rules)

    def test_date_literal_inside_line_comment_is_not_flagged(self):
        sql = (
            "SELECT id FROM orders WHERE order_date >= current_date "
            "-- previously hardcoded to '2024-01-01'"
        )
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("HARDCODED_DATE_FILTER", rules)

    def test_date_literal_inside_block_comment_is_not_flagged(self):
        sql = (
            "SELECT id FROM orders /* was WHERE order_date >= '2024-01-01' */ "
            "WHERE order_date >= current_date"
        )
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("HARDCODED_DATE_FILTER", rules)

    def test_date_literal_outside_where_clause_is_not_flagged(self):
        sql = "SELECT '2024-01-01' AS fixed_label, id FROM orders"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("HARDCODED_DATE_FILTER", rules)


class NotEqualNullRiskTests(unittest.TestCase):
    def test_not_equal_without_null_guard_is_flagged(self):
        sql = "SELECT id FROM orders WHERE status != 'cancelled'"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertIn("NOT_EQUAL_NULL_RISK", rules)
        self.assertEqual(report["overall_risk"], "high")

    def test_angle_bracket_not_equal_without_null_guard_is_flagged(self):
        sql = "SELECT id FROM orders WHERE status <> 'cancelled'"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertIn("NOT_EQUAL_NULL_RISK", rules)

    def test_explicit_null_guard_is_not_flagged(self):
        sql = (
            "SELECT id FROM orders "
            "WHERE (status != 'cancelled' OR status IS NULL)"
        )
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("NOT_EQUAL_NULL_RISK", rules)

    def test_not_equal_outside_where_clause_is_not_flagged(self):
        sql = "SELECT status != 'cancelled' AS is_active FROM orders"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("NOT_EQUAL_NULL_RISK", rules)


class ExistingRulesNotDuplicatedTests(unittest.TestCase):
    def test_select_star_reported_once(self):
        sql = "SELECT * FROM orders"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertEqual(rules.count("SELECT_STAR"), 1)

    def test_missing_join_on_reported_once(self):
        sql = "SELECT o.id FROM orders o JOIN customers c"
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertEqual(rules.count("MISSING_JOIN_ON"), 1)

    def test_left_join_nullified_reported_once(self):
        sql = (
            "SELECT o.id FROM orders o "
            "LEFT JOIN customers c ON o.customer_id = c.id "
            "WHERE c.active = 1"
        )
        report = run_ast_analysis(sql, "orders")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertEqual(rules.count("LEFT_JOIN_NULLIFIED"), 1)


class RunAstAnalysisContractTests(unittest.TestCase):
    def test_output_is_deterministic(self):
        sql = (
            "SELECT * FROM orders o LEFT JOIN customers c ON o.customer_id = c.id "
            "WHERE c.active = 1 AND o.status != 'cancelled' "
            "AND o.order_date >= '2024-01-01' AND o.total / o.item_count > 0"
        )
        first = run_ast_analysis(sql, "orders")
        second = run_ast_analysis(sql, "orders")

        self.assertEqual(first, second)

    def test_result_is_json_serializable(self):
        sql = "SELECT * FROM orders WHERE status != 'cancelled'"
        report = run_ast_analysis(sql, "orders")

        serialized = json.dumps(report)
        self.assertIsInstance(serialized, str)

    def test_input_sql_is_not_mutated(self):
        sql = "SELECT * FROM orders WHERE status != 'cancelled'"
        original = copy.deepcopy(sql)

        run_ast_analysis(sql, "orders")

        self.assertEqual(sql, original)


if __name__ == "__main__":
    unittest.main()
