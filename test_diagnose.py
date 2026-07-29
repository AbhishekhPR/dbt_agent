import builtins
import json
import socket
import subprocess
import unittest
import urllib.request
from unittest.mock import patch

from agent.diagnose import FailureDiagnosis, diagnose_failure
from agent.presenters.diagnosis import render_diagnosis
from agent.signals import Severity


CLASSIFICATION_CASES = (
    ("column_not_found", 'column "order_status" does not exist'),
    ("table_not_found", 'relation "analytics.orders" does not exist'),
    ("syntax_error", 'syntax error at or near "FROM"'),
    ("type_mismatch", "cannot cast type text to integer"),
    ("permission_error", "permission denied for relation orders"),
    ("ambiguous_column", "column reference id is ambiguous"),
    ("division_by_zero", "division by zero"),
    ("not_null_violation", "null value in column id violates not-null constraint"),
    ("unknown_error", "compilation failed for an unclassified reason"),
)


class FailureDiagnosisTests(unittest.TestCase):
    def test_every_deterministic_category_returns_the_canonical_type(self):
        for expected_category, error_log in CLASSIFICATION_CASES:
            with self.subTest(category=expected_category):
                result = diagnose_failure(error_log, "", "")

                self.assertIsInstance(result, FailureDiagnosis)
                self.assertEqual(result.category, expected_category)
                self.assertTrue(result.root_cause)
                self.assertTrue(result.explanation)
                self.assertTrue(result.recommendation)
                self.assertIsInstance(result.evidence, tuple)
                self.assertTrue(result.evidence)
                self.assertIsInstance(result.severity, Severity)
                self.assertIsInstance(result.confidence, int)
                self.assertNotIsInstance(result.confidence, bool)
                self.assertGreaterEqual(result.confidence, 0)
                self.assertLessEqual(result.confidence, 100)

    def test_unknown_locations_remain_none(self):
        result = diagnose_failure(
            'column "order_status" does not exist',
            "select order_status from orders",
            "columns:\n  - status",
        )

        self.assertIsInstance(result, FailureDiagnosis)
        self.assertIsNone(result.affected_model)
        self.assertIsNone(result.affected_file)
        self.assertIsNone(result.affected_line)
        self.assertFalse(result.data_loss_risk)

    def test_confidence_rejects_values_outside_zero_to_one_hundred(self):
        arguments = {
            "category": "unknown_error",
            "root_cause": "Unknown failure",
            "explanation": "The failure was not classified.",
            "evidence": ("failure",),
            "recommendation": "Inspect the logs.",
            "severity": Severity.MEDIUM,
        }

        for confidence in (-1, 101):
            with self.subTest(confidence=confidence):
                with self.assertRaises(ValueError):
                    FailureDiagnosis(confidence=confidence, **arguments)

        with self.assertRaises(TypeError):
            FailureDiagnosis(confidence=True, **arguments)

    def test_to_dict_is_stable_and_json_serializable(self):
        diagnosis = FailureDiagnosis(
            category="column_not_found",
            root_cause="Referenced column order_status was not found",
            explanation="The database rejected the column reference.",
            evidence=('column "order_status" does not exist',),
            recommendation="Update the model to use an available column.",
            severity=Severity.HIGH,
            confidence=90,
            metadata={"source": "unit-test"},
        )

        expected = {
            "category": "column_not_found",
            "root_cause": "Referenced column order_status was not found",
            "explanation": "The database rejected the column reference.",
            "evidence": ['column "order_status" does not exist'],
            "recommendation": "Update the model to use an available column.",
            "severity": "HIGH",
            "confidence": 90,
            "affected_model": None,
            "affected_file": None,
            "affected_line": None,
            "data_loss_risk": False,
            "metadata": {"source": "unit-test"},
        }

        self.assertEqual(diagnosis.to_dict(), expected)
        self.assertEqual(diagnosis.to_dict(), diagnosis.to_dict())
        self.assertEqual(json.loads(json.dumps(diagnosis.to_dict())), expected)
        self.assertNotIn("likely_cause", diagnosis.__dict__)
        self.assertNotIn("recommended_fix", diagnosis.__dict__)
        self.assertNotIn("suggested_fix", diagnosis.__dict__)

    def test_metadata_is_defensively_copied_at_both_boundaries(self):
        caller_metadata = {"context": {"columns": ["order_id"]}}
        diagnosis = FailureDiagnosis(
            category="unknown_error",
            root_cause="Unknown failure",
            explanation="The failure was not classified.",
            evidence=("failure",),
            recommendation="Inspect the logs.",
            severity=Severity.MEDIUM,
            confidence=40,
            metadata=caller_metadata,
        )

        caller_metadata["context"]["columns"].append("caller_mutation")
        serialized = diagnosis.to_dict()
        serialized["metadata"]["context"]["columns"].append(
            "serializer_mutation"
        )

        self.assertEqual(
            diagnosis.metadata,
            {"context": {"columns": ["order_id"]}},
        )

    def test_context_is_used_only_when_it_supports_column_evidence(self):
        error_log = 'column "order_status" does not exist'
        without_context = diagnose_failure(error_log, "", "")
        irrelevant_context = diagnose_failure(
            error_log,
            "select customer_id from customers",
            "unrelated prose",
        )
        relevant_context = diagnose_failure(
            error_log,
            "select order_status from orders",
            "columns:\n  - status",
        )

        self.assertIsInstance(without_context, FailureDiagnosis)
        self.assertIsInstance(irrelevant_context, FailureDiagnosis)
        self.assertIsInstance(relevant_context, FailureDiagnosis)
        self.assertEqual(irrelevant_context.evidence, without_context.evidence)
        self.assertTrue(
            any("Model SQL" in item for item in relevant_context.evidence),
            relevant_context.evidence,
        )
        self.assertTrue(
            any("upstream schema" in item for item in relevant_context.evidence),
            relevant_context.evidence,
        )
        self.assertNotIn("select customer_id", "\n".join(irrelevant_context.evidence))

    def test_unclassified_compile_failure_has_a_root_cause(self):
        result = diagnose_failure(
            "Compilation Error in model fct_orders: depends on a node named missing_model",
            "",
            "",
        )

        self.assertIsInstance(result, FailureDiagnosis)
        self.assertEqual(result.category, "unknown_error")
        self.assertTrue(result.root_cause)
        self.assertTrue(result.recommendation)


