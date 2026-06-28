import unittest

from agent.signals import Severity, Signal


class SignalTests(unittest.TestCase):
    def test_signal_stores_all_fields(self):
        signal = Signal(
            component="ast",
            severity="HIGH",
            confidence=95,
            score=-40,
            reasons=["LEFT JOIN nullification detected"],
            metadata={"rule": "LEFT_JOIN_WHERE"},
        )

        self.assertEqual(signal.component, "ast")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.confidence, 95)
        self.assertEqual(signal.score, -40)
        self.assertEqual(signal.reasons, ["LEFT JOIN nullification detected"])
        self.assertEqual(signal.metadata, {"rule": "LEFT_JOIN_WHERE"})

    def test_signal_normalizes_string_severity_to_enum(self):
        signal = Signal(
            component="ast",
            severity="HIGH",
            confidence=95,
            score=-40,
        )

        self.assertEqual(signal.severity, Severity.HIGH)

    def test_signal_rejects_unknown_severity(self):
        with self.assertRaises(ValueError):
            Signal(
                component="ast",
                severity="HGIH",
                confidence=95,
                score=-40,
            )

    def test_reasons_defaults_to_empty_list(self):
        signal = Signal(
            component="metadata",
            severity="LOW",
            confidence=80,
            score=0,
        )

        self.assertEqual(signal.reasons, [])

    def test_metadata_defaults_to_empty_dict(self):
        signal = Signal(
            component="metadata_drift",
            severity="MEDIUM",
            confidence=90,
            score=-10,
        )

        self.assertEqual(signal.metadata, {})

    def test_signal_instances_do_not_share_mutable_defaults(self):
        first = Signal(
            component="blast_radius",
            severity="HIGH",
            confidence=90,
            score=-25,
        )
        second = Signal(
            component="metadata",
            severity="LOW",
            confidence=70,
            score=5,
        )

        first.reasons.append("Downstream model affected")
        first.metadata["affected_models"] = ["orders"]

        self.assertEqual(second.reasons, [])
        self.assertEqual(second.metadata, {})


if __name__ == "__main__":
    unittest.main()
