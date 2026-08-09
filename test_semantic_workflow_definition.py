"""Static proof about the semantic-diff workflow job.

A dispatch selects jobs by evaluating each job's `if` against the operation
input. That is normally invisible until a run happens, and a run of this
particular workflow mutates a real GitHub App webhook and a real fixture
repository. So the selection is asserted here, statically, before anything is
ever dispatched.

It also pins the properties that make the job safe to run at all: it is
covered by the shared concurrency group, its cleanup step is unconditional,
and the operations it is allowed to perform are the ones already proven.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent / ".github" / "workflows" / "governance-e2e.yml"

#: Every operation the dispatch input offers.
OPERATIONS = ("governance", "blast-radius", "semantic-diff",
              "recover-webhook", "cleanup-fixtures")


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def selected_jobs(document: dict, operation: str) -> list[str]:
    """Which jobs run for an operation, by evaluating each job's `if`.

    Every job in this workflow gates on an exact equality against the
    operation input, so the condition can be compared literally rather than
    interpreted.
    """
    chosen = []
    for name, job in (document.get("jobs") or {}).items():
        condition = str(job.get("if") or "").strip()
        if condition == f"inputs.operation == '{operation}'":
            chosen.append(name)
    return sorted(chosen)


class WorkflowSelection(unittest.TestCase):
    def setUp(self):
        self.document = load()

    def test_semantic_diff_selects_only_the_semantic_job(self):
        self.assertEqual(selected_jobs(self.document, "semantic-diff"),
                         ["semantic-diff"])

    def test_every_other_operation_is_unchanged(self):
        for operation in ("governance", "blast-radius", "recover-webhook",
                          "cleanup-fixtures"):
            with self.subTest(operation=operation):
                chosen = selected_jobs(self.document, operation)
                self.assertEqual(len(chosen), 1, f"{operation} selected {chosen}")
                self.assertNotIn("semantic-diff", chosen)

    def test_every_job_is_gated_on_exactly_one_operation(self):
        """An ungated job would run on every dispatch, including this one."""
        for name, job in self.document["jobs"].items():
            with self.subTest(job=name):
                condition = str(job.get("if") or "").strip()
                matches = [op for op in OPERATIONS
                           if condition == f"inputs.operation == '{op}'"]
                self.assertEqual(len(matches), 1,
                                 f"job {name} has condition {condition!r}")

    def test_semantic_diff_is_an_offered_operation(self):
        # YAML 1.1 reads a bare `on:` key as the boolean True.
        triggers = self.document.get("on") or self.document.get(True)
        options = triggers["workflow_dispatch"]["inputs"]["operation"]["options"]
        self.assertIn("semantic-diff", options)
        self.assertEqual(sorted(options), sorted(OPERATIONS))

    def test_the_shared_concurrency_group_still_covers_every_run(self):
        concurrency = self.document.get("concurrency") or {}
        self.assertEqual(concurrency.get("group"), "metadata-review-e2e")
        self.assertFalse(concurrency.get("cancel-in-progress"))


class SemanticJobSafety(unittest.TestCase):
    def setUp(self):
        self.job = load()["jobs"]["semantic-diff"]
        self.steps = self.job["steps"]

    def _step(self, fragment: str) -> dict:
        for step in self.steps:
            if fragment.lower() in str(step.get("name") or "").lower():
                return step
        raise AssertionError(f"no step named like {fragment!r}")

    def test_the_job_is_no_longer_the_inert_probe(self):
        names = " ".join(str(s.get("name") or "") for s in self.steps)
        self.assertNotIn("probe", names.lower())
        self.assertIn("semantic", names.lower())

    def test_cleanup_runs_even_when_the_run_fails(self):
        cleanup = self._step("cleanup")
        self.assertEqual(str(cleanup.get("if")).strip(), "always()")
        self.assertIn("--cleanup-only", cleanup["run"])

    def test_cleanup_failure_fails_the_job(self):
        self.assertIn("exit $rc", self._step("cleanup")["run"])

    def test_the_driver_is_the_semantic_one(self):
        run = self._step("Run only the semantic")["run"]
        self.assertIn("scripts/e2e/semantic_diff_e2e.py", run)
        self.assertNotIn("blast_radius_e2e.py", run)

    def test_the_harness_is_proven_before_it_may_touch_the_fixture(self):
        """Test order matters: the harness self-test precedes any mutation."""
        names = [str(s.get("name") or "") for s in self.steps]
        proof = next(i for i, n in enumerate(names) if "Prove the harness" in n)
        run = next(i for i, n in enumerate(names) if "Run only the semantic" in n)
        self.assertLess(proof, run)
        self.assertIn("test_semantic_diff_e2e_harness",
                      self._step("Prove the harness")["run"])

    def test_evidence_is_secret_scanned_before_upload(self):
        upload = self._step("Upload semantic-diff evidence")
        self.assertIn("semantic_secret_scan.outcome == 'success'", str(upload["if"]))

    def test_the_fixture_token_never_reaches_a_webhook_step(self):
        """App auth owns webhook operations; the fixture token must not."""
        for step in self.steps:
            env = step.get("env") or {}
            if "RELIUM_E2E_FIXTURE_TOKEN" in env:
                self.assertNotIn("hook", str(step.get("run") or "").lower())

    def test_the_application_role_is_asserted_least_privileged(self):
        run = self._step("Assert the application role")["run"]
        self.assertIn("NOT rolsuper", run)
        self.assertIn("LEAST_PRIVILEGED", run)

    def test_the_private_key_is_written_outside_the_workspace(self):
        run = self._step("private key")["run"]
        self.assertIn("$RUNNER_TEMP", run)
        self.assertNotIn("github.workspace", run)


class UnchangedJobs(unittest.TestCase):
    """The other operations must behave exactly as before."""

    def setUp(self):
        self.jobs = load()["jobs"]

    def test_the_proven_drivers_are_still_wired_to_their_jobs(self):
        expected = {
            "blast-radius": "scripts/e2e/blast_radius_e2e.py",
            "webhook-recovery": "scripts/e2e/webhook_recovery_e2e.py",
            "fixture-cleanup": "scripts/e2e/cleanup_stale_fixtures.py",
        }
        for job_name, driver in expected.items():
            with self.subTest(job=job_name):
                runs = " ".join(str(s.get("run") or "")
                                for s in self.jobs[job_name]["steps"])
                self.assertIn(driver, runs)

    def test_no_other_job_invokes_the_semantic_driver(self):
        for name, job in self.jobs.items():
            if name == "semantic-diff":
                continue
            with self.subTest(job=name):
                runs = " ".join(str(s.get("run") or "") for s in job["steps"])
                self.assertNotIn("semantic_diff_e2e.py", runs)


if __name__ == "__main__":
    unittest.main()
