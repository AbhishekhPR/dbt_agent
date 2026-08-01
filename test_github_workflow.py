import pathlib
import unittest

import yaml

from bash_test_support import run_bash


WORKFLOW = pathlib.Path(".github/workflows/relium-pr-review.yml")
HARDENING_WORKFLOWS = (
    pathlib.Path(".github/workflows/test.yml"),
    pathlib.Path(".github/workflows/security.yml"),
)


class GitHubWorkflowTests(unittest.TestCase):
    def test_hardening_workflows_have_top_level_triggers_permissions_and_jobs(self):
        for path in HARDENING_WORKFLOWS:
            with self.subTest(workflow=str(path)):
                workflow = yaml.load(
                    path.read_text(encoding="utf-8"),
                    Loader=yaml.BaseLoader,
                )
                self.assertIsInstance(workflow, dict)
                self.assertIn("on", workflow)
                self.assertIn("permissions", workflow)
                self.assertIn("jobs", workflow)

                triggers = workflow["on"]
                permissions = workflow["permissions"]
                jobs = workflow["jobs"]
                self.assertIsInstance(triggers, dict)
                self.assertIn("push", triggers)
                self.assertIn("pull_request", triggers)
                self.assertIsInstance(permissions, dict)
                self.assertNotIn("pull_request", permissions)
                self.assertEqual(permissions.get("contents"), "read")
                self.assertIsInstance(jobs, dict)
                self.assertTrue(jobs)

    def test_workflow_yaml_and_shell_blocks_are_valid(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        shell_blocks = [
            step["run"]
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if "run" in step
        ]
        self.assertTrue(shell_blocks)
        for block in shell_blocks:
            with self.subTest(block=block.splitlines()[0]):
                result = run_bash(["-n"], input_text=block)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_uses_truthful_skip_and_supported_review_path(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "Relium skipped: no dbt manifest was available for this repository.",
            text,
        )
        self.assertIn("python -m agent.cli review-deployment", text)
        self.assertIn("--dbt-manifest target/manifest.json", text)
        self.assertIn('enforcement_mode="shadow"', text)
        self.assertIn("load_repository_config", text)
        self.assertIn("--enforcement-mode", text)
        self.assertIn('"$enforcement_mode"', text)
        self.assertIn('cp .relium/manifest-status.md relium-review.md\n            exit 0', text)
        self.assertNotIn("github_pr_commenter", text)
        self.assertNotIn("pr_guard", text)


if __name__ == "__main__":
    unittest.main()
