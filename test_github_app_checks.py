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


if __name__ == "__main__":
    unittest.main()