class DiagnosisPresenterTests(unittest.TestCase):
    def test_complete_diagnosis_renders_canonical_fields(self):
        diagnosis = FailureDiagnosis(
            category="column_not_found",
            root_cause="Column order_status is unavailable",
            explanation="The compiled query references a missing column.",
            evidence=("Database reported a missing column.",),
            recommendation="Use the current upstream column name.",
            severity=Severity.HIGH,
            confidence=90,
            affected_model="fct_orders",
            affected_file="models/marts/fct_orders.sql",
            affected_line="4",
            data_loss_risk=True,
        )

        rendered = render_diagnosis(diagnosis)

        self.assertIn("Severity: HIGH", rendered)
        self.assertIn("Root cause: Column order_status is unavailable", rendered)
        self.assertIn("Evidence:", rendered)
        self.assertIn("- Database reported a missing column.", rendered)
        self.assertIn("Recommendation: Use the current upstream column name.", rendered)
        self.assertIn("Affected model: fct_orders", rendered)
        self.assertIn("Affected file: models/marts/fct_orders.sql", rendered)
        self.assertIn("Affected line: 4", rendered)

    def test_absent_optional_fields_render_without_placeholders_or_errors(self):
        diagnosis = FailureDiagnosis(
            category="unknown_error",
            root_cause="Unclassified failure",
            explanation="The error did not match a known deterministic category.",
            evidence=(),
            recommendation="Inspect the dbt logs.",
            severity=Severity.MEDIUM,
            confidence=40,
        )

        rendered = render_diagnosis(diagnosis)

        self.assertIn("Root cause: Unclassified failure", rendered)
        self.assertIn("Evidence:\n- None provided", rendered)
        self.assertNotIn("Affected model:", rendered)
        self.assertNotIn("Affected file:", rendered)
        self.assertNotIn("Affected line:", rendered)
        self.assertNotIn("None:", rendered)

    def test_presenter_rejects_dictionary_input(self):
        with self.assertRaises(TypeError):
            render_diagnosis({"root_cause": "dictionary input"})

    def test_presenter_has_no_io_or_network_side_effects(self):
        diagnosis = FailureDiagnosis(
            category="unknown_error",
            root_cause="Unclassified failure",
            explanation="No known classification matched.",
            evidence=("failure",),
            recommendation="Inspect the logs.",
            severity=Severity.MEDIUM,
            confidence=40,
        )

        with (
            patch.object(builtins, "open") as open_mock,
            patch.object(subprocess, "run") as subprocess_mock,
            patch.object(socket, "create_connection") as socket_mock,
            patch.object(urllib.request, "urlopen") as urlopen_mock,
        ):
            first = render_diagnosis(diagnosis)
            second = render_diagnosis(diagnosis)

        self.assertEqual(first, second)
        open_mock.assert_not_called()
        subprocess_mock.assert_not_called()
        socket_mock.assert_not_called()
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
