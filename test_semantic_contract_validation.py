import copy
import unittest

from agent.semantic_contract_validation import validate_semantic_contracts, to_signal
from agent.semantic_knowledge import SemanticContract
from agent.semantic_kpi_inference import ImpactedKPI, KPIImpactReport
from agent.signals import Severity


class SemanticContractValidationTests(unittest.TestCase):
    def test_changed_model_touching_contract_creates_signal(self):
        contract = _contract(
            "Revenue",
            related_models=["fct_payments"],
            invariants=["never negative"],
        )

        result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["fct_payments"],
        )
        signal = to_signal(result)

        self.assertEqual(result["severity"], "MEDIUM")
        self.assertEqual(signal.component, "semantic_contract")
        self.assertEqual(signal.severity, Severity.MEDIUM)
        self.assertIn("Revenue may be impacted by changed model fct_payments", result["reasons"])
        self.assertEqual(result["metadata"]["contract_names"], ["Revenue"])
        self.assertEqual(result["metadata"]["impacted_models"], ["fct_payments"])

    def test_impacted_kpi_creates_stronger_signal(self):
        contract = _contract("Revenue", related_models=["fct_payments"])

        touched_result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["fct_payments"],
        )
        impacted_result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["stg_payments"],
            kpi_impact_report=_impact_report(),
        )

        self.assertEqual(touched_result["severity"], "MEDIUM")
        self.assertEqual(impacted_result["severity"], "HIGH")
        self.assertGreater(impacted_result["confidence"], touched_result["confidence"])
        self.assertEqual(impacted_result["score"], -30)
        self.assertEqual(impacted_result["metadata"]["impacted_kpis"], ["Revenue"])

    def test_negative_revenue_invariant_violation_is_high(self):
        contract = _contract(
            "Revenue",
            related_models=["fct_payments"],
            related_columns=["gross_revenue"],
            invariants=["never negative"],
        )

        result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["fct_payments"],
            metadata={"metric_values": {"gross_revenue": -10}},
        )

        self.assertEqual(result["severity"], "HIGH")
        self.assertEqual(result["score"], -30)
        self.assertEqual(result["metadata"]["violated_invariants"], {"Revenue": ["never negative"]})
        self.assertIn("Revenue violates invariant: never negative", result["reasons"])

    def test_percentage_metric_outside_zero_to_one_hundred_is_high(self):
        contract = _contract(
            "Conversion",
            related_models=["fct_funnel"],
            related_columns=["conversion_rate"],
            invariants=["between 0 and 100%"],
        )

        result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["fct_funnel"],
            metadata={"metric_values": {"Conversion": 125}},
        )

        self.assertEqual(result["severity"], "HIGH")
        self.assertEqual(result["metadata"]["violated_invariants"], {"Conversion": ["between 0 and 100%"]})
        self.assertIn("Conversion violates invariant: between 0 and 100%", result["reasons"])

    def test_healthy_contracts_return_low(self):
        contract = _contract(
            "Revenue",
            related_models=["fct_payments"],
            related_columns=["gross_revenue"],
            invariants=["never negative"],
        )

        result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["stg_customers"],
            metadata={"metric_values": {"gross_revenue": 100}},
        )
        signal = to_signal(result)

        self.assertEqual(result["severity"], "LOW")
        self.assertEqual(result["confidence"], 75)
        self.assertEqual(result["score"], 0)
        self.assertEqual(signal.severity, Severity.LOW)

    def test_metadata_is_preserved(self):
        contract = _contract("Revenue", related_models=["fct_payments"])
        metadata = {"metric_values": {"Revenue": 25}, "source": "unit-test"}

        result = validate_semantic_contracts(
            contracts=[contract],
            changed_models=["fct_payments"],
            metadata=metadata,
            kpi_impact_report=_impact_report(),
        )

        self.assertEqual(result["metadata"]["input_metadata"], metadata)
        self.assertEqual(
            result["metadata"]["impact_paths"],
            [["stg_payments", "fct_payments", "Revenue"]],
        )
        self.assertEqual(result["metadata"]["impacted_kpis"], ["Revenue"])

    def test_input_objects_are_not_mutated(self):
        contracts = [
            _contract(
                "Revenue",
                related_models=["fct_payments"],
                related_columns=["gross_revenue"],
                invariants=["never negative"],
            )
        ]
        changed_models = ["fct_payments"]
        metadata = {"metric_values": {"gross_revenue": -1}}
        report = _impact_report()
        original_contracts = copy.deepcopy(contracts)
        original_changed_models = copy.deepcopy(changed_models)
        original_metadata = copy.deepcopy(metadata)
        original_report = copy.deepcopy(report)

        validate_semantic_contracts(
            contracts=contracts,
            changed_models=changed_models,
            metadata=metadata,
            kpi_impact_report=report,
        )

        self.assertEqual(contracts, original_contracts)
        self.assertEqual(changed_models, original_changed_models)
        self.assertEqual(metadata, original_metadata)
        self.assertEqual(report, original_report)


def _contract(
    kpi_name,
    *,
    related_models=None,
    related_columns=None,
    invariants=None,
):
    return SemanticContract(
        kpi_name=kpi_name,
        description=f"{kpi_name} contract",
        business_meaning=f"{kpi_name} meaning",
        related_models=list(related_models or []),
        related_columns=list(related_columns or []),
        upstream_sources=[],
        downstream_consumers=[],
        assumptions=[],
        invariants=list(invariants or []),
        confidence=85,
        metadata={"source": "unit-test"},
    )


def _impact_report():
    return KPIImpactReport(
        changed_models=["stg_payments"],
        impacted_kpis=[
            ImpactedKPI(
                name="Revenue",
                confidence=95,
                impacted_by_models=["stg_payments", "fct_payments"],
                related_columns=["gross_revenue"],
                reasons=["Revenue is impacted through stg_payments → fct_payments → Revenue"],
                metadata={
                    "impact_paths": [["stg_payments", "fct_payments", "Revenue"]],
                },
            )
        ],
        unaffected_kpis=[],
        confidence=95,
        reasons=["Revenue impacted by stg_payments, fct_payments"],
        metadata={"semantic_graph_provided": True},
    )


if __name__ == "__main__":
    unittest.main()
