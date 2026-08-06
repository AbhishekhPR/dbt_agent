"""Tests that the genuine-GitHub E2E driver cannot report false success.

These exist because of a specific defect: an earlier driver reached a
placeholder ``return 0`` after the environment gate, so the workflow would
have shown green for an E2E that never ran. Twelve live stages were missing
and four cleanup state fields were read but never written.

Every test here is static or in-process. None require secrets, GitHub, a
tunnel or a database, so they run in ordinary CI before the workflow is ever
enabled.
"""
from __future__ import annotations

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

E2E = Path(__file__).with_name("scripts") / "e2e"
DRIVER = E2E / "metadata_review_e2e.py"
LIVE = E2E / "live_flow.py"
VERIFY = E2E / "verify_flow.py"
STAGES = E2E / "stages.py"
WORKFLOW = Path(__file__).with_name(".github") / "workflows" / "metadata-review-e2e.yml"

# Every mandatory live operation, and the function that must implement it.
MANDATORY_OPERATIONS = {
    "tunnel startup": "start_tunnel",
    "api startup": "start_api",
    "worker startup": "start_worker",
    "webhook repoint": "point_webhook",
    "webhook verification": "verify_webhook",
    "fixture PR creation": "create_fixture_pr",
    "genuine webhook arrival": "verify_genuine_webhook",
    "postgres review": "verify_postgres_review",
    "targeted request": "verify_targeted_request",
    "waiting publication": "verify_waiting_publication",
    "snapshot submission": "submit_primary_snapshot",
    "duplicate snapshot": "verify_duplicate",
    "conflicting replay": "verify_conflicting_replay",
    "worker recomputation": "verify_recomputation",
    "github reconciliation": "verify_reconciliation",
    "dashboard": "verify_dashboard",
    "variants": "run_variant",
    "webhook restoration": "restore_webhook",
    "cleanup": "cleanup",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _called_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class StageRegistryTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(E2E))

    def tearDown(self):
        if str(E2E) in sys.path:
            sys.path.remove(str(E2E))

    def test_driver_fails_when_any_stage_is_incomplete(self):
        from stages import REQUIRED_STAGES, StageIncomplete, StageTracker

        with tempfile.TemporaryDirectory() as tmp:
            tracker = StageTracker(Path(tmp) / "t.json")
            for name in REQUIRED_STAGES[:-1]:
                tracker.complete(name, {"observed": "real"})
            with self.assertRaises(StageIncomplete):
                tracker.assert_all_complete()
            self.assertEqual(tracker.incomplete(), [REQUIRED_STAGES[-1]])

    def test_an_empty_live_flow_cannot_return_success(self):
        """A tracker with nothing marked must never pass."""
        from stages import StageIncomplete, StageTracker

        with tempfile.TemporaryDirectory() as tmp:
            tracker = StageTracker(Path(tmp) / "t.json")
            with self.assertRaises(StageIncomplete):
                tracker.assert_all_complete()

    def test_evidence_generation_alone_cannot_mark_a_stage(self):
        from stages import StageIncomplete, StageTracker

        with tempfile.TemporaryDirectory() as tmp:
            tracker = StageTracker(Path(tmp) / "t.json")
            for bogus in ({}, {"evidence_file": "x.json"}, {"planned": True},
                          {"note": "will do later"}):
                with self.assertRaises(StageIncomplete):
                    tracker.complete("api_started", bogus)
            self.assertIn("api_started", tracker.incomplete())

    def test_a_recorded_failure_blocks_success(self):
        from stages import REQUIRED_STAGES, StageIncomplete, StageTracker

        with tempfile.TemporaryDirectory() as tmp:
            tracker = StageTracker(Path(tmp) / "t.json")
            for name in REQUIRED_STAGES:
                tracker.complete(name, {"observed": "real"})
            tracker.fail("dashboard_verified", "leaked a connection string")
            with self.assertRaises(StageIncomplete):
                tracker.assert_all_complete()


