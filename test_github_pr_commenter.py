import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class GithubPrCommenterTests(unittest.TestCase):
    def test_render_comment_without_risks(self):
        from agent.github_pr_commenter import render_pr_comment

        comment = render_pr_comment(
            {
                "project": "business_demo",
                "files_scanned": 0,
                "risks_found": 0,
                "highest_severity": "NONE",
                "safe_to_merge": True,
                "risks": [],
            }
        )

        self.assertIn("<!-- relium-pr-guard -->", comment)
        self.assertIn("## Relium PR Guard", comment)
        self.assertIn("Safe to merge: YES", comment)
        self.assertIn(
            "No risky SQL/dbt transformation logic was detected in the scanned files.",
            comment,
        )

    def test_missing_github_environment_does_not_post(self):
        from agent.github_pr_commenter import post_or_update_pr_comment

        with patch.dict("os.environ", {}, clear=True):
            result = post_or_update_pr_comment("body")

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "missing_environment")

    def test_detects_pull_request_number_from_event_path(self):
        from agent.github_pr_commenter import _pull_request_number

        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "event.json"
            event_path.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")

            self.assertEqual(_pull_request_number(str(event_path)), 42)


if __name__ == "__main__":
    unittest.main()
