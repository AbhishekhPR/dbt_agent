"""The semantic E2E harness, driven against a recording GitHub adapter.

No test here touches GitHub. A fake server holds refs, pulls and the App
webhook config, so the harness performs its real ownership bookkeeping and
its real cleanup against state that can be inspected afterwards.

The question every fault-injection case asks is the same one: if the process
dies here, does cleanup still attempt every artifact that was actually
created, and does it refuse to touch anything that was not?
"""
from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "e2e"))

from live_flow import StageFailure  # noqa: E402


MAIN_SHA = "a" * 40


class FakeGitHub:
    """A minimal GitHub that records every call and can fail on demand."""

    def __init__(self):
        self.refs = {"main": MAIN_SHA}
        self.pulls = {}
        self.next_pr = 41
        self.webhook = {"url": "https://original.example/hook",
                        "content_type": "json", "insecure_ssl": "0"}
        self.calls = []
        self.blocked = set()
        self.commit_counter = 0

    def __call__(self, method, path, token, body=None, bearer=True):
        self.calls.append((method, path))
        for pattern in self.blocked:
            if pattern in f"{method} {path}":
                raise OSError(f"injected transport failure on {method} {path}")
        return self._route(method, path, body)

    def _route(self, method, path, body):
        if path == "/app/hook/config":
            if method == "GET":
                return 200, dict(self.webhook)
            self.webhook.update({k: v for k, v in (body or {}).items()})
            return 200, dict(self.webhook)
        if "/git/ref/heads/" in path and method == "GET":
            branch = path.split("/git/ref/heads/", 1)[1]
            if branch not in self.refs:
                return 404, {}
            return 200, {"object": {"sha": self.refs[branch]}}
        if path.endswith("/git/refs") and method == "POST":
            branch = body["ref"].split("refs/heads/", 1)[1]
            if branch in self.refs:
                return 422, {}
            self.refs[branch] = body["sha"]
            return 201, {"ref": body["ref"], "object": {"sha": body["sha"]}}
        if "/git/refs/heads/" in path and method == "PATCH":
            branch = path.split("/git/refs/heads/", 1)[1]
            self.refs[branch] = body["sha"]
            return 200, {"object": {"sha": body["sha"]}}
        if "/git/refs/heads/" in path and method == "DELETE":
            branch = path.split("/git/refs/heads/", 1)[1]
            if branch not in self.refs:
                return 404, {}
            del self.refs[branch]
            return 204, {}
        if "/git/commits/" in path and method == "GET":
            sha = path.rsplit("/", 1)[1]
            return 200, {"sha": sha, "tree": {"sha": f"tree-{sha}"}}
        if path.endswith("/git/blobs") and method == "POST":
            self.commit_counter += 1
            return 201, {"sha": f"blob{self.commit_counter}"}
        if path.endswith("/git/trees") and method == "POST":
            self.commit_counter += 1
            return 201, {"sha": f"tree{self.commit_counter}"}
        if path.endswith("/git/commits") and method == "POST":
            self.commit_counter += 1
            return 201, {"sha": f"commit{self.commit_counter}"}
        if path.endswith("/pulls") and method == "POST":
            self.next_pr += 1
            self.pulls[self.next_pr] = {
                "number": self.next_pr, "state": "open", "merged": False,
                "head": {"ref": body["head"]}, "base": {"ref": body["base"]}}
            return 201, dict(self.pulls[self.next_pr])
        if "/pulls?" in path and method == "GET":
            head = path.split("head=", 1)[1].split("&")[0].split(":", 1)[1]
            return 200, [dict(p) for p in self.pulls.values()
                         if p["head"]["ref"] == head]
        if "/pulls/" in path and method == "GET":
            number = int(path.rsplit("/", 1)[1])
            if number not in self.pulls:
                return 404, {}
            return 200, dict(self.pulls[number])
        if "/pulls/" in path and method == "PATCH":
            number = int(path.rsplit("/", 1)[1])
            self.pulls[number]["state"] = body.get("state", "open")
            return 200, dict(self.pulls[number])
        return 404, {}

    # -- inspection helpers used by assertions ----------------------------
    def deletions(self):
        return [p for m, p in self.calls if m == "DELETE"]

    def closed_pulls(self):
        return sorted(n for n, p in self.pulls.items() if p["state"] == "closed")