class ReachabilityTests(unittest.TestCase):
    """Every mandatory operation must have a call path from main()."""

    def test_every_mandatory_operation_is_reachable_from_main(self):
        driver = ast.parse(_source(DRIVER))
        live = ast.parse(_source(LIVE))
        verify = ast.parse(_source(VERIFY))
        reachable = _called_names(driver) | _called_names(live) | _called_names(verify)

        missing = [f"{label} -> {fn}()" for label, fn in MANDATORY_OPERATIONS.items()
                   if fn not in reachable]
        self.assertEqual(missing, [], f"unreachable mandatory operations: {missing}")

    def test_mandatory_functions_are_defined_not_just_referenced(self):
        defined = set()
        for path in (DRIVER, LIVE, VERIFY):
            tree = ast.parse(_source(path))
            defined |= {n.name for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)}
        missing = [fn for fn in MANDATORY_OPERATIONS.values() if fn not in defined]
        self.assertEqual(missing, [], f"declared but never defined: {missing}")

    def test_main_reaches_the_stage_assertion(self):
        source = _source(DRIVER)
        self.assertIn("tracker.assert_all_complete()", source,
                      "main() must assert every stage completed before success")

    def test_no_placeholder_success_path_remains(self):
        """The exact defect that made the previous driver fail open."""
        source = _source(DRIVER)
        self.assertNotIn("live flow stages follow", source)
        self.assertNotIn("The remaining stages", source)
        # a bare `return 0` may only appear after the stage assertion
        assertion = source.index("tracker.assert_all_complete()")
        for match in re.finditer(r"^    return 0$", source, re.M):
            self.assertGreater(match.start(), assertion,
                               "return 0 appears before the stage assertion")


class StateFieldTests(unittest.TestCase):
    """The four fields that were read but never written."""

    def test_state_fields_are_written_by_real_execution_paths(self):
        combined = _source(DRIVER) + _source(LIVE)
        writes = {
            "mutated": r'state\["mutated"\]\s*=\s*True',
            "procs": r'state\["procs"\]\.append',
            "tunnel": r'state\["tunnel"\]\s*=',
            "pr_number": r'state\["pr_number"\]\s*=',
        }
        for field, pattern in writes.items():
            with self.subTest(field=field):
                self.assertRegex(combined, pattern,
                                 f'_state["{field}"] is never written')

    def test_mutation_flag_is_set_before_the_webhook_call(self):
        source = _source(LIVE)
        body = source[source.index("def point_webhook"):]
        flag = body.index('state["mutated"] = True')
        call = body.index('gh("PATCH", "/app/hook/config"')
        self.assertLess(flag, call,
                        "the mutation flag must be set before the GitHub call")

    def test_cleanup_is_registered_before_any_mutation(self):
        source = _source(DRIVER)
        armed = source.index("arm()   # armed BEFORE")
        # point_webhook is the only mutating call, invoked from main()
        mutation = source.index("lf.point_webhook(")
        self.assertLess(armed, mutation,
                        "cleanup must be armed before the webhook is mutated")

    def test_restoration_happens_before_process_shutdown(self):
        source = _source(DRIVER)
        body = source[source.index("def cleanup("):]
        restore = body.index("restore_webhook()")
        stop_tunnel = body.index('state["tunnel"]["proc"].terminate()')
        stop_procs = body.index("for label, proc in state[\"procs\"]")
        self.assertLess(restore, stop_tunnel, "webhook must be restored before the tunnel")
        self.assertLess(restore, stop_procs, "webhook must be restored before processes")

    def test_cleanup_failure_produces_a_non_zero_result(self):
        source = _source(DRIVER)
        self.assertIn('if not result.get("cleanup_passed")', source)
        self.assertIn("cleanup failed", source)
        tail = source[source.index('if __name__ == "__main__":'):]
        self.assertIn('cleanup_passed', tail,
                      "the entrypoint must fail when cleanup fails")

    def test_cleanup_runs_on_exception_and_signal(self):
        source = _source(DRIVER)
        self.assertIn("atexit.register(cleanup", source)
        self.assertIn("signal.SIGTERM", source)
        self.assertIn("signal.SIGINT", source)
        self.assertIn("finally:", source)

    def test_cleanup_is_idempotent(self):
        source = _source(DRIVER)
        body = source[source.index("def cleanup("):]
        self.assertIn('state.get("cleanup_done")', body)


class ImportPathTests(unittest.TestCase):
    """Run 2 failed with ModuleNotFoundError: No module named 'agent'.

    sys.path[0] is the script's own directory, so the repository root must be
    added explicitly or every `agent...` import fails at the first stage.
    """

    def test_driver_puts_the_repository_root_on_sys_path(self):
        source = _source(DRIVER)
        self.assertIn("REPO_ROOT = Path(__file__).resolve().parents[2]", source)
        self.assertIn("sys.path.insert(0, str(REPO_ROOT))", source)

    def test_helper_modules_also_reach_the_repository_root(self):
        for path in (LIVE, VERIFY):
            with self.subTest(module=path.name):
                source = _source(path)
                self.assertIn("parents[2]", source,
                              f"{path.name} cannot import agent modules")

    def test_repo_root_is_inserted_before_agent_imports_are_used(self):
        source = _source(DRIVER)
        root = source.index("sys.path.insert(0, str(REPO_ROOT))")
        first_agent = source.index("from agent.")
        self.assertLess(root, first_agent,
                        "the repository root must be on sys.path before agent imports")


