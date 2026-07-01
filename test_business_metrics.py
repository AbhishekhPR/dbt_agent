import copy
import unittest

from agent.business_metrics import (
    calculate_operational_metrics,
    evaluate_metric_reliability,
    to_signal,
)
from agent.signals import Severity


class BusinessMetricsTests(unittest.TestCase):
    def test_calculates_carts_delivered_wrong_staging_area_and_late(self):
        events = [
            {
                "event_type": "cart_delivered",
                "cart_id": "cart-1",
                "expected_staging_area": "A",
                "actual_staging_area": "B",
                "delivered_at": "2026-07-01T10:05:00",
                "due_at": "2026-07-01T10:00:00",
            },
            {
                "event_type": "cart_delivered",
                "cart_id": "cart-2",
                "expected_staging_area": "A",
                "actual_staging_area": "B",
                "delivered_at": "2026-07-01T09:55:00",
                "due_at": "2026-07-01T10:00:00",
            },
        ]

        metrics = calculate_operational_metrics(events)

        self.assertEqual(metrics["carts_delivered_wrong_staging_area_and_late"], 1)

    def test_calculates_mis_sorts(self):
        metrics = calculate_operational_metrics([
            {
                "event_type": "sort",
                "expected_sort_location": "lane-1",
                "actual_sort_location": "lane-2",
            },
            {
                "event_type": "sort",
                "expected_sort_location": "lane-3",
                "actual_sort_location": "lane-3",
            },
        ])

        self.assertEqual(metrics["mis_sorts"], 1)

    def test_calculates_totes_loaded_in_incorrect_order(self):
        metrics = calculate_operational_metrics([
            {
                "event_type": "tote_loaded",
                "tote_id": "tote-1",
                "expected_load_sequence": 1,
                "actual_load_sequence": 2,
            },
            {
                "event_type": "tote_loaded",
                "tote_id": "tote-2",
                "expected_load_sequence": 2,
                "actual_load_sequence": 2,
            },
        ])

        self.assertEqual(metrics["totes_loaded_in_incorrect_order"], 1)

    def test_calculates_failed_pickups(self):
        metrics = calculate_operational_metrics([
            {"event_type": "pickup", "pickup_id": "p1", "pickup_status": "failed"},
            {"event_type": "pickup", "pickup_id": "p2", "pickup_status": "complete"},
        ])

        self.assertEqual(metrics["failed_pickups"], 1)

    def test_calculates_overflow_avalanches(self):
        metrics = calculate_operational_metrics([
            {
                "event_type": "overflow",
                "overflow_count": 4,
                "avalanche_detected": True,
            },
            {
                "event_type": "overflow",
                "overflow_count": 2,
                "avalanche_detected": False,
            },
        ])

        self.assertEqual(metrics["overflow_avalanches"], 1)

    def test_combined_operational_metric_calculation(self):
        events = [
            {
                "event_type": "cart_delivered",
                "expected_staging_area": "A",
                "actual_staging_area": "B",
                "delivered_at": "2026-07-01T10:05:00",
                "due_at": "2026-07-01T10:00:00",
            },
            {
                "event_type": "sort",
                "expected_sort_location": "lane-1",
                "actual_sort_location": "lane-2",
            },
            {
                "event_type": "tote_loaded",
                "expected_load_sequence": 1,
                "actual_load_sequence": 2,
            },
            {"event_type": "pickup", "pickup_status": "failed"},
            {
                "event_type": "overflow",
                "overflow_count": 1,
                "avalanche_detected": True,
            },
        ]

        self.assertEqual(
            calculate_operational_metrics(events),
            {
                "carts_delivered_wrong_staging_area_and_late": 1,
                "mis_sorts": 1,
                "totes_loaded_in_incorrect_order": 1,
                "failed_pickups": 1,
                "overflow_avalanches": 1,
                "total_events": 5,
            },
        )

    def test_empty_event_volume_is_high_severity(self):
        result = evaluate_metric_reliability({
            "carts_delivered_wrong_staging_area_and_late": 0,
            "mis_sorts": 0,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": 0,
            "overflow_avalanches": 0,
            "total_events": 0,
        })

        self.assertEqual(result["severity"], "HIGH")
        self.assertIn("Zero or empty event volume detected", result["reasons"])

    def test_missing_metric_fields_are_detected(self):
        result = evaluate_metric_reliability({
            "mis_sorts": 0,
            "total_events": 10,
        })

        self.assertEqual(result["severity"], "HIGH")
        self.assertEqual(
            result["metadata"]["missing_fields"],
            [
                "carts_delivered_wrong_staging_area_and_late",
                "totes_loaded_in_incorrect_order",
                "failed_pickups",
                "overflow_avalanches",
            ],
        )
        self.assertIn("Missing metric fields detected", result["reasons"])

    def test_spike_versus_baseline_is_detected(self):
        metrics = {
            "carts_delivered_wrong_staging_area_and_late": 1,
            "mis_sorts": 6,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": 0,
            "overflow_avalanches": 0,
            "total_events": 40,
        }
        baseline = {
            "carts_delivered_wrong_staging_area_and_late": 1,
            "mis_sorts": 2,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": 0,
            "overflow_avalanches": 0,
            "total_events": 40,
        }

        result = evaluate_metric_reliability(metrics, baseline)

        self.assertEqual(result["severity"], "HIGH")
        self.assertIn("High severity metric spike detected", result["reasons"])
        self.assertEqual(result["metadata"]["spike_fields"], ["mis_sorts"])

    def test_high_severity_spike_becomes_high_signal(self):
        result = evaluate_metric_reliability(
            {
                "carts_delivered_wrong_staging_area_and_late": 0,
                "mis_sorts": 0,
                "totes_loaded_in_incorrect_order": 9,
                "failed_pickups": 0,
                "overflow_avalanches": 0,
                "total_events": 50,
            },
            {
                "carts_delivered_wrong_staging_area_and_late": 0,
                "mis_sorts": 0,
                "totes_loaded_in_incorrect_order": 3,
                "failed_pickups": 0,
                "overflow_avalanches": 0,
                "total_events": 50,
            },
        )

        signal = to_signal(result)

        self.assertEqual(signal.component, "business_metrics")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.confidence, result["confidence"])
        self.assertEqual(signal.score, result["score"])
        self.assertEqual(signal.reasons, result["reasons"])

    def test_low_healthy_result_becomes_low_signal(self):
        result = evaluate_metric_reliability({
            "carts_delivered_wrong_staging_area_and_late": 0,
            "mis_sorts": 1,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": 0,
            "overflow_avalanches": 0,
            "total_events": 100,
        })

        signal = to_signal(result)

        self.assertEqual(signal.component, "business_metrics")
        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, ["Business metrics within expected range"])

    def test_metadata_preservation(self):
        metrics = {
            "carts_delivered_wrong_staging_area_and_late": 0,
            "mis_sorts": 1,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": 0,
            "overflow_avalanches": 0,
            "total_events": 20,
        }
        baseline = dict(metrics)

        result = evaluate_metric_reliability(metrics, baseline)
        signal = to_signal(result)

        self.assertEqual(signal.metadata, result["metadata"])
        self.assertEqual(signal.metadata["metrics"], metrics)
        self.assertEqual(signal.metadata["baseline"], baseline)

    def test_output_is_deterministic_and_inputs_are_not_mutated(self):
        events = [
            {
                "event_type": "sort",
                "expected_sort_location": "lane-1",
                "actual_sort_location": "lane-2",
            }
        ]
        baseline = {
            "carts_delivered_wrong_staging_area_and_late": 0,
            "mis_sorts": 1,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": 0,
            "overflow_avalanches": 0,
            "total_events": 1,
        }
        original_events = copy.deepcopy(events)
        original_baseline = copy.deepcopy(baseline)

        first_metrics = calculate_operational_metrics(events)
        second_metrics = calculate_operational_metrics(events)
        first_result = evaluate_metric_reliability(first_metrics, baseline)
        second_result = evaluate_metric_reliability(second_metrics, baseline)

        self.assertEqual(first_metrics, second_metrics)
        self.assertEqual(first_result, second_result)
        self.assertEqual(events, original_events)
        self.assertEqual(baseline, original_baseline)


if __name__ == "__main__":
    unittest.main()