class HarnessTestCase(unittest.TestCase):
    """Loads the driver with its module globals pointed at a temp directory."""

    def setUp(self):
        self.evidence = Path(tempfile.mkdtemp(prefix="relium-semantic-test-"))
        self.addCleanup(shutil.rmtree, self.evidence, ignore_errors=True)
        sys.argv = ["semantic_diff_e2e.py", str(self.evidence)]
        import semantic_diff_e2e as driver
        self.driver = importlib.reload(driver)
        self.gh = FakeGitHub()
        self.driver.GH = self.gh
        self.driver.APP_JWT = lambda: "test-jwt"
        self.driver.FIXTURE_TOKEN = "fixture-token"
        self.driver.RUN = "testrun01"
        self.driver.REPO = "AbhishekhPR/relium-e2e-dbt"
        self.driver.OWNER = "AbhishekhPR"
        self.driver.state.update({"cleanup_done": False, "cleanup_result": None,
                                  "procs": [], "tunnel": None})

    # -- staged driver ----------------------------------------------------
    #
    # Mirrors main()'s ordering without the local service legs, so a crash
    # can be simulated at any boundary the real run passes through.
    STAGES = ("recovery", "webhook_preserved", "webhook_repointed",
              "block_base_branch", "block_head_branch", "block_pr",
              "between_cases", "allow_base_branch", "allow_head_branch",
              "allow_pr", "db_export", "db_restore", "before_browser")

    def drive(self, upto: str):
        d = self.driver
        d._initial_recovery()
        if upto == "recovery":
            return
        d.preserve_webhook()
        if upto == "webhook_preserved":
            return
        record = d._load_recovery()
        record["webhook_mutated"] = True
        d._write_recovery(record)
        self.gh("PATCH", "/app/hook/config", "jwt",
                {"url": "https://tunnel.example/hook", "content_type": "json",
                 "insecure_ssl": "0"})
        if upto == "webhook_repointed":
            return

        for case in d.CASES:
            base, head = d.case_branches(d.RUN, case)
            d.make_branch(base, MAIN_SHA)
            if upto == f"{case}_base_branch":
                return
            d.commit_file(base, "relium.yml", "enabled: true\n", "base")
            base_sha = d._load_recovery()["owned_branch_heads"][base]
            d.make_branch(head, base_sha)
            if upto == f"{case}_head_branch":
                return
            d.commit_file(head, "models/x.sql", f"select 1 -- {case}\n", "head")
            d.open_pull(case, head, base, f"semantic {case}", "do not merge")
            if upto == f"{case}_pr":
                return
            if case == "block" and upto == "between_cases":
                return

        record = d._load_recovery()
        export = self.evidence / "semantic-review-state.sql"
        export.write_text("-- export\n", encoding="utf-8")
        record["database"] = {"export_path": str(export), "restored": None}
        d._write_recovery(record)
        if upto == "db_export":
            return
        record = d._load_recovery()
        record["database"]["restored"] = {"cluster": "throwaway", "port": 55999}
        d._write_recovery(record)
        if upto in ("db_restore", "before_browser"):
            return


