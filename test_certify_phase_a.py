"""The Phase A certifier, driven against fakes.

Nothing here touches GitHub, PostgreSQL or the network. What needs proving is
that the certifier would REFUSE a database or a publication that is not the
certified result - a certifier that passes on anything is worse than none.

The fakes mirror shapes taken from run 31406121190's own export: attempt rows
as `review_attempts` returns them, comparisons as the API projection stores
them, and GitHub payloads as the REST API returns them.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "e2e"))

from live_flow import StageFailure  # noqa: E402

REVIEW_ID = "gh-c1a35451991e924c41fb39d"
PULL = 57
COMMENT_ID = 5242686857
CHECK_ID = 93513476030
HEAD_SHA = "b" * 40
SNAP_A = "snap-ed2736ce2c5a44d3a165aa68"
SNAP_B = "snap-e7f7de391403418da7f2c2b8"
APP_ID = 4456468


def load():
    module = importlib.import_module("certify_phase_a")
    module = importlib.reload(module)
    module.results.clear()
    return module


class CertifierTestCase(unittest.TestCase):
    def setUp(self):
        self.c = load()
        self.addCleanup(self.c.results.clear)


# --------------------------------------------------------------- fixtures

def comparison(**overrides):
    doc = {
        "status": "evaluated",
        "baseline_snapshot_id": SNAP_A, "current_snapshot_id": SNAP_B,
        "changes": [
            {"kind": "row_count_changed", "relation": "main.dim_customers",
             "column": None, "signal": "row_count", "before": 1000, "after": 800,
             "absolute_delta": -200, "relative_delta": -0.2},
            {"kind": "null_rate_changed", "relation": "main.dim_customers",
             "column": "customer_id", "signal": "null_rate",
             "before": 0.01, "after": 0.82, "percentage_point_delta": 81.0},
            {"kind": "cardinality_changed", "relation": "main.dim_customers",
             "column": "customer_id", "signal": "cardinality",
             "before": 0.37, "after": 0.42, "percentage_point_delta": 5.0},
        ],
    }
    doc.update(overrides)
    return doc


def truth(**overrides):
    doc = {
        "review": {
            "review_id": REVIEW_ID, "pull_number": PULL, "attempt": 2,
            "base_sha": "a" * 40, "head_sha": HEAD_SHA,
            "lifecycle_state": "DECISION_READY", "decision": "WARN", "health": 100,
            "evidence_coverage": "COMPLETE",
            "github_comment_id": str(COMMENT_ID),
            "github_check_run_id": str(CHECK_ID),
            "payload": {"plan": {"downstream_models":
                                 ["model.relium_e2e_dbt.dim_customers"]}},
        },
        "attempts": [
            {"attempt": 1, "lifecycle_state": "WAITING_FOR_METADATA",
             "decision": None, "health": 100, "trigger": "initial",
             "snapshot_id": None, "metadata_comparison": None,
             "semantic_evidence": {"status": "evaluated", "models": [
                 {"model_name": "int_customer_orders", "status": "evaluated",
                  "changes": [{"kind": "filter_changed", "scope": "where",
                               "model_name": "int_customer_orders"}]}]},
             "payload": {"findings": [{"code": "metadata.pending",
                                       "severity": "warn", "category": "evidence"}]}},
            {"attempt": 2, "lifecycle_state": "METADATA_COMPLETE",
             "decision": "WARN", "health": 100, "trigger": "metadata_snapshot",
             "snapshot_id": SNAP_B, "metadata_comparison": comparison(),
             "semantic_evidence": None,
             "payload": {"findings": [
                 {"code": "column.high_null_rate", "severity": "warn",
                  "category": "production"},
                 {"code": "relation.not_collected", "severity": "info",
                  "category": "production"}]}},
        ],
        "requests": [{"request_id": "req-1", "review_id": REVIEW_ID,
                      "state": "COMPLETED"}],
        "targets": [
            {"request_id": "req-1", "target_index": 0,
             "relation_name": "main.dim_customers", "columns": ["customer_id"],
             "required_signals": ["relation_exists", "null_rate", "row_count"]},
            {"request_id": "req-1", "target_index": 1,
             "relation_name": "main.stg_orders",
             "columns": ["customer_id", "order_id"],
             "required_signals": ["relation_exists", "null_rate"]},
        ],
        "snapshots": [
            {"snapshot_id": SNAP_A, "completeness": "COMPLETE",
             "freshness_state": "CURRENT", "observed_at": "2026-08-10 15:55:00+00"},
            {"snapshot_id": SNAP_B, "completeness": "COMPLETE",
             "freshness_state": "CURRENT", "observed_at": "2026-08-10 15:58:00+00"},
        ],
        "bindings": [{"snapshot_id": SNAP_B, "binding_state": "ACCEPTED"}],
        "outbox": [{"event_type": "review.publication_reconcile_requested",
                    "state": "COMPLETED", "attempts": 1, "last_error": None}],
        "migrations": list(range(1, 13)),
    }
    for key, value in overrides.items():
        doc[key] = value
    return doc


class ReviewCertificationTests(CertifierTestCase):
    def test_the_certified_result_passes(self):
        proof = self.c.certify_review(truth())
        self.assertEqual(proof["decision"], "WARN")
        self.assertEqual(proof["health"], 100)
        self.assertEqual(proof["pull_number"], PULL)

    def test_a_different_decision_is_refused(self):
        doc = truth()
        doc["review"]["decision"] = "ALLOW"
        with self.assertRaises(StageFailure):
            self.c.certify_review(doc)

    def test_a_different_health_is_refused(self):
        doc = truth()
        doc["review"]["health"] = 80
        with self.assertRaises(StageFailure):
            self.c.certify_review(doc)

    def test_an_undecided_review_is_refused(self):
        doc = truth()
        doc["review"]["lifecycle_state"] = "WAITING_FOR_METADATA"
        with self.assertRaises(StageFailure):
            self.c.certify_review(doc)

    def test_another_pull_request_is_refused(self):
        doc = truth()
        doc["review"]["pull_number"] = 99
        with self.assertRaises(StageFailure):
            self.c.certify_review(doc)


class FindingCertificationTests(CertifierTestCase):
    def test_the_certified_findings_pass(self):
        proof = self.c.certify_attempts(truth())
        self.assertIn("column.high_null_rate", proof["finding_codes"])
        self.assertEqual(proof["code_findings"], 0)
        self.assertEqual(proof["comparison_derived_findings"], 0)

    def test_a_missing_high_null_rate_is_refused(self):
        doc = truth()
        doc["attempts"][1]["payload"]["findings"] = []
        with self.assertRaises(StageFailure):
            self.c.certify_attempts(doc)

    def test_a_code_finding_is_refused(self):
        doc = truth()
        doc["attempts"][1]["payload"]["findings"].append(
            {"code": "sql.x", "severity": "warn", "category": "code"})
        with self.assertRaises(StageFailure):
            self.c.certify_attempts(doc)

    def test_a_comparison_derived_finding_is_refused(self):
        doc = truth()
        doc["attempts"][1]["payload"]["findings"].append(
            {"code": "metadata_comparison.row_count_drop", "category": "production"})
        with self.assertRaises(StageFailure):
            self.c.certify_attempts(doc)


class SemanticCertificationTests(CertifierTestCase):
    def test_the_required_change_passes(self):
        proof = self.c.certify_semantic(truth())
        self.assertEqual(proof["models"], ["int_customer_orders"])

    def test_a_wrong_scope_is_refused(self):
        doc = truth()
        doc["attempts"][0]["semantic_evidence"]["models"][0]["changes"][0]["scope"] = "having"
        with self.assertRaises(StageFailure):
            self.c.certify_semantic(doc)

    def test_absent_semantic_evidence_is_refused(self):
        doc = truth()
        for attempt in doc["attempts"]:
            attempt["semantic_evidence"] = None
        with self.assertRaises(StageFailure):
            self.c.certify_semantic(doc)


class BlastRadiusCertificationTests(CertifierTestCase):
    def test_node_ids_pass(self):
        proof = self.c.certify_blast_radius(truth())
        self.assertEqual(proof["direct_downstream_models"],
                         ["model.relium_e2e_dbt.dim_customers"])

    def test_bare_names_are_refused(self):
        doc = truth()
        doc["review"]["payload"]["plan"]["downstream_models"] = ["dim_customers"]
        with self.assertRaises(StageFailure):
            self.c.certify_blast_radius(doc)


class RequestCertificationTests(CertifierTestCase):
    def test_a_bounded_request_passes(self):
        proof = self.c.certify_request(truth())
        self.assertTrue(proof["bounded"])
        self.assertFalse(proof["raw_row_request"])

    def test_a_raw_row_request_is_refused(self):
        doc = truth()
        doc["targets"][0]["required_signals"].append("raw_rows")
        with self.assertRaises(StageFailure):
            self.c.certify_request(doc)


class ComparisonCertificationTests(CertifierTestCase):
    def test_the_certified_comparison_passes(self):
        proof = self.c.certify_comparison(truth())
        self.assertEqual(proof["baseline_snapshot_id"], SNAP_A)
        self.assertEqual(proof["current_snapshot_id"], SNAP_B)
        self.assertEqual(proof["null_rate_percentage_points"], 81.0)
        self.assertEqual(proof["cardinality_percentage_points"], 5.0)

    def test_a_wrong_baseline_is_refused(self):
        doc = truth()
        doc["attempts"][1]["metadata_comparison"] = comparison(
            baseline_snapshot_id="snap-someone-else")
        with self.assertRaises(StageFailure):
            self.c.certify_comparison(doc)

    def test_a_wrong_row_count_delta_is_refused(self):
        doc = truth()
        bad = comparison()
        bad["changes"][0]["after"] = 900
        doc["attempts"][1]["metadata_comparison"] = bad
        with self.assertRaises(StageFailure):
            self.c.certify_comparison(doc)

    def test_a_percentage_point_delta_reported_as_percent_is_refused(self):
        doc = truth()
        bad = comparison()
        bad["changes"][1]["percentage_point_delta"] = 8100.0
        doc["attempts"][1]["metadata_comparison"] = bad
        with self.assertRaises(StageFailure):
            self.c.certify_comparison(doc)

    def test_an_unchanged_fingerprint_reported_as_a_change_is_refused(self):
        doc = truth()
        bad = comparison()
        bad["changes"].append({"kind": "schema_fingerprint_changed",
                               "signal": "schema_fingerprint",
                               "before": "fp", "after": "fp"})
        doc["attempts"][1]["metadata_comparison"] = bad
        with self.assertRaises(StageFailure):
            self.c.certify_comparison(doc)

    def test_a_stale_observation_is_refused(self):
        doc = truth()
        doc["snapshots"][0]["freshness_state"] = "STALE"
        with self.assertRaises(StageFailure):
            self.c.certify_comparison(doc)

    def test_a_baseline_that_does_not_precede_b_is_refused(self):
        doc = truth()
        doc["snapshots"][0]["observed_at"] = "2026-08-10 23:00:00+00"
        with self.assertRaises(StageFailure):
            self.c.certify_comparison(doc)


class PublicationStateTests(CertifierTestCase):
    def test_the_certified_publication_state_passes(self):
        proof = self.c.certify_publication_state(truth())
        self.assertEqual(proof["github_comment_id"], COMMENT_ID)
        self.assertEqual(proof["github_check_run_id"], CHECK_ID)

    def test_a_different_persisted_comment_id_is_refused(self):
        doc = truth()
        doc["review"]["github_comment_id"] = "999"
        with self.assertRaises(StageFailure):
            self.c.certify_publication_state(doc)

    def test_an_incomplete_publication_job_is_refused(self):
        doc = truth()
        doc["outbox"][0]["state"] = "FAILED"
        with self.assertRaises(StageFailure):
            self.c.certify_publication_state(doc)


class ExactPublicationVerificationTests(CertifierTestCase):
    def install(self, *, comment_issue=PULL, comment_app=APP_ID,
                check_head=HEAD_SHA, check_status="completed", check_app=APP_ID,
                comment_status=200, check_http=200):
        seen = []

        def fake_gh(method, path, token, body=None, bearer=True):
            seen.append((method, path, token))
            if path == "/app":
                return 200, {"slug": "relium-e2e", "id": APP_ID}
            if "access_tokens" in path:
                return 201, {"token": "ghs_installation"}
            if "/issues/comments/" in path:
                return comment_status, {
                    "id": COMMENT_ID,
                    "issue_url": f"https://api.github.com/repos/x/y/issues/{comment_issue}",
                    "performed_via_github_app": {"id": comment_app}}
            if "/check-runs/" in path:
                return check_http, {"id": CHECK_ID, "head_sha": check_head,
                                    "status": check_status, "conclusion": "neutral",
                                    "app": {"id": check_app}}
            return 200, {}

        self.c.gh = fake_gh
        self.c.app_jwt = lambda: "jwt"
        self.c.installation_token = lambda jwt=None: "ghs_installation"
        return seen

    def test_the_exact_ids_verify(self):
        seen = self.install()
        proof = self.c.verify_exact_publications(HEAD_SHA)
        self.assertEqual(proof["comment"]["id"], COMMENT_ID)
        self.assertEqual(proof["check_run"]["id"], CHECK_ID)
        self.assertFalse(proof["fixture_token_used"])
        self.assertEqual(proof["selected_by"], "exact persisted ids, not a search")

    def test_the_exact_ids_are_fetched_by_id_not_searched(self):
        seen = self.install()
        self.c.verify_exact_publications(HEAD_SHA)
        paths = [p for _m, p, _t in seen]
        self.assertTrue(any(str(COMMENT_ID) in p for p in paths))
        self.assertTrue(any(str(CHECK_ID) in p for p in paths))
        # A listing endpoint would be "the latest publication", not this one.
        self.assertFalse(any(p.endswith("/comments") for p in paths))
        self.assertFalse(any(p.endswith("/check-runs") for p in paths))

    def test_publication_reads_never_use_the_fixture_token(self):
        seen = self.install()
        self.c.verify_exact_publications(HEAD_SHA)
        for _method, path, token in seen:
            if str(COMMENT_ID) in path or str(CHECK_ID) in path:
                self.assertEqual(token, "ghs_installation")

    def test_a_comment_on_another_pull_request_is_refused(self):
        self.install(comment_issue=99)
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)

    def test_a_comment_owned_by_another_app_is_refused(self):
        self.install(comment_app=999)
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)

    def test_a_check_on_another_head_sha_is_refused(self):
        self.install(check_head="c" * 40)
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)

    def test_an_incomplete_check_is_refused(self):
        self.install(check_status="in_progress")
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)

    def test_a_check_owned_by_another_app_is_refused(self):
        self.install(check_app=999)
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)

    def test_a_missing_comment_is_refused(self):
        self.install(comment_status=404)
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)

    def test_the_wrong_app_slug_is_refused(self):
        def fake_gh(method, path, token, body=None, bearer=True):
            if path == "/app":
                return 200, {"slug": "relium-pilot", "id": 1}
            return 200, {}

        self.c.gh = fake_gh
        self.c.app_jwt = lambda: "jwt"
        with self.assertRaises(StageFailure):
            self.c.verify_exact_publications(HEAD_SHA)


class ChecksumTests(CertifierTestCase):
    def make(self, body=b"-- dump\n", recorded=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name)
        (source / self.c.DUMP_NAME).write_bytes(body)
        digest = recorded or hashlib.sha256(body).hexdigest()
        (source / self.c.CHECKSUM_NAME).write_text(
            f"{digest}  /home/runner/work/{self.c.DUMP_NAME}\n", encoding="utf-8")
        return source

    def test_a_matching_checksum_passes(self):
        proof = self.c.verify_dump_checksum(self.make())
        self.assertTrue(proof["matches_recorded_checksum"])

    def test_a_tampered_dump_is_refused(self):
        source = self.make()
        (source / self.c.DUMP_NAME).write_bytes(b"-- tampered\n")
        with self.assertRaises(StageFailure):
            self.c.verify_dump_checksum(source)

    def test_a_missing_dump_is_refused(self):
        source = self.make()
        (source / self.c.DUMP_NAME).unlink()
        with self.assertRaises(StageFailure):
            self.c.verify_dump_checksum(source)


class ReadOnlyContractTests(unittest.TestCase):
    """The certifier must not be able to mutate a product resource."""

    def setUp(self):
        self.source = (REPO_ROOT / "scripts" / "e2e"
                       / "certify_phase_a.py").read_text(encoding="utf-8")

    def test_no_write_verb_reaches_github(self):
        for verb in ('"POST"', '"PATCH"', '"PUT"', '"DELETE"'):
            self.assertNotIn(f"gh({verb}", self.source,
                             f"a {verb} call would mutate a product resource")

    def test_the_app_webhook_is_never_touched(self):
        self.assertNotIn("/app/hook", self.source)

    def test_no_fixture_pull_request_machinery(self):
        for fragment in ("create_fixture_pr", "/pulls", "git/refs",
                         "open_pull", "make_branch"):
            self.assertNotIn(fragment, self.source)

    def test_no_snapshot_ingest_or_recomputation(self):
        for fragment in ("metadata-snapshots", "recompute_review",
                         "submit_metadata_snapshot"):
            self.assertNotIn(fragment, self.source)

    def test_the_fixture_token_is_never_read(self):
        self.assertNotIn("RELIUM_E2E_FIXTURE_TOKEN", self.source)
        self.assertNotIn("FIXTURE_TOKEN", self.source)


class WorkflowJobTests(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / ".github" / "workflows"
                     / "governance-e2e.yml").read_text(encoding="utf-8")
        self.job = self.text[self.text.index("  certify-phase-a:"):
                             self.text.index("  webhook-recovery:")]

    def test_the_operation_is_selectable_and_gated(self):
        self.assertIn("certify-phase-a", self.text)
        self.assertIn("if: inputs.operation == 'certify-phase-a'", self.job)

    def test_only_app_secrets_are_wired(self):
        for secret in ("RELIUM_E2E_APP_ID", "RELIUM_E2E_PRIVATE_KEY",
                       "RELIUM_E2E_INSTALLATION_ID"):
            self.assertIn(f"secrets.{secret}", self.job)
        # The name may appear in a comment explaining why it is absent; what
        # must never appear is the wiring itself.
        self.assertNotIn("secrets.RELIUM_E2E_FIXTURE_TOKEN", self.job)
        self.assertNotIn("RELIUM_E2E_FIXTURE_TOKEN: ", self.job)

    def test_it_requests_only_read_permissions(self):
        self.assertIn("contents: read", self.job)
        self.assertIn("actions: read", self.job)
        self.assertNotIn("write", self.job.split("permissions:")[1].split("services:")[0])

    def test_the_key_is_written_outside_the_workspace_and_removed(self):
        self.assertIn("$RUNNER_TEMP/relium-secrets/app.pem", self.job)
        self.assertIn("Remove the key material", self.job)

    def test_the_certifier_is_proven_before_it_reads(self):
        self.assertLess(self.job.index("test_certify_phase_a"),
                        self.job.index("certify_phase_a.py \"$EVIDENCE_DIR\""))

    def test_the_phase_b_artifact_is_uploaded_after_a_secret_scan(self):
        self.assertIn("secret_scan.py", self.job)
        self.assertIn("phase_b_secret_scan.outcome == 'success'", self.job)
        self.assertIn("integrated-product-phase-b-input", self.job)


if __name__ == "__main__":
    unittest.main()