class ProhibitedContentTests(unittest.TestCase):
    def test_no_todo_pass_or_mocked_success_in_the_live_path(self):
        for path in (DRIVER, LIVE, VERIFY, STAGES):
            source = _source(path)
            with self.subTest(file=path.name):
                self.assertNotIn("TODO", source)
                self.assertNotIn("FIXME", source)
                self.assertNotRegex(source, r"^\s+pass\s*$",
                                    "bare pass statement in the live path")
                for mock in ("MagicMock", "unittest.mock", "monkeypatch",
                             "return True  # stub", "fake_success"):
                    self.assertNotIn(mock, source)

    def test_variant_letters_are_all_implemented(self):
        source = _source(DRIVER)
        for letter in "abcde":
            with self.subTest(variant=letter):
                self.assertIn(f'"variant_{letter}_verified"', source)
        self.assertIn('"variant_f_verified"', source)
        self.assertIn('"variant_g_verified"', source)


class FixtureTokenBoundaryTests(unittest.TestCase):
    """The fixture token is the only credential with contents:write.

    The dedicated App deliberately keeps contents:read, so this token's blast
    radius must stay confined to fixture branch/commit/PR operations.
    """

    def test_fixture_token_scope_is_asserted_before_use(self):
        """Execution order inside main(), not source position.

        run_variant is DEFINED above main() but only CALLED from it, so a
        whole-file index comparison measures the wrong thing.
        """
        driver = _source(DRIVER)
        body = driver[driver.index("def main() -> int:"):]
        assertion = body.index("assert_fixture_token_scope")
        first_use = body.index("lf.create_fixture_pr(")
        self.assertLess(assertion, first_use,
                        "token scope must be asserted before any fixture write")

    def test_scope_assertion_precedes_the_variant_helper_call(self):
        driver = _source(DRIVER)
        body = driver[driver.index("def main() -> int:"):]
        assertion = body.index("assert_fixture_token_scope")
        variant_call = body.index("run_variant(")
        self.assertLess(assertion, variant_call)

    def test_scope_assertion_rejects_unrelated_private_repositories(self):
        live = _source(LIVE)
        body = live[live.index("def assert_fixture_token_scope"):]
        self.assertIn("unrelated private repository", body)
        self.assertIn("raise StageFailure", body)

    def test_fixture_token_is_never_used_for_app_operations(self):
        driver = _source(DRIVER)
        forbidden = ("/app/hook/config", "/app/installations",
                     "/issues/", "/check-runs", "/api/metadata-snapshots")
        for line in driver.splitlines():
            if "FIXTURE_TOKEN" not in line:
                continue
            for path in forbidden:
                with self.subTest(line=line.strip()[:60], path=path):
                    self.assertNotIn(path, line,
                                     "fixture token used on an App-only path")

    def test_webhook_operations_use_the_app_jwt_only(self):
        driver = _source(DRIVER)
        for line in driver.splitlines():
            if "/app/hook/config" in line:
                self.assertNotIn("FIXTURE_TOKEN", line)


class WorkflowTests(unittest.TestCase):
    def test_workflow_is_dispatch_only_with_bounded_timeout(self):
        import yaml
        doc = yaml.safe_load(_source(WORKFLOW))
        triggers = doc.get(True) or doc.get("on")
        self.assertEqual(list(triggers), ["workflow_dispatch"])
        job = doc["jobs"]["e2e"]
        self.assertIsInstance(job["timeout-minutes"], int)
        self.assertLessEqual(job["timeout-minutes"], 60)

    def test_cleanup_and_scrub_run_always(self):
        import yaml
        doc = yaml.safe_load(_source(WORKFLOW))
        always = [s.get("name") for s in doc["jobs"]["e2e"]["steps"]
                  if s.get("if") == "always()"]
        self.assertTrue(any("cleanup" in (n or "").lower() for n in always))
        self.assertTrue(any("scrub" in (n or "").lower() for n in always))

    def test_workflow_never_triggers_on_push_or_pull_request(self):
        source = _source(WORKFLOW)
        triggers = source[:source.index("permissions:")]
        self.assertNotIn("\n  push:", triggers)
        self.assertNotIn("\n  pull_request:", triggers)


if __name__ == "__main__":
    unittest.main()
