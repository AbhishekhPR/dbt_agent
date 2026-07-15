import unittest


class GitHubAppCheckTests(unittest.TestCase):
    def test_decisions_map_to_stable_github_conclusions(self):
        from agent.github_app.checks import conclusion_for_decision

        self.assertEqual(conclusion_for_decision("ALLOW"), "success")
        self.assertEqual(conclusion_for_decision("BLOCK"), "failure")
        self.assertEqual(conclusion_for_decision("BLOCK", mode="warn"), "neutral")
        self.assertEqual(conclusion_for_decision("REVIEW"), "neutral")

    def test_payload_is_completed_and_bounded(self):
        from agent.github_app.checks import build_check_run_payload

        payload = build_check_run_payload(
            head_sha="abc", result={"decision": "BLOCK", "rendered": {"markdown": "x" * 70000}}
        )
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["conclusion"], "failure")
        self.assertEqual(payload["head_sha"], "abc")
        self.assertLessEqual(len(payload["output"]["summary"]), 65535)


if __name__ == "__main__":
    unittest.main()