class OwnershipRecord(HarnessTestCase):
    def test_the_candidate_set_is_exactly_four_ordered_refs(self):
        self.assertEqual(self.driver.expected_branches("testrun01"), [
            "e2e/semantic-block-base-testrun01",
            "e2e/semantic-block-head-testrun01",
            "e2e/semantic-allow-base-testrun01",
            "e2e/semantic-allow-head-testrun01"])

    def test_a_record_naming_foreign_refs_is_rejected(self):
        record = self.driver._initial_recovery()
        record["branch_candidates"] = ["main", "release"]
        with self.assertRaises(StageFailure):
            self.driver._validate_record(record)

    def test_ownership_must_be_an_ordered_prefix(self):
        record = self.driver._initial_recovery()
        record["owned_branches"] = ["e2e/semantic-allow-head-testrun01"]
        record["owned_branch_heads"] = {"e2e/semantic-allow-head-testrun01": "x"}
        with self.assertRaises(StageFailure):
            self.driver._validate_record(record)

    def test_a_branch_cannot_be_created_out_of_order(self):
        self.driver._initial_recovery()
        with self.assertRaises(StageFailure):
            self.driver.make_branch("e2e/semantic-allow-head-testrun01", MAIN_SHA)

    def test_a_pull_between_unowned_refs_is_refused(self):
        self.driver._initial_recovery()
        with self.assertRaises(StageFailure):
            self.driver.open_pull("block", "main", "release", "t", "b")

    def test_a_pull_whose_refs_are_not_owned_is_never_closed(self):
        record = self.driver._initial_recovery()
        foreign = {"number": 9, "state": "open", "merged": False,
                   "head": {"ref": "somebody-elses"}, "base": {"ref": "main"}}
        with self.assertRaises(StageFailure):
            self.driver._validate_owned_pull(foreign, record["owned_branches"])

    def test_creation_intent_is_durable_before_the_mutating_call(self):
        self.driver._initial_recovery()
        branch = "e2e/semantic-block-base-testrun01"
        seen = {}
        original = self.gh._route

        def capture(method, path, body):
            if method == "POST" and path.endswith("/git/refs"):
                seen["intent"] = self.driver._load_recovery()["branch_mutation_intents"]
            return original(method, path, body)

        self.gh._route = capture
        self.driver.make_branch(branch, MAIN_SHA)
        self.assertEqual(seen["intent"][0]["branch"], branch)


class HappyPathCleanup(HarnessTestCase):
    def test_a_complete_run_removes_every_artifact(self):
        self.drive("before_browser")
        result = self.driver.cleanup("normal")
        self.assertTrue(result["cleanup_passed"], result["failures"])
        self.assertEqual(sorted(self.gh.refs), ["main"])
        self.assertEqual(len(result["fixture_pulls"]), 2)
        self.assertTrue(all(p["verified_through_github"]
                            for p in result["fixture_pulls"]))

    def test_the_webhook_is_restored_to_the_exact_original(self):
        self.drive("before_browser")
        self.driver.cleanup("normal")
        self.assertEqual(self.gh.webhook["url"], "https://original.example/hook")

    def test_both_pulls_are_closed_unmerged(self):
        self.drive("before_browser")
        self.driver.cleanup("normal")
        self.assertEqual(len(self.gh.closed_pulls()), 2)
        self.assertFalse(any(p["merged"] for p in self.gh.pulls.values()))

    def test_the_database_export_is_removed_and_verified_absent(self):
        self.drive("before_browser")
        result = self.driver.cleanup("normal")
        self.assertTrue(result["database"]["export_verified_absent"])
        self.assertFalse((self.evidence / "semantic-review-state.sql").exists())

    def test_cleanup_never_touches_a_ref_it_does_not_own(self):
        self.gh.refs["someone-elses-branch"] = "b" * 40
        self.drive("before_browser")
        self.driver.cleanup("normal")
        self.assertIn("someone-elses-branch", self.gh.refs)
        self.assertIn("main", self.gh.refs)


