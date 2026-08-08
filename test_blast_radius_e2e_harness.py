"""Fail-closed contract tests for the dedicated blast-radius live proof.

These tests never contact GitHub, start a tunnel, or require PostgreSQL.  They
exercise the pure manifest/proof helpers in process and statically constrain
the manual workflow before it is ever dispatched.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).parent
DRIVER = ROOT / "scripts" / "e2e" / "blast_radius_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "governance-e2e.yml"


def _source(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required harness file is missing: {path}")
    return path.read_text(encoding="utf-8")


def _load_driver(evidence_dir: Path, name: str):
    if not DRIVER.is_file():
        raise AssertionError(f"dedicated driver is missing: {DRIVER}")
    old_argv = sys.argv
    sys.argv = [str(DRIVER), str(evidence_dir)]
    try:
        spec = importlib.util.spec_from_file_location(name, DRIVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.argv = old_argv


def _manifest():
    project = "blast_fixture"
    source = f"source.{project}.raw.orders"
    staging = f"model.{project}.stg_orders"
    intermediate = f"model.{project}.int_order_payments"
    fact = f"model.{project}.fct_orders"
    direct_a = f"model.{project}.customer_lifetime_value"
    direct_b = f"model.{project}.fct_revenue_daily"
    transitive = f"model.{project}.executive_revenue_summary"
    exposure = f"exposure.{project}.revenue_dashboard"
    return {
        "nodes": {
            staging: {"resource_type": "model", "name": "stg_orders",
                      "depends_on": {"nodes": [source]}},
            intermediate: {"resource_type": "model", "name": "int_order_payments",
                           "depends_on": {"nodes": [staging]}},
            fact: {"resource_type": "model", "name": "fct_orders",
                   "depends_on": {"nodes": [staging, intermediate]}},
            direct_a: {"resource_type": "model", "name": "customer_lifetime_value",
                       "depends_on": {"nodes": [fact]}},
            direct_b: {"resource_type": "model", "name": "fct_revenue_daily",
                       "depends_on": {"nodes": [fact]}},
            transitive: {"resource_type": "model", "name": "executive_revenue_summary",
                         "depends_on": {"nodes": [direct_b]}},
        },
        "sources": {source: {"resource_type": "source", "name": "orders"}},
        "exposures": {
            exposure: {"resource_type": "exposure", "name": "revenue_dashboard",
                       "depends_on": {"nodes": [direct_a, direct_b]}}
        },
    }


class BlastRadiusFixtureTests(unittest.TestCase):
    @staticmethod
    def _main_project():
        return {
            "dbt_project.yml": "name: relium_e2e_dbt\nprofile: relium_e2e_dbt\n",
            "profiles.yml": "relium_e2e_dbt:\n  target: dev\n",
            "models/staging/stg_orders.sql":
                "select * from {{ source('raw', 'orders') }}\n",
            "models/marts/finance/fct_orders.sql":
                "select * from {{ ref('stg_orders') }}\n",
            "models/marts/customers/customer_lifetime_value.sql":
                "select * from {{ ref('fct_orders') }}\n",
            "models/marts/finance/fct_revenue_daily.sql":
                "select * from {{ ref('fct_orders') }}\n",
            "models/executive/exec_daily_kpis.sql":
                "select * from {{ ref('fct_revenue_daily') }}\n",
        }

    def test_fixture_sources_change_only_the_fact_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_fixture_sources")
            self.assertIn("main_files", inspect.signature(
                driver._fixture_files).parameters)
            main = self._main_project()
            direct_models = ["customer_lifetime_value", "fct_revenue_daily"]
            base = driver._fixture_files(
                main, changed_fact=False, exposure_models=direct_models)
            head = driver._fixture_files(
                main, changed_fact=True, exposure_models=direct_models)

        changed = sorted(path for path in set(base) | set(head)
                         if base.get(path) != head.get(path))
        fact_path = "models/marts/finance/fct_orders.sql"
        self.assertEqual(changed, [fact_path])
        self.assertEqual(base[fact_path], main[fact_path])
        self.assertIn("source('raw', 'orders')", base["models/staging/stg_orders.sql"])
        self.assertIn("relium.yml", base)
        self.assertEqual(base["relium.yml"],
                         "enabled: true\nenforcement_mode: enforce\n")
        self.assertNotIn("relium.yml", main)
        exposure = base["models/blast_radius_exposure.yml"]
        self.assertIn("type: dashboard", exposure)
        self.assertNotIn("type: exposure", exposure)
        self.assertNotIn("models/blast_radius_exposure.yml", main)

    def test_fixture_driver_does_not_hardcode_expected_direct_product_identities(self):
        source = _source(DRIVER)
        self.assertNotIn("customer_lifetime_value", source)
        self.assertNotIn("fct_revenue_daily", source)

    def test_fixture_project_is_read_from_the_exact_default_main_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_read_main_project")
            calls = []
            tree = {"truncated": False, "tree": [
                {"path": "dbt_project.yml", "type": "blob", "sha": "one"},
                {"path": "profiles.yml", "type": "blob", "sha": "two"},
                {"path": "models/marts/finance/fct_orders.sql",
                 "type": "blob", "sha": "three"},
                {"path": "README.md", "type": "blob", "sha": "ignored"},
            ]}

            def fake_gh(method, path, token, bearer=True):
                calls.append((method, path, token, bearer))
                if "/git/trees/main-sha?recursive=1" in path:
                    return 200, tree
                blobs = {
                    "one": "bmFtZTogcmVsaXVtX2UyZV9kYnQK",
                    "two": "cmVsaXVtX2UyZV9kYnQ6IHt9Cg==",
                    "three": "c2VsZWN0IDEK",
                }
                sha = path.rsplit("/", 1)[-1]
                return 200, {"encoding": "base64", "content": blobs[sha]}

            driver.gh = fake_gh
            self.assertTrue(hasattr(driver, "_read_fixture_project"),
                            "driver must read the exact fixture-main project")
            files = driver._read_fixture_project("fixture-token", "main-sha")

        self.assertEqual(sorted(files), [
            "dbt_project.yml", "models/marts/finance/fct_orders.sql", "profiles.yml"])
        self.assertFalse(any("ignored" in path for _, path, *_ in calls))

    def test_truncated_default_main_tree_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_truncated_main_tree")
            driver.gh = lambda *_args, **_kwargs: (
                200, {"truncated": True, "tree": []})
            self.assertTrue(hasattr(driver, "_read_fixture_project"),
                            "driver must fail closed on a truncated Git tree")
            with self.assertRaisesRegex(driver.StageFailure, "truncated"):
                driver._read_fixture_project("fixture-token", "main-sha")

    def test_manifest_topology_derives_direct_models_and_excludes_other_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_manifest_topology")
            try:
                proof = driver._manifest_topology(
                    _manifest(), changed_model_name="fct_orders")
            except driver.StageFailure as exc:
                self.fail(str(exc))

        self.assertEqual(
            [node.rsplit(".", 1)[-1] for node in proof["direct_model_ids"]],
            ["customer_lifetime_value", "fct_revenue_daily"],
        )
        self.assertNotIn(proof["transitive_model_id"], proof["direct_model_ids"])
        self.assertNotIn(proof["exposure_id"], proof["direct_model_ids"])
        self.assertTrue(proof["source_to_staging"])
        self.assertTrue(proof["staging_to_changed_model"])

    def test_dbt_parse_is_mandatory_and_failure_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_parse_required")
            self.assertIn("main_files", inspect.signature(
                driver._fixture_files).parameters)
            completed = subprocess.CompletedProcess(
                args=["dbt", "parse"], returncode=2, stdout="parse failed", stderr="bad project")
            with mock.patch.object(driver.shutil, "which", return_value="/venv/bin/dbt"), \
                    mock.patch.object(driver.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(driver.StageFailure, "dbt parse failed"):
                    driver._parse_manifest(
                        driver._fixture_files(
                            self._main_project(), False,
                            ["customer_lifetime_value", "fct_revenue_daily"]))

        source = _source(DRIVER)
        main = source[source.index("def main() -> int:"):]
        self.assertGreaterEqual(main.count("_parse_manifest("), 2)


class BlastRadiusTruthTests(unittest.TestCase):
    def test_persisted_plan_and_public_api_must_exactly_match_manifest_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_exact_truth")
            topology = driver._manifest_topology(
                _manifest(), changed_model_name="fct_orders")
            expected = topology["direct_model_ids"]
            persisted = {"payload": {"plan": {"downstream_models": expected}}}
            public = {"review_id": "review-1",
                      "change_plan": {"downstream_models": expected}}
            proof = driver._verify_backend_truth(topology, persisted, public)
            self.assertEqual(proof["persisted_downstream_model_ids"], expected)
            self.assertEqual(proof["public_api_downstream_model_ids"], expected)

            public["change_plan"]["downstream_models"] = expected[:1]
            with self.assertRaisesRegex(driver.StageFailure, "public API"):
                driver._verify_backend_truth(topology, persisted, public)

    def test_expected_production_model_ids_are_not_hardcoded_in_truth_assertion(self):
        source = _source(DRIVER)
        body = source[source.index("def _verify_backend_truth("):
                      source.index("def _sanitized_public_review(")]
        self.assertNotIn("customer_lifetime_value", body)
        self.assertNotIn("fct_revenue_daily", body)
        self.assertIn('topology["direct_model_ids"]', body)

    def test_frontend_browser_leg_is_truthfully_not_run(self):
        source = _source(DRIVER)
        self.assertIn('"status": "NOT_RUN"', source)
        self.assertIn("promoted frontend source is unavailable", source)
        self.assertNotIn("sync_playwright", source)
        self.assertNotIn("browser_capture", source)


class BlastRadiusOwnershipTests(unittest.TestCase):
    def test_initial_recovery_preclaims_no_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_initial_ref_ownership")
            record = driver._initial_recovery()

        self.assertEqual(record["owned_branches"], [])
        self.assertEqual(record["owned_branch_heads"], {})
        self.assertEqual(record["branch_mutation_intents"], [])
        self.assertEqual(record["branch_head_mutation_intents"], [])
        self.assertEqual(record["branch_candidates"], [
            f"e2e/blast-radius-base-{driver.RUN}",
            f"e2e/blast-radius-head-{driver.RUN}",
        ])
        self.assertNotIn("branches", record)

    def test_branch_create_preflights_absence_then_records_exact_verified_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_create_owned_ref")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path, body))
                if method == "GET" and len(calls) == 1:
                    return 404, {}
                if method == "POST":
                    return 201, {}
                if method == "GET":
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": "base-sha"}}
                self.fail(f"unexpected operation {method} {path}")

            driver.gh = fake_gh
            driver._make_branch("fixture-token", branch, "base-sha")
            saved = driver._load_recovery()

        self.assertEqual([call[0] for call in calls], ["GET", "POST", "GET"])
        self.assertEqual(saved["owned_branches"], [branch])

    def test_lost_create_response_requires_exact_ref_verification_before_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_lost_ref_response")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if method == "GET" and len(calls) == 1:
                    return 404, {}
                if method == "POST":
                    raise TimeoutError("response lost")
                return 200, {"ref": f"refs/heads/{branch}",
                             "object": {"type": "commit", "sha": "base-sha"}}

            driver.gh = fake_gh
            driver._make_branch("fixture-token", branch, "base-sha")
            saved = driver._load_recovery()

        self.assertEqual(saved["owned_branches"], [branch])

    def test_branch_create_polls_recognized_delayed_404_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_create_delayed_visibility")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            posted = {"value": False, "reads": 0}

            def fake_gh(method, path, _token, body=None, bearer=True):
                if method == "POST":
                    posted["value"] = True
                    return 201, {}
                if method == "GET" and not posted["value"]:
                    return 404, {}
                if method == "GET":
                    posted["reads"] += 1
                    if posted["reads"] <= 2:
                        return 404, {}
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": "base-sha"}}
                self.fail(f"unexpected operation {method} {path}")

            driver.gh = fake_gh
            with mock.patch.object(driver.time, "sleep") as sleep:
                driver._make_branch("fixture-token", branch, "base-sha")

        self.assertEqual(posted["reads"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_failed_ownership_write_compensates_by_removing_exact_created_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_ref_record_failure")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            calls = []
            ref_present = {"value": False}

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if method == "POST":
                    ref_present["value"] = True
                    return 201, {}
                if method == "DELETE":
                    ref_present["value"] = False
                    return 204, {}
                if method == "GET" and ref_present["value"]:
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": "base-sha"}}
                return 404, {}

            driver.gh = fake_gh
            real_write = driver._write_recovery
            writes = {"count": 0}

            def fail_promotion(document):
                writes["count"] += 1
                if writes["count"] == 1:
                    return real_write(document)
                raise OSError("disk write failed")

            with mock.patch.object(driver, "_write_recovery",
                                   side_effect=fail_promotion):
                with self.assertRaises(OSError):
                    driver._make_branch("fixture-token", branch, "base-sha")

            saved = driver._load_recovery()

        self.assertEqual(saved["owned_branches"], [])
        self.assertIn(("DELETE", f"/repos/{driver.REPO}/git/refs/heads/{branch}"), calls)
        self.assertEqual(calls[-1],
                         ("GET", f"/repos/{driver.REPO}/git/ref/heads/{branch}"))

    def test_fixture_pull_request_is_always_draft(self):
        source = _source(DRIVER)
        fixture_body = source[source.index("def _create_fixture_pr("):
                              source.index("def _process_alive(")]
        self.assertIn('"draft": True', fixture_body)
        self.assertNotIn('"draft": False', fixture_body)

    def test_created_pr_response_must_be_exact_draft_and_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_created_pr_response")
            branches = ["e2e/blast-radius-base-abc123",
                        "e2e/blast-radius-head-abc123"]
            valid = {"number": 72, "state": "open", "draft": True,
                     "head": {"ref": branches[1], "sha": "head-sha"},
                     "base": {"ref": branches[0], "sha": "base-sha"}}
            self.assertEqual(
                driver._validate_created_pr_response(valid, branches), valid)
            for field, value in (("draft", False), ("state", "closed")):
                invalid = dict(valid, **{field: value})
                with self.assertRaisesRegex(driver.StageFailure, "draft OPEN"):
                    driver._validate_created_pr_response(invalid, branches)
            wrong_head = dict(valid, head={"ref": "main", "sha": "head-sha"})
            with self.assertRaisesRegex(driver.StageFailure, "exact owned refs"):
                driver._validate_created_pr_response(wrong_head, branches)

    def test_cleanup_promotes_exact_interrupted_intent_then_applies_pr_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_intent_exact_cleanup")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            record["branch_mutation_intents"] = [{
                "branch": branch,
                "ref": f"refs/heads/{branch}",
                "expected_sha": "base-sha",
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []
            ref_present = {"value": True}

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if "/pulls?" in path:
                    return 200, []
                if method == "DELETE":
                    ref_present["value"] = False
                    return 204, {}
                if method == "GET" and ref_present["value"]:
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": "base-sha"}}
                return 404, {}

            driver.gh = fake_gh
            result = driver.cleanup("interrupted-post")
            saved = driver._load_recovery()

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(saved["branch_mutation_intents"], [])
        self.assertEqual(saved["owned_branches"], [])
        self.assertEqual(saved["owned_branch_heads"], {})
        self.assertEqual(result["fixture_branches_deleted"], [branch])
        get_index = calls.index(
            ("GET", f"/repos/{driver.REPO}/git/ref/heads/{branch}"))
        delete_index = calls.index(
            ("DELETE", f"/repos/{driver.REPO}/git/refs/heads/{branch}"))
        self.assertLess(get_index, delete_index)

    def test_cleanup_mismatched_interrupted_intent_fails_without_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_intent_mismatch_cleanup")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            record["branch_mutation_intents"] = [{
                "branch": branch,
                "ref": f"refs/heads/{branch}",
                "expected_sha": "expected-sha",
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                return 200, {"ref": f"refs/heads/{branch}",
                             "object": {"type": "commit", "sha": "wrong-sha"}}

            driver.gh = fake_gh
            result = driver.cleanup("mismatched-intent")

        self.assertFalse(result["cleanup_passed"])
        self.assertIn("does not exactly match", " ".join(result["failures"]))
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_cleanup_404_intent_clears_without_claiming_or_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_intent_absent_cleanup")
            record = driver._initial_recovery()
            branch = record["branch_candidates"][0]
            record["branch_mutation_intents"] = [{
                "branch": branch,
                "ref": f"refs/heads/{branch}",
                "expected_sha": "expected-sha",
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                return 404, {}

            driver.gh = fake_gh
            result = driver.cleanup("absent-intent")
            saved = driver._load_recovery()

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(saved["branch_mutation_intents"], [])
        self.assertEqual(saved["owned_branches"], [])
        self.assertFalse(any(method == "DELETE" for method, _path in calls))


class BlastRadiusRecoveryDurabilityTests(unittest.TestCase):
    def test_interrupted_atomic_replace_preserves_last_valid_recovery_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_atomic_recovery")
            valid = driver._initial_recovery()
            changed = dict(valid, webhook_mutated=True)
            with mock.patch.object(driver.os, "replace",
                                   side_effect=OSError("interrupted replace")):
                with self.assertRaises(OSError):
                    driver._write_recovery(changed)

            self.assertEqual(driver._load_recovery(), valid)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class BlastRadiusHeadMutationTests(unittest.TestCase):
    @staticmethod
    def _owned_base(driver, old_sha="old-sha"):
        record = driver._initial_recovery()
        branch = record["branch_candidates"][0]
        record["owned_branches"] = [branch]
        record["owned_branch_heads"] = {branch: old_sha}
        driver._write_recovery(record)
        return record, branch

    @staticmethod
    def _git_data_gh(driver, branch, remote, calls):
        def fake_gh(method, path, _token, body=None, bearer=True):
            calls.append((method, path, body))
            if method == "GET" and "/git/commits/" in path:
                return 200, {"sha": remote["sha"], "tree": {"sha": "tree-old"}}
            if method == "POST" and path.endswith("/git/blobs"):
                return 201, {"sha": "blob-new"}
            if method == "POST" and path.endswith("/git/trees"):
                return 201, {"sha": "tree-new"}
            if method == "POST" and path.endswith("/git/commits"):
                return 201, {"sha": "new-sha"}
            if method == "PATCH" and path.endswith(f"/git/refs/heads/{branch}"):
                persisted = driver._load_recovery()
                intent = persisted["branch_head_mutation_intents"]
                if len(intent) != 1:
                    raise AssertionError("head intent was not durable before PATCH")
                remote["sha"] = body["sha"]
                return 200, {"ref": f"refs/heads/{branch}",
                             "object": {"type": "commit", "sha": body["sha"]}}
            if method == "GET" and path.endswith(f"/git/ref/heads/{branch}"):
                return 200, {"ref": f"refs/heads/{branch}",
                             "object": {"type": "commit", "sha": remote["sha"]}}
            raise AssertionError(f"unexpected operation {method} {path}")
        return fake_gh

    def test_git_data_commit_persists_exact_intent_before_ref_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_git_data_commit")
            _record, branch = self._owned_base(driver)
            calls = []
            remote = {"sha": "old-sha"}
            driver.gh = self._git_data_gh(driver, branch, remote, calls)

            result = driver._commit_file_git_data(
                "fixture-token", branch, "models/fct_orders.sql",
                "select 1\n", "fact mutation")
            saved = driver._load_recovery()

        self.assertEqual(result, "new-sha")
        self.assertEqual(saved["owned_branch_heads"][branch], "new-sha")
        self.assertEqual(saved["branch_head_mutation_intents"], [])
        self.assertFalse(any("/contents/" in path for _method, path, _body in calls))
        patch_index = next(i for i, call in enumerate(calls) if call[0] == "PATCH")
        commit_index = next(i for i, call in enumerate(calls)
                            if call[0:2] == ("POST", f"/repos/{driver.REPO}/git/commits"))
        self.assertLess(commit_index, patch_index)
        self.assertNotIn("/contents/", _source(DRIVER))

    def test_ref_update_polls_only_exact_old_sha_until_new_sha_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_update_delayed_visibility")
            _record, branch = self._owned_base(driver)
            reads = {"count": 0}

            def fake_gh(method, path, _token, body=None, bearer=True):
                if method == "GET" and "/git/commits/" in path:
                    return 200, {"sha": "old-sha", "tree": {"sha": "tree-old"}}
                if method == "POST" and path.endswith("/git/blobs"):
                    return 201, {"sha": "blob-new"}
                if method == "POST" and path.endswith("/git/trees"):
                    return 201, {"sha": "tree-new"}
                if method == "POST" and path.endswith("/git/commits"):
                    return 201, {"sha": "new-sha"}
                if method == "PATCH":
                    return 200, {}
                if method == "GET" and "/git/ref/heads/" in path:
                    reads["count"] += 1
                    sha = "old-sha" if reads["count"] <= 2 else "new-sha"
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": sha}}
                self.fail(f"unexpected operation {method} {path}")

            driver.gh = fake_gh
            with mock.patch.object(driver.time, "sleep") as sleep:
                result = driver._commit_file_git_data(
                    "fixture-token", branch, "models/fct_orders.sql",
                    "select 1\n", "fact mutation")

        self.assertEqual(result, "new-sha")
        self.assertEqual(reads["count"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_promotion_failure_is_recovered_by_fresh_cleanup_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)
            driver = _load_driver(evidence, "blast_head_promotion_failure")
            _record, branch = self._owned_base(driver)
            calls = []
            remote = {"sha": "old-sha"}
            driver.gh = self._git_data_gh(driver, branch, remote, calls)
            real_write = driver._write_recovery
            writes = {"count": 0}

            def fail_promotion(document):
                writes["count"] += 1
                if writes["count"] == 1:
                    return real_write(document)
                raise OSError("promotion write failed")

            with mock.patch.object(driver, "_write_recovery",
                                   side_effect=fail_promotion):
                with self.assertRaises(OSError):
                    driver._commit_file_git_data(
                        "fixture-token", branch, "models/fct_orders.sql",
                        "select 1\n", "fact mutation")
            durable = driver._load_recovery()
            self.assertEqual(durable["owned_branch_heads"][branch], "old-sha")
            self.assertEqual(durable["branch_head_mutation_intents"][0]["new_sha"],
                             "new-sha")

            outer = _load_driver(evidence, "blast_head_promotion_outer")
            outer.FIXTURE_TOKEN = "fixture-token"
            outer_calls = []

            def cleanup_gh(method, path, _token, body=None, bearer=True):
                outer_calls.append((method, path))
                if "/pulls?" in path:
                    return 200, []
                if method == "DELETE":
                    remote["sha"] = None
                    return 204, {}
                if method == "GET" and remote["sha"] is not None:
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": remote["sha"]}}
                return 404, {}

            outer.gh = cleanup_gh
            result = outer.cleanup("fresh-outer")
            saved = outer._load_recovery()

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(saved["branch_head_mutation_intents"], [])
        self.assertEqual(saved["owned_branches"], [])
        self.assertEqual(len([call for call in outer_calls if call[0] == "DELETE"]), 1)

    def test_cleanup_remote_old_sha_clears_unapplied_intent_then_deletes_old_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_head_intent_old")
            record, branch = self._owned_base(driver)
            record["branch_head_mutation_intents"] = [{
                "branch": branch, "ref": f"refs/heads/{branch}",
                "old_sha": "old-sha", "new_sha": "new-sha",
                "operation_identity": {"kind": "git-data-file-commit",
                                       "path": "models/fct_orders.sql",
                                       "blob_sha": "blob-new"},
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            remote = {"sha": "old-sha"}
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if "/pulls?" in path:
                    return 200, []
                if method == "DELETE":
                    remote["sha"] = None
                    return 204, {}
                if method == "GET" and remote["sha"] is not None:
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": remote["sha"]}}
                return 404, {}

            driver.gh = fake_gh
            result = driver.cleanup("mutation-not-applied")
            saved = driver._load_recovery()

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(saved["branch_head_mutation_intents"], [])
        self.assertEqual(result["fixture_branches_deleted"], [branch])

    def test_cleanup_head_intent_sha_mismatch_fails_without_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_head_intent_mismatch")
            record, branch = self._owned_base(driver)
            record["branch_head_mutation_intents"] = [{
                "branch": branch, "ref": f"refs/heads/{branch}",
                "old_sha": "old-sha", "new_sha": "new-sha",
                "operation_identity": {"kind": "git-data-file-commit",
                                       "path": "models/fct_orders.sql",
                                       "blob_sha": "blob-new"},
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                return 200, {"ref": f"refs/heads/{branch}",
                             "object": {"type": "commit", "sha": "other-sha"}}

            driver.gh = fake_gh
            result = driver.cleanup("head-intent-mismatch")

        self.assertFalse(result["cleanup_passed"])
        self.assertIn("head mutation intent", " ".join(result["failures"]))
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_head_intent_404_clears_only_after_pr_absence_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_head_intent_absent_safe")
            record, branch = self._owned_base(driver)
            record["branch_head_mutation_intents"] = [{
                "branch": branch, "ref": f"refs/heads/{branch}",
                "old_sha": "old-sha", "new_sha": "new-sha",
                "operation_identity": {"kind": "git-data-file-commit",
                                       "path": "models/fct_orders.sql",
                                       "blob_sha": "blob-new"},
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if "/pulls?" in path:
                    return 200, []
                return 404, {}

            driver.gh = fake_gh
            result = driver.cleanup("head-intent-absent-safe")
            saved = driver._load_recovery()

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(saved["branch_head_mutation_intents"], [])
        self.assertEqual(saved["owned_branches"], [])
        self.assertEqual(result["fixture_branches_already_absent"], [branch])
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_head_intent_404_is_retained_when_pr_gate_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_head_intent_absent_unsafe")
            record, branch = self._owned_base(driver)
            record["pr_number"] = 71
            record["branch_head_mutation_intents"] = [{
                "branch": branch, "ref": f"refs/heads/{branch}",
                "old_sha": "old-sha", "new_sha": "new-sha",
                "operation_identity": {"kind": "git-data-file-commit",
                                       "path": "models/fct_orders.sql",
                                       "blob_sha": "blob-new"},
            }]
            driver._write_recovery(record)
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if "/git/ref/heads/" in path:
                    return 404, {}
                if path.endswith("/pulls/71"):
                    return 500, {}
                self.fail(f"unexpected operation {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("head-intent-absent-unsafe")
            saved = driver._load_recovery()

        self.assertFalse(result["cleanup_passed"])
        self.assertEqual(len(saved["branch_head_mutation_intents"]), 1)
        self.assertEqual(saved["owned_branches"], [branch])
        self.assertFalse(any(method == "DELETE" for method, _path in calls))


class BlastRadiusStartupTests(unittest.TestCase):
    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def test_api_startup_failure_persists_pid_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_api_start_failure")
            driver._initial_recovery()
            process = self.FakeProcess()
            runtime_state = {"procs": []}
            log_path = Path(tmp) / "api.log"
            with mock.patch.object(driver.lf.subprocess, "Popen", return_value=process), \
                    mock.patch.object(driver.lf, "poll",
                                      side_effect=driver.StageFailure("startup failed")):
                with self.assertRaisesRegex(driver.StageFailure, "startup failed"):
                    driver.lf.start_api(
                        runtime_state, ROOT, "dsn", Path(tmp), "secret", "1", "key",
                        log_path, on_start=driver._persist_process)

            saved = driver._load_recovery()

        self.assertEqual(runtime_state["procs"], [("api", process)])
        self.assertEqual(saved["processes"], [
            {"label": "api", "pid": process.pid, "marker": "uvicorn"}])

    def test_tunnel_startup_failure_retains_handle_and_persists_pid_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_tunnel_start_failure")
            driver._initial_recovery()
            process = self.FakeProcess()
            runtime_state = {"procs": [], "tunnel": None}
            observed = []

            def persist(label, proc, marker):
                observed.append(runtime_state["tunnel"]["proc"] is proc)
                driver._persist_process(label, proc, marker)

            with mock.patch.object(driver.lf.subprocess, "Popen", return_value=process), \
                    mock.patch.object(driver.lf, "poll",
                                      side_effect=driver.StageFailure("tunnel failed")):
                with self.assertRaisesRegex(driver.StageFailure, "tunnel failed"):
                    driver.lf.start_tunnel(
                        runtime_state, Path(tmp) / "tunnel.log", on_start=persist)

            saved = driver._load_recovery()

        self.assertEqual(observed, [True])
        self.assertIs(runtime_state["tunnel"]["proc"], process)
        self.assertEqual(saved["processes"], [
            {"label": "tunnel", "pid": process.pid, "marker": "cloudflared"}])

    def test_cleanup_process_candidates_union_durable_and_in_memory_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_process_union")
            api = self.FakeProcess()
            tunnel = self.FakeProcess()
            tunnel.pid = 4322
            driver.state["procs"] = [("api", api)]
            driver.state["tunnel"] = {"proc": tunnel, "url": None}
            candidates = driver._process_candidates({"processes": []}, [])

        self.assertEqual(
            [(item["label"], item["pid"], item["marker"]) for item in candidates],
            [("api", 4321, "uvicorn"), ("tunnel", 4322, "cloudflared")],
        )

    def test_cleanup_rejects_untrusted_durable_process_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_process_identity_guard")
            failures = []
            candidates = driver._process_candidates({
                "processes": [{"label": "other", "pid": 4321,
                               "marker": "python"}]}, failures)

        self.assertEqual(candidates, [])
        self.assertIn("invalid durable process identity", failures)


class BlastRadiusRefPollingTests(unittest.TestCase):
    def test_wrong_ref_type_or_unrelated_sha_fails_immediately_without_retry(self):
        cases = [
            {"ref": "refs/heads/wrong", "object": {"type": "commit",
                                                      "sha": "new-sha"}},
            {"ref": "refs/heads/proof", "object": {"type": "blob",
                                                      "sha": "new-sha"}},
            {"ref": "refs/heads/proof", "object": {"type": "commit",
                                                      "sha": "unrelated-sha"}},
        ]
        for index, response in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                driver = _load_driver(Path(tmp), f"blast_ref_mismatch_{index}")
                calls = []
                driver.gh = lambda *_args, **_kwargs: (
                    calls.append(True) or (200, response))
                with self.assertRaisesRegex(driver.StageFailure, "unexpected ref state"):
                    driver._wait_for_exact_ref(
                        "proof", "fixture-token", expected_sha="new-sha",
                        transitional_sha="old-sha", description="test update",
                        _sleep=lambda _seconds: self.fail("mismatch must not retry"))
                self.assertEqual(len(calls), 1)

    def test_recognized_transition_times_out_fail_closed_without_real_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_ref_timeout")
            driver.gh = lambda *_args, **_kwargs: (404, {})
            clock_values = iter([0.0, 0.5, 1.1])
            sleeps = []
            with self.assertRaisesRegex(driver.StageFailure, "timed out"):
                driver._wait_for_exact_ref(
                    "proof", "fixture-token", expected_sha="new-sha",
                    absent_is_transitional=True, description="test create",
                    timeout=1.0, interval=0.1,
                    _clock=lambda: next(clock_values),
                    _sleep=sleeps.append)

        self.assertEqual(sleeps, [0.1])


class BlastRadiusCleanupTests(unittest.TestCase):
    def _record(self, driver):
        candidates = [
            "e2e/blast-radius-base-abc123",
            "e2e/blast-radius-head-abc123",
        ]
        record = {
            "run_id": "abc123",
            "webhook_preserved": True,
            "webhook_mutated": True,
            "original_webhook": {
                "url": "https://original.invalid/github/webhook",
                "content_type": "json",
                "insecure_ssl": "0",
            },
            "pr_number": 71,
            "branch_candidates": candidates,
            "owned_branches": list(candidates),
            "owned_branch_heads": {
                candidates[0]: "base-tip-sha",
                candidates[1]: "head-tip-sha",
            },
            "branch_mutation_intents": [],
            "branch_head_mutation_intents": [],
            "processes": [],
        }
        driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
        return record

    @staticmethod
    def _pull(record, number, state="open", merged=False):
        base_branch, head_branch = record["branch_candidates"]
        return {"number": number, "state": state, "merged": merged,
                "draft": True, "head": {"ref": head_branch},
                "base": {"ref": base_branch}}

    def test_cleanup_restores_first_then_closes_unmerged_pr_and_exact_two_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_exact")
            record = self._record(driver)
            driver.FIXTURE_TOKEN = "fixture-token"
            driver.app_jwt = lambda: "app-jwt"
            calls = []
            pr_state = {"state": "open"}
            remote_refs = dict(record["owned_branch_heads"])

            def fake_gh(method, path, token, body=None, bearer=True):
                calls.append((method, path, token, body, bearer))
                if path == "/app/hook/config" and method == "PATCH":
                    return 200, {}
                if path == "/app/hook/config" and method == "GET":
                    return 200, record["original_webhook"]
                if path.endswith("/pulls/71") and method == "GET":
                    return 200, self._pull(
                        record, 71, state=pr_state["state"])
                if path.endswith("/pulls/71") and method == "PATCH":
                    pr_state["state"] = "closed"
                    return 200, self._pull(record, 71, state="closed")
                if "/git/ref/heads/" in path and method == "GET":
                    branch = path.split("/heads/", 1)[1]
                    if branch in remote_refs:
                        return 200, {"ref": f"refs/heads/{branch}",
                                     "object": {"type": "commit",
                                                "sha": remote_refs[branch]}}
                    return 404, {}
                if "/git/refs/heads/" in path and method == "DELETE":
                    branch = path.split("/heads/", 1)[1]
                    remote_refs.pop(branch)
                    return 204, {}
                self.fail(f"unexpected GitHub operation: {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("test")

        self.assertTrue(result["cleanup_passed"], result)
        self.assertTrue(result["webhook"]["verified_through_github"])
        self.assertEqual(result["fixture_pr"]["state"], "closed")
        self.assertFalse(result["fixture_pr"]["merged"])
        deleted = [path for method, path, *_ in calls if method == "DELETE"]
        self.assertEqual(
            deleted,
            [f"/repos/{driver.REPO}/git/refs/heads/{branch}"
             for branch in reversed(record["owned_branches"])],
        )
        self.assertNotIn("matching-refs", "\n".join(path for _, path, *_ in calls))
        restore_index = next(i for i, call in enumerate(calls)
                             if call[0:2] == ("PATCH", "/app/hook/config"))
        pr_close_index = next(i for i, call in enumerate(calls)
                              if call[0] == "PATCH" and "/pulls/71" in call[1])
        self.assertLess(restore_index, pr_close_index)

    def test_cleanup_fails_when_exact_webhook_get_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_webhook_mismatch")
            self._record(driver)
            driver.FIXTURE_TOKEN = "fixture-token"
            driver.app_jwt = lambda: "app-jwt"

            def fake_gh(method, path, _token, body=None, bearer=True):
                if path == "/app/hook/config" and method == "PATCH":
                    return 200, {}
                if path == "/app/hook/config" and method == "GET":
                    return 200, {"url": "https://wrong.invalid/hook",
                                 "content_type": "json"}
                return 404, {}

            driver.gh = fake_gh
            result = driver.cleanup("test")

        self.assertFalse(result["cleanup_passed"])
        self.assertFalse(result["webhook"]["verified_through_github"])

    def test_cleanup_never_deletes_refs_when_pr_state_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_pr_unverifiable")
            record = self._record(driver)
            driver.FIXTURE_TOKEN = "fixture-token"
            driver.app_jwt = lambda: "app-jwt"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path == "/app/hook/config" and method == "PATCH":
                    return 200, {}
                if path == "/app/hook/config" and method == "GET":
                    return 200, record["original_webhook"]
                if path.endswith("/pulls/71") and method == "GET":
                    return 500, {}
                self.fail(f"cleanup must stop before ref deletion: {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("test")

        self.assertFalse(result["cleanup_passed"])
        self.assertEqual(result["fixture_branches_deleted"], [])
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_cleanup_never_deletes_refs_when_pr_was_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_pr_merged")
            record = self._record(driver)
            driver.FIXTURE_TOKEN = "fixture-token"
            driver.app_jwt = lambda: "app-jwt"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path == "/app/hook/config" and method == "PATCH":
                    return 200, {}
                if path == "/app/hook/config" and method == "GET":
                    return 200, record["original_webhook"]
                if path.endswith("/pulls/71") and method == "GET":
                    return 200, self._pull(record, 71, state="closed", merged=True)
                self.fail(f"merged PR must block ref deletion: {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("test")

        self.assertFalse(result["cleanup_passed"])
        self.assertIn("MERGED", " ".join(result["failures"]))
        self.assertEqual(result["fixture_branches_deleted"], [])
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_finalizer_deletes_then_fresh_outer_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)
            driver = _load_driver(evidence, "blast_cleanup_first_process")
            record = self._record(driver)
            record["webhook_mutated"] = False
            driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
            driver.FIXTURE_TOKEN = "fixture-token"
            remote_refs = dict(record["owned_branch_heads"])
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path.endswith("/pulls/71") and method == "GET":
                    return 200, self._pull(record, 71, state="closed")
                for branch, sha in list(remote_refs.items()):
                    if path.endswith(f"/git/ref/heads/{branch}") and method == "GET":
                        return 200, {"ref": f"refs/heads/{branch}",
                                     "object": {"type": "commit", "sha": sha}}
                    if path.endswith(f"/git/refs/heads/{branch}") and method == "DELETE":
                        del remote_refs[branch]
                        return 204, {}
                if "/git/ref/heads/" in path and method == "GET":
                    return 404, {}
                self.fail(f"unexpected operation {method} {path}")

            driver.gh = fake_gh
            first = driver.cleanup("driver-finalizer")
            after_first = driver._load_recovery()

            outer = _load_driver(evidence, "blast_cleanup_fresh_outer")
            outer.FIXTURE_TOKEN = "fixture-token"
            outer.gh = fake_gh
            second = outer.cleanup("workflow-always-step")

        self.assertTrue(first["cleanup_passed"], first)
        self.assertTrue(second["cleanup_passed"], second)
        self.assertEqual(after_first["owned_branches"], [])
        self.assertEqual(after_first["owned_branch_heads"], {})
        self.assertEqual(len([call for call in calls if call[0] == "DELETE"]), 2)
        self.assertEqual(remote_refs, {})

    def test_already_absent_owned_refs_clear_durably_without_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_already_absent")
            record = self._record(driver)
            record["webhook_mutated"] = False
            driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path.endswith("/pulls/71") and method == "GET":
                    return 200, self._pull(record, 71, state="closed")
                if "/git/ref/heads/" in path and method == "GET":
                    return 404, {}
                self.fail(f"already absent ref must not be deleted: {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("fresh-outer")
            saved = driver._load_recovery()

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(saved["owned_branches"], [])
        self.assertEqual(saved["owned_branch_heads"], {})
        self.assertEqual(
            sorted(result["fixture_branches_already_absent"]),
            sorted(record["owned_branches"]),
        )
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_delete_polls_exact_present_ref_until_404_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_delete_delayed_visibility")
            record = self._record(driver)
            record["webhook_mutated"] = False
            driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
            driver.FIXTURE_TOKEN = "fixture-token"
            remote = {branch: {"deleted": False, "lag_reads": 0}
                      for branch in record["owned_branches"]}

            def fake_gh(method, path, _token, body=None, bearer=True):
                if path.endswith("/pulls/71") and method == "GET":
                    return 200, self._pull(record, 71, state="closed")
                if "/git/refs/heads/" in path and method == "DELETE":
                    branch = path.split("/heads/", 1)[1]
                    remote[branch]["deleted"] = True
                    return 204, {}
                if "/git/ref/heads/" in path and method == "GET":
                    branch = path.split("/heads/", 1)[1]
                    state = remote[branch]
                    if state["deleted"]:
                        state["lag_reads"] += 1
                        if state["lag_reads"] > 2:
                            return 404, {}
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit",
                                            "sha": record["owned_branch_heads"][branch]}}
                self.fail(f"unexpected operation {method} {path}")

            driver.gh = fake_gh
            with mock.patch.object(driver.time, "sleep") as sleep:
                result = driver.cleanup("delete-visibility-lag")

        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(sorted(result["fixture_branches_deleted"]),
                         sorted(record["owned_branches"]))

    def test_owned_ref_sha_mismatch_fails_before_any_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_ref_sha_mismatch")
            record = self._record(driver)
            record["webhook_mutated"] = False
            driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
            driver.FIXTURE_TOKEN = "fixture-token"
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path.endswith("/pulls/71") and method == "GET":
                    return 200, self._pull(record, 71, state="closed")
                if "/git/ref/heads/" in path and method == "GET":
                    branch = path.split("/heads/", 1)[1]
                    sha = (record["owned_branch_heads"][branch]
                           if branch.endswith("head-abc123") else "wrong-sha")
                    return 200, {"ref": f"refs/heads/{branch}",
                                 "object": {"type": "commit", "sha": sha}}
                self.fail(f"mismatch must stop before delete: {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("sha-mismatch")

        self.assertFalse(result["cleanup_passed"])
        self.assertIn("does not match durable ownership", " ".join(result["failures"]))
        self.assertFalse(any(method == "DELETE" for method, _path in calls))

    def test_exact_webhook_restore_also_verifies_tls_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_tls_mismatch")
            record = self._record(driver)
            driver.app_jwt = lambda: "app-jwt"

            def fake_gh(method, path, _token, body=None, bearer=True):
                if method == "PATCH":
                    self.assertIn("insecure_ssl", body)
                    self.assertEqual(body["insecure_ssl"], "0")
                    return 200, {}
                return 200, {"url": record["original_webhook"]["url"],
                             "content_type": "json", "insecure_ssl": "1"}

            driver.gh = fake_gh
            result = driver.restore_webhook(record)

        self.assertFalse(result["verified_through_github"])
        self.assertFalse(result["insecure_ssl_matches_original"])

    def test_fresh_cleanup_invents_no_restoration_or_fixture_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_fresh")
            driver.gh = lambda *_args, **_kwargs: self.fail(
                "fresh cleanup must not touch GitHub")
            result = driver.cleanup("fresh")

        self.assertTrue(result["cleanup_passed"])
        self.assertTrue(result["nothing_owned"])
        self.assertIsNone(result["webhook"]["restored"])
        self.assertFalse(result["webhook"]["verified_through_github"])
        self.assertIsNone(result["fixture_pr"])
        self.assertEqual(result["fixture_branches_deleted"], [])

    def test_in_process_children_are_reaped_not_left_as_zombies(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_reaps_children")

            class FakeProcess:
                pid = 321

                def __init__(self):
                    self.returncode = None
                    self.wait_called = False

                def terminate(self):
                    pass

                def wait(self, timeout=None):
                    self.wait_called = True
                    self.returncode = 0
                    return 0

                def kill(self):
                    self.returncode = -9

            class FakeProcPath:
                def is_file(self):
                    return True

                def read_bytes(self):
                    return b"python\x00-c\x00uvicorn.run()"

            process = FakeProcess()
            driver.state["procs"] = [("api", process)]
            driver.Path = lambda _value: FakeProcPath()
            driver._process_alive = lambda _pid: process.returncode is None
            failures = []
            result = driver._stop_recorded_processes(
                {"processes": [{"label": "api", "pid": 321,
                                "marker": "uvicorn"}]}, failures)

        self.assertTrue(process.wait_called)
        self.assertEqual(failures, [])
        self.assertTrue(result[0]["stopped"])

    def test_cleanup_rejects_any_non_owned_or_incomplete_branch_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_ref_guard")
            record = self._record(driver)
            record["owned_branches"] = ["main", "e2e/blast-radius-head-abc123"]
            driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
            calls = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path == "/app/hook/config" and method == "PATCH":
                    return 200, {}
                if path == "/app/hook/config" and method == "GET":
                    return 200, record["original_webhook"]
                self.fail("invalid ownership record must not mutate fixture refs")

            driver.app_jwt = lambda: "app-jwt"
            driver.gh = fake_gh
            result = driver.cleanup("test")

        self.assertFalse(result["cleanup_passed"])
        self.assertIn("owned branch record", " ".join(result["failures"]))
        self.assertTrue(result["webhook"]["verified_through_github"])
        self.assertEqual(calls, [("PATCH", "/app/hook/config"),
                                 ("GET", "/app/hook/config")])

    def test_owned_branch_record_rejects_path_like_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_run_id_guard")
            record = {"run_id": "../../main",
                      "branch_candidates": ["e2e/blast-radius-base-../../main",
                                            "e2e/blast-radius-head-../../main"],
                      "owned_branches": [], "branch_mutation_intents": [],
                      "branch_head_mutation_intents": []}
            with self.assertRaisesRegex(driver.StageFailure, "run id"):
                driver._validate_owned_branches(record)

    def test_cleanup_discovers_exact_owned_pr_when_number_was_not_recorded(self):
        """The PR POST may succeed immediately before the process is killed.

        Recovery cannot depend solely on persisting the response; it may query
        only the exact owned head/base pair and must close the one match.
        """
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_cleanup_discover_pr")
            record = self._record(driver)
            record["pr_number"] = None
            driver.RUN_RECOVERY.write_text(json.dumps(record), encoding="utf-8")
            driver.FIXTURE_TOKEN = "fixture-token"
            driver.app_jwt = lambda: "app-jwt"
            calls = []
            pr_state = {"state": "open"}
            remote_refs = dict(record["owned_branch_heads"])

            def fake_gh(method, path, _token, body=None, bearer=True):
                calls.append((method, path))
                if path == "/app/hook/config" and method == "PATCH":
                    return 200, {}
                if path == "/app/hook/config" and method == "GET":
                    return 200, record["original_webhook"]
                if "/pulls?" in path and method == "GET":
                    self.assertIn("head=AbhishekhPR%3Ae2e%2Fblast-radius-head-abc123", path)
                    self.assertIn("base=e2e%2Fblast-radius-base-abc123", path)
                    return 200, [self._pull(record, 72, state="open")]
                if path.endswith("/pulls/72") and method == "GET":
                    return 200, self._pull(
                        record, 72, state=pr_state["state"])
                if path.endswith("/pulls/72") and method == "PATCH":
                    pr_state["state"] = "closed"
                    return 200, self._pull(record, 72, state="closed")
                if "/git/ref/heads/" in path and method == "GET":
                    branch = path.split("/heads/", 1)[1]
                    if branch in remote_refs:
                        return 200, {"ref": f"refs/heads/{branch}",
                                     "object": {"type": "commit",
                                                "sha": remote_refs[branch]}}
                    return 404, {}
                if "/git/refs/heads/" in path and method == "DELETE":
                    branch = path.split("/heads/", 1)[1]
                    remote_refs.pop(branch)
                    return 204, {}
                self.fail(f"unexpected GitHub operation: {method} {path}")

            driver.gh = fake_gh
            result = driver.cleanup("test")

        self.assertIsNotNone(result["fixture_pr"], result)
        self.assertTrue(result["cleanup_passed"], result)
        self.assertEqual(result["fixture_pr"]["number"], 72)
        self.assertEqual(result["fixture_pr"]["state"], "closed")
        fixture_mutations = [path for method, path in calls
                             if method in ("PATCH", "DELETE") and path != "/app/hook/config"]
        self.assertEqual(len(fixture_mutations), 3)

    def test_lost_response_discovery_uses_all_states_and_rejects_untyped_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_discovery_typed")
            record = self._record(driver)
            driver.FIXTURE_TOKEN = "fixture-token"
            seen = []

            def fake_gh(method, path, _token, body=None, bearer=True):
                seen.append(path)
                return 200, [{"number": "not-an-integer", "state": "open"}]

            driver.gh = fake_gh
            with self.assertRaisesRegex(driver.StageFailure, "typed"):
                driver._discover_owned_pr(record["branch_candidates"])

        self.assertIn("state=all", seen[0])


class BlastRadiusWorkflowTests(unittest.TestCase):
    def test_initial_durable_state_satisfies_live_flow_webhook_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = _load_driver(Path(tmp), "blast_live_flow_state_contract")
            record = driver._initial_recovery()
            calls = []

            def fake_gh(method, path, token, body=None, bearer=True):
                calls.append((method, path, token, body))
                if method == "GET" and path == "/app":
                    return 200, {"slug": driver.APP_SLUG}
                if method == "PATCH" and path == "/app/hook/config":
                    return 200, {}
                self.fail(f"unexpected operation {method} {path}")

            proof = driver.lf.point_webhook(
                driver.state, fake_gh, lambda: "app-jwt",
                "https://tunnel.example.invalid")

        self.assertTrue({"procs", "tunnel", "expected_slug", "mutated",
                         "cleanup_done", "cleanup_result"}
                        .issubset(driver.state))
        self.assertEqual(record["expected_app_slug"], driver.APP_SLUG)
        self.assertEqual(driver.state["expected_slug"], driver.APP_SLUG)
        self.assertTrue(driver.state["mutated"])
        self.assertEqual(proof["app_slug"], driver.APP_SLUG)
        self.assertEqual([call[:2] for call in calls], [
            ("GET", "/app"), ("PATCH", "/app/hook/config")])

    def test_manual_workflow_has_a_focused_blast_radius_job(self):
        doc = yaml.safe_load(_source(WORKFLOW))
        triggers = doc.get(True) or doc.get("on")
        choices = triggers["workflow_dispatch"]["inputs"]["operation"]["options"]
        self.assertIn("blast-radius", choices)
        job = doc["jobs"]["blast-radius"]
        self.assertEqual(job["if"], "inputs.operation == 'blast-radius'")
        self.assertLessEqual(job["timeout-minutes"], 35)
        commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
        self.assertIn("blast_radius_e2e.py", commands)
        self.assertNotIn("governance_e2e.py", commands)
        self.assertNotIn("metadata_review_e2e.py", commands)
        self.assertIn("requirements-direct-downstream-e2e.txt", commands)

    def test_workflow_uses_secure_credentials_and_cleanup_includes_artifact_proof(self):
        doc = yaml.safe_load(_source(WORKFLOW))
        self.assertIn("blast-radius", doc["jobs"])
        job = doc["jobs"]["blast-radius"]
        serialized = json.dumps(job)
        self.assertIn("RELIUM_E2E_PRIVATE_KEY", serialized)
        self.assertIn("RELIUM_E2E_FIXTURE_TOKEN", serialized)
        self.assertNotIn("RELIUM_PILOT", serialized.upper())
        names = [step.get("name") for step in job["steps"]]
        cleanup_index = names.index("Mandatory exact blast-radius cleanup")
        scan_index = names.index("Scan blast-radius evidence for secrets")
        upload_index = names.index("Upload blast-radius evidence")
        self.assertLess(cleanup_index, scan_index)
        self.assertLess(scan_index, upload_index)
        self.assertEqual(job["steps"][cleanup_index].get("if"), "always()")
        self.assertEqual(job["steps"][scan_index].get("if"), "always()")
        self.assertEqual(job["steps"][scan_index].get("id"), "blast_secret_scan")
        self.assertEqual(
            job["steps"][upload_index].get("if"),
            "always() && steps.blast_secret_scan.outcome == 'success'",
        )

    def test_workflow_records_one_exact_postgres_container_before_cleanup(self):
        doc = yaml.safe_load(_source(WORKFLOW))
        steps = doc["jobs"]["blast-radius"]["steps"]
        capture = next((step for step in steps
                        if step.get("name") == "Record the exact PostgreSQL service"), None)
        self.assertIsNotNone(capture)
        commands = capture["run"]
        self.assertIn("docker ps", commands)
        self.assertIn("ancestor=postgres:18", commands)
        self.assertIn("POSTGRES_SERVICE_ID", commands)
        cleanup = next(step for step in steps
                       if step.get("name") == "Mandatory exact blast-radius cleanup")
        self.assertNotIn("job.services.postgres.id", json.dumps(cleanup))

    def test_cloudflared_is_version_pinned_and_sha256_verified(self):
        doc = yaml.safe_load(_source(WORKFLOW))
        steps = doc["jobs"]["blast-radius"]["steps"]
        install = next(step for step in steps
                       if step.get("name") == "Install pinned cloudflared")
        commands = install["run"]
        self.assertNotIn("/latest/", commands)
        self.assertRegex(commands, r"releases/download/\d{4}\.\d+\.\d+/")
        self.assertIn("sha256sum --check", commands)
        self.assertRegex(commands, r"[0-9a-f]{64}  /tmp/cloudflared")

    def test_driver_arms_cleanup_and_preserves_before_webhook_mutation(self):
        source = _source(DRIVER)
        main = source[source.index("def main() -> int:"):]
        armed = main.index("_arm_cleanup()")
        preserved = main.index("preserve_webhook()")
        mutated = main.index("lf.point_webhook(")
        self.assertLess(armed, preserved)
        self.assertLess(preserved, mutated)
        self.assertNotIn("relium-pilot", source.lower())
        self.assertIn('"pull_request"', source)


if __name__ == "__main__":
    unittest.main()
