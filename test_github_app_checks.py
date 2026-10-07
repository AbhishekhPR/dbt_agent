import unittest


class GitHubAppCheckTests(unittest.TestCase):
    def test_shadow_keeps_all_decisions_non_failing(self):
        from agent.github_app.checks import conclusion_for_decision

        self.assertEqual(conclusion_for_decision("ALLOW"), "success")
        self.assertEqual(conclusion_for_decision("WARN"), "neutral")
        self.assertEqual(conclusion_for_decision("BLOCK"), "neutral")
        self.assertEqual(conclusion_for_decision("REVIEW"), "neutral")

    def test_enforce_fails_only_block(self):
        from agent.github_app.checks import conclusion_for_decision

        self.assertEqual(
            conclusion_for_decision("ALLOW", enforcement_mode="enforce"),
            "success",
        )
        self.assertEqual(
            conclusion_for_decision("WARN", enforcement_mode="enforce"),
            "neutral",
        )
        self.assertEqual(
            conclusion_for_decision("BLOCK", enforcement_mode="enforce"),
            "failure",
        )

    def test_payload_is_completed_and_bounded(self):
        from agent.github_app.checks import build_check_run_payload

        payload = build_check_run_payload(
            head_sha="abc", result={"decision": "BLOCK", "rendered": {"markdown": "x" * 70000}}
        )
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["conclusion"], "neutral")
        self.assertEqual(payload["head_sha"], "abc")
        self.assertLessEqual(len(payload["output"]["summary"]), 65535)


class ShadowModeResultTests(unittest.TestCase):
    """A neutral shadow check is never published beside a BLOCK."""

    def _result(self, decision):
        return {"decision": decision, "incident": {"decision": decision, "health": 65,
                                                   "severity": "HIGH"}}

    def test_shadow_block_is_published_as_warn_that_would_block(self):
        from agent.github_app.checks import conclusion_for_decision, shadow_mode_result

        shaped = shadow_mode_result(self._result("BLOCK"), "shadow")
        self.assertEqual(shaped["decision"], "WARN")
        self.assertEqual(shaped["incident"]["decision"], "WARN")
        self.assertEqual(shaped["enforce_mode_decision"], "BLOCK")
        self.assertEqual(shaped["enforcement_mode"], "shadow")
        self.assertEqual((shaped["incident"]["health"], shaped["incident"]["severity"]),
                         (65, "HIGH"))
        self.assertEqual(conclusion_for_decision(shaped["decision"]), "neutral")

    def test_enforce_and_non_block_decisions_are_unchanged(self):
        from agent.github_app.checks import shadow_mode_result

        for mode, decision in (("enforce", "BLOCK"), ("shadow", "WARN"),
                               ("shadow", "ALLOW"), ("enforce", "WARN")):
            with self.subTest(mode=mode, decision=decision):
                shaped = shadow_mode_result(self._result(decision), mode)
                self.assertEqual(shaped["decision"], decision)
                self.assertEqual(shaped["enforce_mode_decision"], decision)

    def test_the_review_result_itself_is_not_mutated(self):
        from agent.github_app.checks import shadow_mode_result

        result = self._result("BLOCK")
        shadow_mode_result(result, "shadow")
        self.assertEqual(result, self._result("BLOCK"))

    def test_comment_carries_the_shadow_note(self):
        from agent.github_app.checks import shadow_mode_result
        from agent.github_app.review_comment import render_review_comment

        body = render_review_comment(shadow_mode_result(self._result("BLOCK"), "shadow"))
        self.assertIn("Decision: WARN (shadow mode — would BLOCK in enforce mode)", body)
        self.assertIn("Risk level: High", body)


if __name__ == "__main__":
    unittest.main()