class FaultInjection(HarnessTestCase):
    """A crash at each boundary must still yield complete cleanup."""

    def _crash_then_cleanup(self, stage: str) -> dict:
        self.drive(stage)
        # A new process would reload the module; cleanup reads only the
        # durable record, so reset the in-process guard to model that.
        self.driver.state["cleanup_done"] = False
        self.driver.state["cleanup_result"] = None
        return self.driver.cleanup(f"crash-after-{stage}")

    def _assert_fully_cleaned(self, result, stage):
        self.assertTrue(result["cleanup_passed"],
                        f"{stage}: {result['failures']}")
        leftover = [r for r in self.gh.refs if r.startswith("e2e/semantic-")]
        self.assertEqual(leftover, [], f"{stage} left refs behind: {leftover}")
        still_open = [n for n, p in self.gh.pulls.items() if p["state"] != "closed"]
        self.assertEqual(still_open, [], f"{stage} left PRs open: {still_open}")
        self.assertEqual(self.gh.webhook["url"], "https://original.example/hook",
                         f"{stage} did not restore the webhook")

    def test_every_stage_is_recoverable(self):
        for stage in ("recovery", "webhook_preserved", "webhook_repointed",
                      "block_base_branch", "block_head_branch", "block_pr",
                      "between_cases", "allow_base_branch", "allow_head_branch",
                      "allow_pr", "db_export", "db_restore", "before_browser"):
            with self.subTest(stage=stage):
                self.setUp()
                result = self._crash_then_cleanup(stage)
                self._assert_fully_cleaned(result, stage)

    def test_cleanup_is_idempotent_at_every_stage(self):
        for stage in ("webhook_repointed", "block_pr", "between_cases",
                      "allow_pr", "before_browser"):
            with self.subTest(stage=stage):
                self.setUp()
                first = self._crash_then_cleanup(stage)
                self.driver.state["cleanup_done"] = False
                self.driver.state["cleanup_result"] = None
                second = self.driver.cleanup("second-pass")
                self.assertTrue(first["cleanup_passed"], first["failures"])
                self.assertTrue(second["cleanup_passed"], second["failures"])
                self.assertEqual(second["fixture_branches_deleted"], [])

    def test_a_branch_created_but_unrecorded_is_adopted_and_removed(self):
        """The window between the durable intent and the recorded success."""
        d = self.driver
        d._initial_recovery()
        d.preserve_webhook()
        branch = "e2e/semantic-block-base-testrun01"
        record = d._load_recovery()
        record["branch_mutation_intents"] = [
            {"branch": branch, "ref": f"refs/heads/{branch}",
             "expected_sha": MAIN_SHA}]
        d._write_recovery(record)
        self.gh.refs[branch] = MAIN_SHA  # the call landed; the record never updated
        result = d.cleanup("crash-inside-branch-creation")
        self.assertNotIn(branch, self.gh.refs)
        self.assertTrue(result["cleanup_passed"], result["failures"])

    def test_an_intent_whose_ref_never_landed_deletes_nothing(self):
        d = self.driver
        d._initial_recovery()
        branch = "e2e/semantic-block-base-testrun01"
        record = d._load_recovery()
        record["branch_mutation_intents"] = [
            {"branch": branch, "ref": f"refs/heads/{branch}",
             "expected_sha": MAIN_SHA}]
        d._write_recovery(record)
        result = d.cleanup("crash-before-ref-created")
        self.assertEqual(self.gh.deletions(), [])
        self.assertTrue(result["cleanup_passed"], result["failures"])

    def test_a_foreign_ref_at_the_intended_name_is_not_claimed(self):
        """Same name, different SHA: somebody else's ref. Never delete it."""
        d = self.driver
        d._initial_recovery()
        branch = "e2e/semantic-block-base-testrun01"
        record = d._load_recovery()
        record["branch_mutation_intents"] = [
            {"branch": branch, "ref": f"refs/heads/{branch}",
             "expected_sha": MAIN_SHA}]
        d._write_recovery(record)
        self.gh.refs[branch] = "f" * 40
        result = d.cleanup("foreign-ref")
        self.assertIn(branch, self.gh.refs)
        self.assertFalse(result["cleanup_passed"])

    def test_a_pull_created_but_unrecorded_is_discovered_and_closed(self):
        d = self.driver
        self.drive("block_head_branch")
        base, head = d.case_branches(d.RUN, "block")
        record = d._load_recovery()
        record["pull_mutation_intents"] = [
            {"case": "block", "head": head, "base": base}]
        d._write_recovery(record)
        status, pull = self.gh("POST", "/repos/x/pulls", "t",
                               {"title": "t", "head": head, "base": base, "body": ""})
        result = d.cleanup("crash-inside-pull-creation")
        self.assertEqual(self.gh.pulls[pull["number"]]["state"], "closed")
        self.assertTrue(result["cleanup_passed"], result["failures"])

    def test_refs_are_retained_when_an_owned_pull_cannot_be_verified(self):
        """Deleting the head ref of a PR we could not close would orphan it."""
        d = self.driver
        self.drive("block_pr")
        self.gh.blocked.add("PATCH /repos/AbhishekhPR/relium-e2e-dbt/pulls/")
        result = d.cleanup("pull-close-fails")
        self.assertFalse(result["cleanup_passed"])
        self.assertIn("e2e/semantic-block-head-testrun01", self.gh.refs)

    def test_a_merged_owned_pull_is_a_hard_failure(self):
        d = self.driver
        self.drive("block_pr")
        number = d._load_recovery()["owned_pulls"][0]["number"]
        self.gh.pulls[number]["merged"] = True
        result = d.cleanup("merged")
        self.assertFalse(result["cleanup_passed"])
        self.assertTrue(any("MERGED" in f for f in result["failures"]))

    def test_cleanup_with_no_record_claims_nothing(self):
        result = self.driver.cleanup("never-started")
        self.assertTrue(result["nothing_owned"])
        self.assertEqual(self.gh.deletions(), [])


