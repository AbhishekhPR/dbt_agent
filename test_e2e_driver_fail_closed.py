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

    def _scope_body(self):
        live = _source(LIVE)
        return live[live.index("def assert_fixture_token_scope"):
                    live.index("def create_fixture_pr")]

    def test_public_repositories_do_not_fail_the_scope_assertion(self):
        """A fine-grained PAT keeps read access to public repositories, so
        public visibility must never be treated as grant scope."""
        body = self._scope_body()
        self.assertIn('if r.get("private")', body,
                      "the scope set must be filtered to private repositories")
        self.assertIn("public_repositories_ignored", body)

    def test_unrelated_private_repositories_fail_the_assertion(self):
        body = self._scope_body()
        self.assertIn("unrelated PRIVATE", body)
        self.assertIn("raise StageFailure", body)

    def test_target_private_repository_is_required(self):
        body = self._scope_body()
        self.assertIn("cannot see the target private repository", body)
        self.assertIn("private_set != [repo]", body)

    def test_public_repo_permissions_are_never_used_as_proof(self):
        """E2E-SEC-01 came from inferring grant scope from a public repo's
        permissions object. That inference must not return."""
        body = self._scope_body()
        for forbidden in ('control_perms', 'permissions") or {}',
                          '.get("push")', '.get("admin")', "dbt_agent"):
            with self.subTest(pattern=forbidden):
                self.assertNotIn(forbidden, body)

    def test_write_capability_is_proven_by_the_real_fixture_creation(self):
        body = self._scope_body()
        self.assertIn("write_capability_proof", body)
        driver = _source(DRIVER)
        self.assertIn("lf.create_fixture_pr(state, gh, FIXTURE_TOKEN", driver)

    def test_driver_only_reads_keys_the_scope_proof_actually_returns(self):
        """Run 6 died on KeyError: 'unrelated_access_denied' - the driver still
        subscripted a key the rewritten proof no longer returns. Nothing
        compared producer to consumer, so nothing caught it."""
        produced = set(re.findall(r'"([a-z_]+)":', self._scope_body()))
        consumed = set(re.findall(r'scope\["([a-z_]+)"\]', _source(DRIVER)))
        self.assertTrue(consumed, "driver must read the scope proof")
        missing = consumed - produced
        self.assertEqual(set(), missing,
                         f"driver reads keys the proof never returns: {missing}")

    def test_fixture_operations_are_restricted_to_the_allowed_set(self):
        body = self._scope_body()
        allowed = ["branch creation", "file commits", "pull request creation",
                   "pull request closure", "fixture branch deletion"]
        for op in allowed:
            with self.subTest(operation=op):
                self.assertIn(op, body)

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


class FixtureShapeTests(unittest.TestCase):
    """Run 7 reached the application and produced no review row.

    The application was right. The review path derives changed models by
    matching changed file paths against each manifest node's
    original_file_path, and the fixture pull request committed only
    relium.yml and target/manifest.json - no dbt model file. The reviewer
    correctly raised "At least one changed model is required." and the runner
    published a neutral skip before the lifecycle could ever run.

    These tests hold the fixture to being a genuine dbt model change.
    """

    def _fixture(self):
        live = _source(LIVE)
        return live[live.index("def _model_files"):]

    def test_fixture_commits_real_dbt_model_files(self):
        body = self._fixture()
        self.assertIn("original_file_path", body,
                      "model files must come from the manifest's own paths")
        self.assertIn("base_files = _model_files(base)", body)
        self.assertIn("head_files = _model_files(head)", body)

    def test_head_must_change_at_least_one_model_file(self):
        body = self._fixture()
        self.assertIn("changed_paths", body)
        self.assertIn("changes no dbt model file", body,
                      "a fixture that changes no model must fail closed, "
                      "not silently produce an unreviewable pull request")

    def test_pull_request_opens_against_a_base_branch(self):
        """The runner reads the base manifest at pull_request.base.sha.
        Opening against the default branch binds the review to a tree with no
        manifest at all."""
        body = self._fixture()
        self.assertIn('"base": base_branch', body)
        self.assertIn('"head": head_branch', body)
        self.assertNotIn('"base": default_branch', body)

    def test_shas_are_read_back_from_github_not_inferred(self):
        body = self._fixture()
        self.assertIn('pr["base"]["sha"]', body)
        self.assertIn('pr["head"]["sha"]', body)

    def test_both_fixture_branches_are_tracked_for_removal(self):
        body = self._fixture()
        self.assertIn('state.setdefault("branches", []).append(name)', body)
        self.assertIn("make_branch(base_branch", body)
        self.assertIn("make_branch(head_branch", body)


class CleanupCompletenessTests(unittest.TestCase):
    def test_cleanup_deletes_fixture_branches(self):
        """state["branches"] was populated at creation and consumed by
        nothing, so every run so far left its branches behind."""
        driver = _source(DRIVER)
        self.assertIn("git/refs/heads/", driver)
        self.assertIn("fixture_branches_deleted", driver)
        self.assertIn("fixture_branches_remaining", driver)

    def test_cleanup_sweeps_by_prefix_for_the_outer_step(self):
        """The workflow's always-step runs cleanup in a fresh process with
        empty state, so removal cannot depend on in-memory state alone."""
        driver = _source(DRIVER)
        self.assertIn("git/matching-refs/heads/e2e/", driver)
        self.assertIn("fixture_prs_swept", driver)

    def test_branch_removal_requires_the_fixture_token(self):
        driver = _source(DRIVER)
        self.assertIn("no fixture token available to remove fixture branches",
                      driver)

    def test_outer_cleanup_does_not_overwrite_the_driver_record(self):
        driver = _source(DRIVER)
        self.assertIn("cleanup-verification-outer.json", driver)
        self.assertIn("if CLEANUP_ONLY", driver)


class ObservabilityTests(unittest.TestCase):
    def test_webhook_stage_asserts_the_application_disposition(self):
        """The application answers 202 for accepted, ignored and duplicate
        alike, so a 202 on its own proves only that the signature verified."""
        verify = _source(VERIFY)
        self.assertIn("/app/hook/deliveries/", verify)
        self.assertIn('disposition != "accepted"', verify)
        self.assertIn("application_disposition", verify)

    def test_application_logs_are_captured(self):
        """Run 7's api.log was empty, so a silent skip inside the review path
        was invisible."""
        live = _source(LIVE)
        self.assertIn("logging.basicConfig", live)
        self.assertNotIn("log_level='warning'", live)


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
