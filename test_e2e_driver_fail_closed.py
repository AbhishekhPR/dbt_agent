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
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

E2E = Path(__file__).with_name("scripts") / "e2e"
DRIVER = E2E / "metadata_review_e2e.py"
LIVE = E2E / "live_flow.py"
VERIFY = E2E / "verify_flow.py"
STAGES = E2E / "stages.py"
WEBHOOK_RECOVERY = E2E / "webhook_recovery_e2e.py"
WORKFLOW = Path(__file__).with_name(".github") / "workflows" / "metadata-review-e2e.yml"
GOVERNANCE_WORKFLOW = (Path(__file__).with_name(".github") / "workflows" /
                       "governance-e2e.yml")

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
    def test_fresh_outer_cleanup_never_claims_webhook_restoration(self):
        """A new cleanup process has no in-memory mutation to restore.

        Run 31246080645's outer cleanup used to report ``restored: true`` in
        exactly this state even though it neither knew nor queried the
        original configuration. Exercise the real cleanup-only entrypoint in
        a new process and require the result to remain explicitly unknown.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            for name in tuple(env):
                if name.startswith("RELIUM_"):
                    env.pop(name)
            proc = subprocess.run(
                [sys.executable, str(DRIVER), tmp, "--cleanup-only"],
                capture_output=True, text=True, env=env, timeout=30,
                check=False)
            self.assertNotEqual(0, proc.returncode,
                                "missing fixture cleanup authority must fail closed")
            record = json.loads(
                (Path(tmp) / "cleanup-verification-outer.json").read_text(
                    encoding="utf-8"))
            webhook = record["webhook"]
            self.assertIsNone(webhook["restored"])
            self.assertFalse(webhook["verified_through_github"])

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

    def test_outer_cleanup_does_not_overwrite_the_stage_tracker(self):
        """StageTracker starts every stage incomplete and write() overwrites,
        so a shared path lets the outer always-step destroy the driver's stage
        record. Run 8 uploaded a tracker reporting 2 of 27 complete that
        described the cleanup process, not the run."""
        driver = _source(DRIVER)
        self.assertIn("stage-tracker-outer.json", driver)
        self.assertNotIn('StageTracker(EV / "stage-tracker.json")', driver)


class WebhookRecoveryHarnessTests(unittest.TestCase):
    def test_recovery_is_pinned_to_the_two_authoritative_runs(self):
        source = _source(WEBHOOK_RECOVERY)
        self.assertIn("31085032785", source)
        self.assertIn("https://example.invalid/github/webhook", source)
        self.assertIn("31246080645", source)
        self.assertIn(
            "https://connector-wind-terms-yet.trycloudflare.com/github/webhook",
            source)

    def test_temporary_mutation_is_preserved_and_restored_in_order(self):
        source = _source(WEBHOOK_RECOVERY)
        body = source[source.index("def main() -> int:"):]
        preserve = body.index("preserve_webhook()")
        temporary = body.index("lf.point_webhook(")
        restore = body.index("restore_webhook()")
        final_verify = body.index("final = read_webhook_state(")
        self.assertLess(preserve, temporary)
        self.assertLess(temporary, restore)
        self.assertLess(restore, final_verify)

    def test_recovery_verifies_identity_and_untouched_configuration(self):
        source = _source(WEBHOOK_RECOVERY)
        for required in ("relium-e2e", "AbhishekhPR/relium-e2e-dbt",
                         "active", "events", "content_type", "insecure_ssl",
                         "verified_through_github", "relium_pilot_touched"):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_recovery_creates_no_fixture_pull_request_or_branch(self):
        source = _source(WEBHOOK_RECOVERY)
        self.assertNotIn("create_fixture_pr", source)
        self.assertNotIn('gh("POST", f"/repos/', source)
        self.assertNotIn('gh("PATCH", f"/repos/', source)
        self.assertNotIn('gh("DELETE", f"/repos/', source)

    def test_recovery_fails_if_tunnel_or_listener_survives_cleanup(self):
        source = _source(WEBHOOK_RECOVERY)
        self.assertIn('_require(cleanup["tunnel_stopped"]', source)
        self.assertIn('_require(not cleanup["local_listener_still_up"]', source)

    def test_secure_workflow_has_a_small_recovery_only_job(self):
        import yaml
        doc = yaml.safe_load(_source(GOVERNANCE_WORKFLOW))
        triggers = doc.get(True) or doc.get("on")
        operation = triggers["workflow_dispatch"]["inputs"]["operation"]
        self.assertEqual("governance", operation["default"])
        recovery = doc["jobs"]["webhook-recovery"]
        self.assertLessEqual(recovery["timeout-minutes"], 15)
        self.assertNotIn("services", recovery)
        commands = "\n".join(
            str(step.get("run", "")) for step in recovery["steps"])
        self.assertIn("webhook_recovery_e2e.py", commands)
        self.assertNotIn("governance_e2e.py", commands)
        self.assertIn("E2E LISTENER STILL UP", commands)


class ObservabilityTests(unittest.TestCase):
    def test_webhook_stage_asserts_the_application_disposition(self):
        """The application answers 202 for accepted, ignored and duplicate
        alike, so a 202 on its own proves only that the signature verified."""
        verify = _source(VERIFY)
        self.assertIn("/app/hook/deliveries/", verify)
        self.assertIn('disposition != "accepted"', verify)
        self.assertIn("application_disposition", verify)

    def test_delivery_detail_is_fetched_by_numeric_id(self):
        """The detail endpoint is keyed by the numeric delivery id. Passing
        the guid returns HTTP 422, which is how run 8 failed."""
        verify = _source(VERIFY)
        self.assertIn("deliveries/{numeric_id}", verify)
        self.assertNotIn("deliveries/{guid}", verify)

    def test_application_logs_are_captured(self):
        """Run 7's api.log was empty, so a silent skip inside the review path
        was invisible."""
        live = _source(LIVE)
        self.assertIn("logging.basicConfig", live)
        self.assertNotIn("log_level='warning'", live)


class VariantFixtureReachabilityTests(unittest.TestCase):
    """Run 9 failed variant A with "expected ALLOW, got WARN".

    That was a fixture defect. Adding a column named net_revenue to fct_orders
    makes the code review report "Revenue / GMV gained related columns
    net_revenue" and score code health 80, and the evidence policy puts
    anything under 90 in the WARN band before production metadata is
    considered. ALLOW was unreachable for that fixture whatever the metadata
    said, so the variant could not isolate the dimension it exists to test.

    These tests run the REAL application engine, so they fail if a fixture
    stops being able to reach the outcome its variant asserts.
    """

    @staticmethod
    def _review(variant):
        sys.path.insert(0, str(E2E))
        import live_flow
        from agent.deployment_review_service import review_manifest_change
        from agent.metadata_evidence.collection_plan import build_collection_plan

        base, head, changed = live_flow.build_manifests(variant)
        files = sorted(live_flow._model_files(head))
        result = review_manifest_change(
            manifest=head, previous_manifest=base, changed_files=files,
            deployment_id="fixture-test",
            manifest_source={"base": "github", "head": "github"},
            base_sha="b" * 40, head_sha="h" * 40)
        plan = build_collection_plan(
            base_manifest=base, head_manifest=head, changed_models=changed,
            evidence_level="profile", critical_models=()).as_dict()
        return (result.get("incident") or {}).get("health"), plan

    def _assert_allow_reachable(self, variant):
        health, plan = self._review(variant)
        self.assertGreaterEqual(
            health, 90,
            f"{variant} scores code health {health}; the evidence policy puts "
            f"anything under 90 in the WARN band, so ALLOW is unreachable and "
            f"the variant cannot isolate the metadata dimension")
        self.assertTrue(
            plan["metadata_required"],
            f"{variant} must still REQUIRE production metadata, otherwise the "
            f"variant no longer exercises the metadata path at all")

    def test_variant_a_fixture_can_reach_allow(self):
        self._assert_allow_reachable("external_clean")

    def test_variant_e_fixture_can_reach_allow(self):
        self._assert_allow_reachable("head_derived_clean")

    def test_variant_a_fixture_keeps_the_external_dependency(self):
        _, plan = self._review("external_clean")
        kinds = {t["dependency_kind"] for t in plan["targets"]}
        self.assertIn("external", kinds,
                      "removing the code-health confound must not remove the "
                      "external production dependency being tested")

    def test_variant_e_fixture_keeps_the_head_derived_dependency(self):
        _, plan = self._review("head_derived_clean")
        kinds = {t["dependency_kind"] for t in plan["targets"]}
        self.assertIn("head_derived", kinds,
                      "variant E exists to test head-derived dependencies")

    def test_allow_variants_use_the_clean_fixtures(self):
        driver = _source(DRIVER)
        self.assertIn('"variant_a_verified", "enforce", "external_clean"', driver)
        self.assertIn('"variant_e_verified", "enforce", "head_derived_clean"',
                      driver)

    def test_primary_and_other_variants_keep_their_proven_fixtures(self):
        """The primary scenario and variants B, C and D passed on 'external'.
        The fix must not disturb them."""
        driver = _source(DRIVER)
        for stage in ("variant_b_verified", "variant_c_verified",
                      "variant_d_verified"):
            with self.subTest(stage=stage):
                self.assertIn(f'"{stage}", "enforce", "external"', driver)

    def test_evidence_explanation_does_not_hardcode_a_health_number(self):
        """Run 9's recomputation evidence claimed "health remains 100" while
        the same document recorded 80. An evidence file must not contradict
        itself."""
        verify = _source(VERIFY)
        self.assertNotIn("health remains 100", verify)
        self.assertIn("Code health is {review['health']}", verify)


class TunnelReachabilityTests(unittest.TestCase):
    """Run 10 lost the webhook to an unserved tunnel hostname.

    cloudflared reported a healthy connection and printed a URL, the webhook
    was repointed at it, the pull request was opened, and the application
    received zero inbound requests. GitHub does not retry a failed webhook
    delivery, so a hostname the Cloudflare edge is not yet serving loses the
    event permanently and the run cannot recover.
    """

    def _tunnel_body(self):
        live = _source(LIVE)
        return live[live.index("def start_tunnel"):live.index("def point_webhook")]

    def test_edge_reachability_is_proven_not_assumed(self):
        body = self._tunnel_body()
        self.assertIn("/healthz", body,
                      "a URL scraped from a log is not a routable endpoint")
        self.assertIn("edge_reachable_from_public_internet", body)

    def test_edge_is_verified_before_the_webhook_is_repointed(self):
        """Repointing first and discovering the edge is dead afterwards loses
        the delivery, because GitHub will not send it again."""
        live = _source(LIVE)
        tunnel_probe = live.index("the Cloudflare edge to serve the tunnel hostname")
        repoint = live.index('gh("PATCH", "/app/hook/config"')
        self.assertLess(tunnel_probe, repoint)

    def test_unreachable_edge_fails_the_stage(self):
        """The probe must fail closed. poll() raises StageFailure on timeout,
        so the stage cannot complete with an unserved hostname."""
        body = self._tunnel_body()
        self.assertRegex(body, r"poll\(edge_serving,")

    def test_missing_webhook_reports_what_github_recorded(self):
        """"last=None" discarded the only evidence that separates "GitHub
        never delivered" from "GitHub delivered and got a non-202"."""
        verify = _source(VERIFY)
        self.assertIn("GitHub recorded", verify)
        self.assertIn("none at all", verify)

    def test_delivery_diagnostics_carry_no_secrets(self):
        """The diagnostic must report event, action, status and timestamp -
        never headers, payloads or the response body."""
        verify = _source(VERIFY)
        block = verify[verify.index("except StageFailure:"):
                       verify.index("# 202 alone is not acceptance")]
        # Comments describe the rule; only executable lines can break it.
        block = "\n".join(line for line in block.splitlines()
                          if not line.lstrip().startswith("#"))
        for forbidden in ("request_headers", "payload", "response",
                          "Authorization", "secret"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, block)


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