class Expectations(HarnessTestCase):
    """Presence-based assertions, using the locally proven real outcomes."""

    BLOCK_EVIDENCE = {"status": "evaluated", "changes": [
        {"kind": "projection_expression_changed", "model_name": "fct_orders",
         "output_name": "net_order_amount",
         "before_sql": "COALESCE(items.gross_order_amount, 0.0) - COALESCE(refunds.refund_amount, 0.0)",
         "after_sql": "COALESCE(items.gross_order_amount, 0.0)"},
        {"kind": "projection_expression_changed", "model_name": "fct_orders",
         "output_name": "net_order_amount_usd", "before_sql": "a", "after_sql": "b"},
        {"kind": "projection_removed", "model_name": "fct_orders",
         "output_name": "refund_amount", "before_sql": "x"},
        {"kind": "join_removed", "model_name": "fct_orders",
         "relation": "int_order_refunds", "before_join_type": "LEFT",
         "before_condition_sql": "orders.order_id = refunds.order_id"}]}
    BLOCK_INCIDENT = {"decision": "BLOCK", "health": 65, "metadata": {
        "manifest_comparison": {"material_sql_changes": [{"model": "fct_orders"}]}}}
    ALLOW_EVIDENCE = {"status": "evaluated", "changes": [
        {"kind": "filter_changed", "model_name": "int_customer_orders",
         "scope": "where", "before_sql": None,
         "after_sql": "NOT status IS NULL"}]}
    ALLOW_INCIDENT = {"decision": "ALLOW", "health": 100, "metadata": {
        "manifest_comparison": {"material_sql_changes": []}}}

    def test_the_proven_block_outcome_passes(self):
        verdict = self.driver.assert_block_expectations(
            self.BLOCK_INCIDENT, self.BLOCK_EVIDENCE)
        self.assertTrue(verdict["passed"], verdict["failures"])
        self.assertEqual(verdict["change_count"], 4)

    def test_extra_truthful_changes_do_not_fail_the_block_case(self):
        evidence = {"status": "evaluated",
                    "changes": self.BLOCK_EVIDENCE["changes"] + [
                        {"kind": "grouping_changed", "model_name": "fct_orders",
                         "before_sql": "a", "after_sql": "b"}]}
        self.assertTrue(
            self.driver.assert_block_expectations(self.BLOCK_INCIDENT, evidence)["passed"])

    def test_evidence_about_an_untouched_model_fails(self):
        evidence = {"status": "evaluated",
                    "changes": self.BLOCK_EVIDENCE["changes"] + [
                        {"kind": "grouping_changed", "model_name": "dim_customers",
                         "before_sql": "a", "after_sql": "b"}]}
        verdict = self.driver.assert_block_expectations(self.BLOCK_INCIDENT, evidence)
        self.assertFalse(verdict["passed"])
        self.assertTrue(any("dim_customers" in f for f in verdict["failures"]))

    def test_a_missing_join_removed_fails_the_block_case(self):
        evidence = {"status": "evaluated", "changes": [
            c for c in self.BLOCK_EVIDENCE["changes"] if c["kind"] != "join_removed"]}
        verdict = self.driver.assert_block_expectations(self.BLOCK_INCIDENT, evidence)
        self.assertFalse(verdict["passed"])
        self.assertTrue(any("join_removed" in str(f) for f in verdict["failures"]))

    def test_evidence_without_the_decision_fails_the_block_case(self):
        verdict = self.driver.assert_block_expectations(
            {"decision": "ALLOW", "health": 100,
             "metadata": {"manifest_comparison": {"material_sql_changes": []}}},
            self.BLOCK_EVIDENCE)
        self.assertFalse(verdict["passed"])

    def test_the_proven_allow_outcome_passes(self):
        verdict = self.driver.assert_allow_expectations(
            self.ALLOW_INCIDENT, self.ALLOW_EVIDENCE)
        self.assertTrue(verdict["passed"], verdict["failures"])

    def test_the_allow_case_fails_if_evidence_is_empty(self):
        verdict = self.driver.assert_allow_expectations(
            self.ALLOW_INCIDENT, {"status": "evaluated", "changes": []})
        self.assertFalse(verdict["passed"])

    def test_the_allow_case_fails_if_a_material_change_appears(self):
        verdict = self.driver.assert_allow_expectations(
            {"decision": "ALLOW", "health": 100, "metadata": {
                "manifest_comparison": {"material_sql_changes": [{"m": 1}]}}},
            self.ALLOW_EVIDENCE)
        self.assertFalse(verdict["passed"])

    def test_unavailable_evidence_is_never_read_as_no_change(self):
        verdict = self.driver.assert_allow_expectations(
            self.ALLOW_INCIDENT, {"status": "unavailable", "changes": []})
        self.assertFalse(verdict["passed"])


