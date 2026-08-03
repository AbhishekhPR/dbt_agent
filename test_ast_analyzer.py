import copy
import json
import unittest

from agent.ast_analyzer import run_ast_analysis, to_signal


class DivisionByZeroRiskTests(unittest.TestCase):
    def test_supported_detector_registry_is_connected_to_ast_reports(self):
        report = run_ast_analysis(
            "select * from customers c join orders o on c.id = o.customer_id",
            "customers",
            base_sql="select * from customers c left join orders o on c.id = o.customer_id",
        )
        self.assertIn("C06_LEFT_TO_INNER_JOIN", [bug["rule"] for bug in report["bugs"]])

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


class IntegerDivisionRiskTests(unittest.TestCase):
    def test_aggregate_integer_division_is_flagged(self):
        sql = (
            "SELECT SUM(order_total) / COUNT(*) AS average_order_value "
            "FROM raw_orders"
        )
        report = run_ast_analysis(sql, "average_order_value")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertIn("INTEGER_DIVISION", rules)

    def test_float_multiplier_prevents_integer_division_finding(self):
        sql = (
            "SELECT SUM(order_total) * 1.0 / COUNT(*) AS average_order_value "
            "FROM raw_orders"
        )
        report = run_ast_analysis(sql, "average_order_value")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("INTEGER_DIVISION", rules)

    def test_explicit_float_cast_prevents_integer_division_finding(self):
        sql = (
            "SELECT CAST(SUM(order_total) AS FLOAT) / COUNT(*) "
            "AS average_order_value FROM raw_orders"
        )
        report = run_ast_analysis(sql, "average_order_value")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("INTEGER_DIVISION", rules)

    def test_nullif_guarded_denominator_is_not_double_reported_as_integer_division(self):
        sql = (
            "SELECT SUM(order_total) / NULLIF(COUNT(*), 0) "
            "AS average_order_value FROM raw_orders"
        )
        report = run_ast_analysis(sql, "average_order_value")

        rules = [bug["rule"] for bug in report["bugs"]]
        self.assertNotIn("INTEGER_DIVISION", rules)

    def test_one_aggregate_expression_shows_both_risks_but_scores_ast_once(self):
        sql = (
            "SELECT SUM(order_total) / COUNT(*) AS average_order_value "
            "FROM raw_orders"
        )
        report = run_ast_analysis(sql, "average_order_value")
        findings = {
            bug["rule"]: bug
            for bug in report["bugs"]
            if bug["rule"] in {"DIVISION_BY_ZERO", "INTEGER_DIVISION"}
        }

        self.assertEqual(
            set(findings),
            {"DIVISION_BY_ZERO", "INTEGER_DIVISION"},
        )
        self.assertEqual(
            findings["DIVISION_BY_ZERO"]["line_reference"],
            "SUM(order_total) / COUNT(*)",
        )
        self.assertEqual(
            findings["INTEGER_DIVISION"]["line_reference"],
            "SUM(order_total) / COUNT(*)",
        )
        self.assertEqual(to_signal(report).score, -35)


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
