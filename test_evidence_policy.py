import unittest

from agent.evidence_policy import (
    EvidenceState,
    default_policy,
    evaluate_evidence_policy,
)
from agent.github_app.config import load_repository_config


class EvidencePolicyTests(unittest.TestCase):
    def test_required_missing_evidence_warns_in_shadow_without_health_change(self):
        result = evaluate_evidence_policy(
            mode="shadow",
            policy=default_policy(),
            evidence={"head_manifest": EvidenceState.MISSING},
            health=100,
        )
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.decision, "WARN")
        self.assertEqual(result.health, 100)
        self.assertIn("head_manifest", result.reasons[0])

    def test_required_missing_evidence_blocks_in_enforce(self):
        result = evaluate_evidence_policy(
            mode="enforce",
            policy=default_policy(),
            evidence={"head_manifest": EvidenceState.MISSING},
            health=100,
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.health, 100)

    def test_optional_missing_is_not_evaluated_without_escalation(self):
        result = evaluate_evidence_policy(
            mode="enforce",
            policy=default_policy(),
            evidence={"history": EvidenceState.NOT_EVALUATED},
            health=100,
        )
        self.assertEqual(result.coverage, "COMPLETE")
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(result.health, 100)
        self.assertEqual(result.unevaluated, ["history"])

    def test_unsupported_is_preserved_as_unsupported(self):
        result = evaluate_evidence_policy(
            mode="enforce",
            policy=default_policy(),
            evidence={"detector:B05": EvidenceState.UNSUPPORTED},
            health=100,
        )
        self.assertEqual(result.unsupported, ["detector:B05"])
        self.assertEqual(result.evidence["detector:B05"], EvidenceState.UNSUPPORTED)

    def test_repository_policy_version_and_requirements_are_loaded(self):
        config = load_repository_config(
            """
version: 1
evidence_policy:
  version: premerge-v2
  sources:
    head_manifest: required
    history: optional
    slack: disabled
"""
        )
        self.assertEqual(config.evidence_policy.version, "premerge-v2")
        self.assertEqual(
            config.evidence_policy.requirements["head_manifest"].value,
            "required",
        )
        self.assertEqual(
            config.evidence_policy.requirements["slack"].value,
            "disabled",
        )
