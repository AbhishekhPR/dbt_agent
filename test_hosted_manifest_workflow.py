"""Static security contract for the customer manifest handoff workflow."""
from pathlib import Path
import unittest


WORKFLOW = Path(__file__).parent / ".github" / "workflows" / "relium-pr-review.yml"


class HostedManifestWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.compile_job = cls.text.split("  compile-manifests:", 1)[1].split(
            "  submit-manifests:", 1)[0]
        cls.submit_job = cls.text.split("  submit-manifests:", 1)[1]

    def test_uses_pull_request_without_github_write_permissions(self):
        self.assertIn("  pull_request:", self.text)
        self.assertNotIn("pull_request_target", self.text)
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotIn("pull-requests: write", self.text)
        self.assertNotIn("checks: write", self.text)

    def test_compiles_the_exact_base_and_head_revisions(self):
        self.assertIn("github.event.pull_request.base.sha", self.compile_job)
        self.assertIn("github.event.pull_request.head.sha", self.compile_job)
        self.assertIn("ref: ${{ matrix.sha }}", self.compile_job)
        self.assertIn("dbt compile", self.compile_job)
        self.assertIn("relium-manifest-${{ matrix.side }}", self.compile_job)

    def test_supports_subdirectories_and_configured_manifest_paths(self):
        self.assertIn("RELIUM_DBT_PROJECT_DIR", self.compile_job)
        self.assertIn("RELIUM_MANIFEST_PATH", self.compile_job)
        self.assertIn("relium.yml", self.compile_job)
        self.assertIn("dbt_project.yml", self.compile_job)

    def test_ci_secret_is_absent_from_untrusted_compile_job(self):
        self.assertNotIn("RELIUM_CI_TOKEN", self.compile_job)
        self.assertIn("RELIUM_CI_TOKEN: ${{ secrets.RELIUM_CI_TOKEN }}",
                      self.submit_job)
        self.assertNotIn("actions/checkout", self.submit_job)
        self.assertIn("head.repo.full_name == github.repository", self.submit_job)

    def test_submits_both_exact_sha_bound_manifests(self):
        self.assertIn("POST", self.submit_job)
        self.assertIn("/api/manifest-evidence", self.submit_job)
        self.assertIn("pull_request.base.sha", self.submit_job)
        self.assertIn("pull_request.head.sha", self.submit_job)
        self.assertIn('"commit_sha"', self.submit_job)
        self.assertIn('"manifest"', self.submit_job)

    def test_does_not_review_or_publish_from_customer_ci(self):
        forbidden = (
            "review-deployment", "semantic review", "github-script",
            "createComment", "updateComment", "create_check", "check-run",
        )
        for value in forbidden:
            self.assertNotIn(value, self.text)


if __name__ == "__main__":
    unittest.main()
