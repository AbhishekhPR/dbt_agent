import copy
import json
import sqlite3
import unittest

from agent.assumption_verification import (
    AssumptionVerificationReport,
    build_assumption_verification_report,
    to_signal,
)
from agent.signals import Severity


class AssumptionVerificationTests(unittest.TestCase):
    def test_generates_non_negative_sql_from_revenue_invariant(self):
        report = build_assumption_verification_report(
            contracts=[
                _contract(
                    related_models=["fct_revenue"],
                    related_columns=["net_revenue"],
                    invariants=["never negative"],
                )
            ],
            project_context=_project_context(),
        )

        check = _check(report, "non_negative", "net_revenue")

        self.assertEqual(check.model_name, "fct_revenue")
        self.assertEqual(check.sql, "SELECT COUNT(*) AS violation_count FROM fct_revenue WHERE net_revenue < 0")
        self.assertFalse(check.evaluated)
        self.assertEqual(check.status, "not_evaluated")

    def test_generates_not_null_sql_for_identifier_columns(self):
        report = build_assumption_verification_report(
            contracts=[_contract(related_models=["fct_orders"])],
            project_context=_project_context(),
        )

        checks = {
            check.column_name: check.sql
            for check in report.checks
            if check.check_type == "not_null"
        }

        self.assertEqual(
            checks,
            {
                "order_id": "SELECT COUNT(*) AS violation_count FROM fct_orders WHERE order_id IS NULL",
                "customer_id": "SELECT COUNT(*) AS violation_count FROM fct_orders WHERE customer_id IS NULL",
            },
        )

    def test_generates_percentage_range_sql(self):
        report = build_assumption_verification_report(
            contracts=[
                _contract(
                    kpi_name="Conversion",
                    related_models=["fct_conversion"],
                    related_columns=["conversion_rate"],
                    invariants=["between 0 and 100%"],
                )
            ],
            project_context={
                "models": [
                    {
                        "name": "fct_conversion",
                        "columns": ["conversion_rate"],
                    }
                ]
            },
        )

        check = _check(report, "percentage_range", "conversion_rate")

        self.assertEqual(
            check.sql,
            (
                "SELECT COUNT(*) AS violation_count FROM fct_conversion "
                "WHERE conversion_rate < 0 OR conversion_rate > 100"
            ),
        )

    def test_generates_non_empty_checks_for_kpi_related_models(self):
        report = build_assumption_verification_report(
            contracts=[_contract(related_models=["fct_revenue", "fct_orders"])],
            project_context=_project_context(),
        )

        non_empty_checks = [
            check for check in report.checks
            if check.check_type == "model_not_empty"
        ]

        self.assertEqual(
            [check.model_name for check in non_empty_checks],
            ["fct_revenue", "fct_orders"],
        )
        self.assertEqual(
            non_empty_checks[0].sql,
            (
                "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END AS violation_count "
                "FROM fct_revenue"
            ),
        )

    def test_evaluates_checks_when_connection_is_available(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fct_revenue (net_revenue INTEGER, order_id TEXT)")
        conn.executemany(
            "INSERT INTO fct_revenue (net_revenue, order_id) VALUES (?, ?)",
            [(10, "o1"), (-5, None)],
        )

        try:
            report = build_assumption_verification_report(
                contracts=[
                    _contract(
                        related_models=["fct_revenue"],
                        related_columns=["net_revenue"],
                        invariants=["never negative"],
                    )
                ],
                project_context={
                    "models": [
                        {
                            "name": "fct_revenue",
                            "columns": ["net_revenue", "order_id"],
                        }
                    ]
                },
                connection=conn,
            )
        finally:
            conn.close()

        non_negative = _check(report, "non_negative", "net_revenue")
        not_null = _check(report, "not_null", "order_id")
        non_empty = _check(report, "model_not_empty", None, model_name="fct_revenue")

        self.assertTrue(non_negative.evaluated)
        self.assertEqual(non_negative.status, "failed")
        self.assertFalse(non_negative.passed)
        self.assertEqual(non_negative.violation_count, 1)
        self.assertEqual(not_null.status, "failed")
        self.assertEqual(not_null.violation_count, 1)
        self.assertEqual(non_empty.status, "passed")
        self.assertEqual(non_empty.violation_count, 0)

    def test_output_is_json_serializable_and_round_trips(self):
        report = build_assumption_verification_report(
            contracts=[_contract(related_models=["fct_revenue"])],
            project_context=_project_context(),
        )

        payload = report.to_dict()
        serialized = json.dumps(payload)
        restored = AssumptionVerificationReport.from_dict(json.loads(serialized))

        self.assertEqual(restored.to_dict(), payload)

    def test_unsupported_invariants_do_not_crash(self):
        report = build_assumption_verification_report(
            contracts=[_contract(invariants=["requires moon phase alignment"])],
            project_context=_project_context(),
        )

        check_types = [check.check_type for check in report.checks]
        self.assertIn("model_not_empty", check_types)
        self.assertNotIn("unsupported", check_types)

    def test_inputs_are_not_mutated(self):
        contracts = [_contract(related_models=["fct_revenue"], invariants=["never negative"])]
        project_context = _project_context()
        original_contracts = copy.deepcopy(contracts)
        original_project_context = copy.deepcopy(project_context)

        report = build_assumption_verification_report(
            contracts=contracts,
            project_context=project_context,
        )
        report.checks[0].metadata["mutated"] = True

        self.assertEqual(contracts, original_contracts)
        self.assertEqual(project_context, original_project_context)

    def test_not_evaluated_checks_do_not_create_decision_signal(self):
        report = build_assumption_verification_report(
            contracts=[
                _contract(
                    related_models=["fct_revenue"],
                    related_columns=["net_revenue"],
                    invariants=["never negative"],
                )
            ],
            project_context=_project_context(),
        )

        self.assertIsNone(to_signal(report))

    def test_passed_evaluated_checks_create_low_decision_signal(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fct_revenue (net_revenue INTEGER, order_id TEXT)")
        conn.execute(
            "INSERT INTO fct_revenue (net_revenue, order_id) VALUES (?, ?)",
            (10, "o1"),
        )

        try:
            report = build_assumption_verification_report(
                contracts=[
                    _contract(
                        related_models=["fct_revenue"],
                        related_columns=["net_revenue"],
                        invariants=["never negative"],
                    )
                ],
                project_context=_project_context(),
                connection=conn,
            )
        finally:
            conn.close()

        signal = to_signal(report)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.component, "assumption_verification")
        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, ["All evaluated assumption checks passed"])
        self.assertEqual(signal.metadata["evaluated_count"], 3)
        self.assertEqual(signal.metadata["failed_count"], 0)

    def test_failed_evaluated_checks_create_high_decision_signal(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fct_revenue (net_revenue INTEGER, order_id TEXT)")
        conn.executemany(
            "INSERT INTO fct_revenue (net_revenue, order_id) VALUES (?, ?)",
            [(10, "o1"), (-5, None)],
        )

        try:
            report = build_assumption_verification_report(
                contracts=[
                    _contract(
                        related_models=["fct_revenue"],
                        related_columns=["net_revenue"],
                        invariants=["never negative"],
                    )
                ],
                project_context=_project_context(),
                connection=conn,
            )
        finally:
            conn.close()

        signal = to_signal(report)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.component, "assumption_verification")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.score, -30)
        self.assertIn(
            "Revenue / GMV assumption failed: fct_revenue.net_revenue never negative (1 violation)",
            signal.reasons,
        )
        self.assertEqual(signal.metadata["failed_count"], 2)
        self.assertIn("revenue_gmv_fct_revenue_net_revenue_non_negative", signal.metadata["failed_checks"])


def _check(report, check_type, column_name, *, model_name=None):
    for check in report.checks:
        if check.check_type != check_type:
            continue
        if check.column_name != column_name:
            continue
        if model_name is not None and check.model_name != model_name:
            continue
        return check
    raise AssertionError(f"{check_type!r} check for {column_name!r} not found")


def _contract(
    *,
    kpi_name="Revenue / GMV",
    related_models=None,
    related_columns=None,
    invariants=None,
):
    return {
        "kpi_name": kpi_name,
        "description": f"{kpi_name} contract",
        "business_meaning": f"{kpi_name} matters to the business.",
        "related_models": list(related_models or ["fct_revenue"]),
        "related_columns": list(related_columns or []),
        "upstream_sources": [],
        "downstream_consumers": [],
        "assumptions": [],
        "invariants": list(invariants or []),
        "confidence": 80,
        "metadata": {},
    }


def _project_context():
    return {
        "models": [
            {
                "name": "fct_revenue",
                "columns": ["net_revenue", "order_id"],
            },
            {
                "name": "fct_orders",
                "columns": ["order_id", "customer_id", "order_total"],
            },
        ]
    }


if __name__ == "__main__":
    unittest.main()