class UiAssertionPlan(HarnessTestCase):
    def test_expected_cards_are_derived_from_the_api_payload(self):
        plan = self.driver.plan_ui_assertions({"semantic_evidence": {
            "status": "evaluated", "changes": [
                {"kind": "projection_expression_changed", "model_name": "fct_orders",
                 "output_name": "net_order_amount",
                 "before_sql": "gross - refunds", "after_sql": "gross"},
                {"kind": "join_removed", "model_name": "fct_orders",
                 "relation": "int_order_refunds", "before_join_type": "LEFT",
                 "before_condition_sql": "a = b"}]}})
        concepts = [card["concept"] for card in plan["expected_cards"]]
        self.assertEqual(concepts, ["expression_before_after", "join_removed"])
        self.assertEqual(plan["expected_cards"][0]["before"], "gross - refunds")
        self.assertEqual(plan["expected_cards"][1]["label"], "int_order_refunds")

    def test_an_empty_payload_yields_nothing_to_assert(self):
        plan = self.driver.plan_ui_assertions({})
        self.assertEqual(plan["expected_cards"], [])

    def test_extra_kinds_are_carried_as_additional_truthful_changes(self):
        plan = self.driver.plan_ui_assertions({"semantic_evidence": {"changes": [
            {"kind": "filter_changed", "model_name": "int_customer_orders"}]}})
        self.assertEqual(plan["expected_cards"][0]["concept"],
                         "additional_truthful_change")

    def test_the_plan_keeps_evidence_separate_from_findings(self):
        plan = self.driver.plan_ui_assertions({})
        joined = " ".join(plan["invariants"])
        self.assertIn("separately from findings", joined)
        self.assertIn("blast radius", joined)
        self.assertEqual(plan["browser"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
