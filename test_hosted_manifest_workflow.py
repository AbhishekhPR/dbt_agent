"""Static security contract for the customer manifest handoff workflow."""
from pathlib import Path
import unittest

import yaml


WORKFLOW = Path(__file__).parent / ".github" / "workflows" / "relium-pr-review.yml"
PROJECT_REQUIREMENTS = Path(__file__).parent / "test_project" / "requirements.txt"
PROJECT_PROFILE = Path(__file__).parent / "test_project" / "profiles.yml"


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

    def test_uses_head_declared_build_config_for_an_older_base_revision(self):
        self.assertIn("path: configuration", self.compile_job)
        self.assertIn(
            "ref: ${{ github.event.pull_request.head.sha }}",
            self.compile_job,
        )
        self.assertIn(
            'configuration/$project_dir/requirements.txt',
            self.compile_job,
        )
        self.assertIn('--profiles-dir "$profiles_dir"', self.compile_job)
        self.assertNotIn("dbt-duckdb", self.compile_job)

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

    def test_reports_only_the_serialized_payload_byte_size_before_posting(self):
        diagnostic = 'print(f"Relium {side} manifest payload bytes: {len(body)}")'
        self.assertIn(diagnostic, self.submit_job)
        self.assertLess(self.submit_job.index(diagnostic),
                        self.submit_job.index("request = Request("))
        self.assertNotIn("print(manifest", self.submit_job)
        self.assertNotIn("print(token", self.submit_job)

    def test_does_not_review_or_publish_from_customer_ci(self):
        forbidden = (
            "review-deployment", "semantic review", "github-script",
            "createComment", "updateComment", "create_check", "check-run",
        )
        for value in forbidden:
            self.assertNotIn(value, self.text)

    def test_test_project_declares_the_verified_dbt_toolchain(self):
        self.assertEqual(
            PROJECT_REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["dbt-core==1.11.8", "dbt-duckdb==1.10.1"],
        )

    def test_test_project_profile_is_credential_free_in_memory_duckdb(self):
        profile = yaml.safe_load(PROJECT_PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(
            profile,
            {
                "test_project": {
                    "target": "dev",
                    "outputs": {
                        "dev": {
                            "type": "duckdb",
                            "path": ":memory:",
                            "threads": 1,
                        }
                    },
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
