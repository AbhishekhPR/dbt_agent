"""Real-PostgreSQL tests for the metadata evidence plane.

These require an actual PostgreSQL server reachable via
RELIUM_TEST_POSTGRES_DSN. They are skipped (not failed) when that variable is
unset so `python -m unittest discover` still works on a machine without
PostgreSQL; CI always sets it against a real postgres service container. No
test here may substitute SQLite, an in-memory store or a mock connection for
the real adapter.

Tenant isolation is exercised with DELIBERATELY SHARED identifiers - two
tenants using the same review id and the same snapshot id - because that is
the case that a previous release shipped broken.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG_A, REPO_A = "org-a", "repo-a"
ORG_B, REPO_B = "org-b", "repo-b"
ENV = "production"

# Shared on purpose: cross-tenant separation must not depend on unique ids.
SHARED_REVIEW = "review-shared-1"
SHARED_SNAPSHOT = "snapshot-shared-1"

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
BASE_MANIFEST_HASH = "basehash"
HEAD_MANIFEST_HASH = "headhash"


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _now():
    return datetime.now(timezone.utc)


def _relation(name="analytics.orders", **overrides):
    relation = {
        "relation_name": name,
        "relation_schema": "analytics",
        "relation_type": "table",
        "schema_fingerprint": "fp-1",
        "row_count": 1000,
        "columns": [
            {"column_name": "order_id", "data_type": "bigint", "is_nullable": False,
             "null_rate": 0.0, "distinct_count": 1000},
            {"column_name": "discount_amount", "data_type": "numeric",
             "is_nullable": True, "null_rate": 0.02},
        ],
    }
    relation.update(overrides)
    return relation


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class MetadataEvidencePlaneTests(unittest.TestCase):
    """One schema reset per test: leakage between tests would mask the very
    isolation properties these tests exist to prove."""

    def setUp(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        self.store = PostgresLifecycleStore(DSN)
        self.addCleanup(self.store.close)
        for org, repo in ((ORG_A, REPO_A), (ORG_B, REPO_B)):
            self.store.ensure_tenant(org, repo, ENV)

    # -- helpers ---------------------------------------------------------

    def _review(self, org=ORG_A, repo=REPO_A, review_id=SHARED_REVIEW, **kwargs):
        params = dict(
            review_id=review_id, pull_number=7, base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash=BASE_MANIFEST_HASH, head_manifest_hash=HEAD_MANIFEST_HASH,
            enforcement_mode="enforce", policy_version="default-v1", policy_hash="ph",
        )
        params.update(kwargs)
        return self.store.upsert_pr_review(org, repo, ENV, **params)

    def _request(self, org=ORG_A, repo=REPO_A, request_id="req-1", review_id=SHARED_REVIEW,
                 targets=None, **kwargs):
        params = dict(
            request_id=request_id, review_id=review_id, reason="pr_review",
            expires_at=_now() + timedelta(minutes=30),
            targets=targets if targets is not None else [
                {"relation_name": "analytics.orders", "columns": ["discount_amount"],
                 "required_signals": ["schema", "null_rate"], "dependency_kind": "external"},
            ],
            base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash=BASE_MANIFEST_HASH, head_manifest_hash=HEAD_MANIFEST_HASH,
        )
        params.update(kwargs)
        return self.store.create_collection_request(org, repo, ENV, **params)

    def _snapshot(self, org=ORG_A, repo=REPO_A, snapshot_id=SHARED_SNAPSHOT,
                  idempotency_key="idem-1", payload_hash="ph-1", **kwargs):
        params = dict(
            snapshot_id=snapshot_id, idempotency_key=idempotency_key,
            payload_hash=payload_hash, evidence_hash="ev-1",
            observed_at=_now(), collected_at=_now(),
            relations=[_relation()],
            review_id=SHARED_REVIEW, base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash=BASE_MANIFEST_HASH, head_manifest_hash=HEAD_MANIFEST_HASH,
        )
        params.update(kwargs)
        return self.store.submit_metadata_snapshot(org, repo, ENV, **params)

    # -- 1. immutability -------------------------------------------------

    def test_snapshot_persists_immutably(self):
        self._review()
        snapshot, created = self._snapshot()
        self.assertTrue(created)
        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT)
        self.assertEqual(stored["snapshot_id"], SHARED_SNAPSHOT)
        self.assertEqual(len(stored["relations"]), 1)
        self.assertEqual(len(stored["relations"][0]["columns"]), 2)

        # The database itself must refuse mutation, not merely the store API.
        import psycopg
        with self.assertRaises(psycopg.errors.RestrictViolation):
            self.store.connection.execute(
                "UPDATE metadata_snapshots SET evidence_hash='tampered' "
                "WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s",
                (ORG_A, REPO_A, SHARED_SNAPSHOT),
            )

    def test_column_absence_survives_the_round_trip(self):
        """A column the collector could not find must still be absent on read.

        The evidence plane had nowhere to record column-level existence, so an
        absent column was stored as a present one with unknown metrics and the
        decision engine reported nothing. Persistence is the layer that has to
        hold this fact; asserting it here stops the regression at the store.
        """
        self._review()
        relation = _relation()
        relation["columns"] = [
            {"column_name": "order_id", "data_type": "bigint",
             "exists_in_production": True, "null_rate": 0.0},
            {"column_name": "discount_amount", "data_type": None,
             "exists_in_production": False},
        ]
        self._snapshot(relations=[relation])

        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT)
        by_name = {c["column_name"]: c
                   for c in stored["relations"][0]["columns"]}
        self.assertTrue(by_name["order_id"]["exists_in_production"])
        self.assertFalse(by_name["discount_amount"]["exists_in_production"])

    def test_columns_default_to_existing_when_the_flag_is_absent(self):
        """Evidence written before the flag existed must not become absent."""
        self._review()
        self._snapshot()
        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT)
        for column in stored["relations"][0]["columns"]:
            self.assertTrue(column["exists_in_production"], column["column_name"])

    def test_snapshot_delete_is_rejected(self):
        self._review()
        self._snapshot()
        import psycopg
        with self.assertRaises(psycopg.errors.RestrictViolation):
            self.store.connection.execute(
                "DELETE FROM metadata_snapshots WHERE organization_id=%s "
                "AND repository_id=%s AND snapshot_id=%s",
                (ORG_A, REPO_A, SHARED_SNAPSHOT),
            )

    # -- 2/3. idempotency and conflicting replay -------------------------

    def test_duplicate_identical_snapshot_is_idempotent(self):
        self._review()
        first, created_first = self._snapshot()
        second, created_second = self._snapshot()
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        count = self.store.connection.execute(
            "SELECT count(*) AS n FROM metadata_snapshots WHERE organization_id=%s",
            (ORG_A,),
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_conflicting_replay_is_rejected(self):
        self._review()
        self._snapshot()
        with self.assertRaises(ValueError):
            self._snapshot(snapshot_id="snapshot-other", payload_hash="DIFFERENT")

    # -- 4/5/6. cross-tenant binding is inexpressible --------------------

    def test_snapshot_cannot_bind_to_another_tenants_review(self):
        """Tenant B owns a review with the SAME id as tenant A's review.
        Tenant A's snapshot must not be bindable to it."""
        self._review(org=ORG_A, repo=REPO_A)
        self._review(org=ORG_B, repo=REPO_B)
        self._snapshot(org=ORG_A, repo=REPO_A)

        import psycopg
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.store.bind_snapshot_to_review(
                ORG_B, REPO_B, review_id=SHARED_REVIEW, snapshot_id=SHARED_SNAPSHOT,
                binding_state="ACCEPTED",
            )

    def test_snapshot_is_not_visible_to_another_tenant(self):
        self._review(org=ORG_A, repo=REPO_A)
        self._snapshot(org=ORG_A, repo=REPO_A)
        self.assertIsNone(self.store.get_snapshot(ORG_B, REPO_B, SHARED_SNAPSHOT))

    def test_same_snapshot_id_in_two_tenants_stays_separate(self):
        self._review(org=ORG_A, repo=REPO_A)
        self._review(org=ORG_B, repo=REPO_B)
        self._snapshot(org=ORG_A, repo=REPO_A, evidence_hash="tenant-a")
        self._snapshot(org=ORG_B, repo=REPO_B, evidence_hash="tenant-b")
        a = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT, expand=False)
        b = self.store.get_snapshot(ORG_B, REPO_B, SHARED_SNAPSHOT, expand=False)
        self.assertEqual(a["evidence_hash"], "tenant-a")
        self.assertEqual(b["evidence_hash"], "tenant-b")

    def test_idempotency_key_is_scoped_per_tenant(self):
        """The same idempotency key in two tenants is two distinct snapshots,
        not a collision."""
        self._review(org=ORG_A, repo=REPO_A)
        self._review(org=ORG_B, repo=REPO_B)
        _, created_a = self._snapshot(org=ORG_A, repo=REPO_A, idempotency_key="shared-key")
        _, created_b = self._snapshot(org=ORG_B, repo=REPO_B, idempotency_key="shared-key")
        self.assertTrue(created_a)
        self.assertTrue(created_b)

    # -- 7/8. SHA and manifest binding ------------------------------------

    def test_binding_records_sha_and_manifest_mismatch(self):
        """A snapshot collected against a different head SHA must be storable
        as a REJECTED binding with the reason preserved - rejection is
        auditable, not silent."""
        self._review()
        self._snapshot(head_sha="c" * 40)
        binding = self.store.bind_snapshot_to_review(
            ORG_A, REPO_A, review_id=SHARED_REVIEW, snapshot_id=SHARED_SNAPSHOT,
            binding_state="REJECTED", rejection_reason="head_sha mismatch",
            base_sha_match=True, head_sha_match=False, manifest_hash_match=True,
        )
        self.assertEqual(binding["binding_state"], "REJECTED")
        self.assertFalse(binding["head_sha_match"])
        self.assertEqual(
            self.store.review_bindings(ORG_A, REPO_A, SHARED_REVIEW, state="ACCEPTED"), [])
        self.assertIsNone(self.store.latest_accepted_snapshot(ORG_A, REPO_A, SHARED_REVIEW))

    # -- 9/10. freshness and completeness ---------------------------------

    def test_stale_snapshot_is_classified_stale(self):
        self._review()
        self._snapshot(freshness_state="STALE",
                       observed_at=_now() - timedelta(hours=4),
                       ttl_seconds=900)
        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT, expand=False)
        self.assertEqual(stored["freshness_state"], "STALE")
        self.assertNotEqual(stored["freshness_state"], "CURRENT")

    def test_partial_snapshot_is_not_complete(self):
        self._review()
        self._snapshot(completeness="PARTIAL", relations=[
            _relation(collection_status="PARTIAL",
                      unevaluated_checks=[{"check": "duplicate_rate",
                                           "reason": "not supported by adapter"}]),
        ])
        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT)
        self.assertEqual(stored["completeness"], "PARTIAL")
        self.assertEqual(stored["relations"][0]["collection_status"], "PARTIAL")
        self.assertTrue(stored["relations"][0]["unevaluated_checks"])

    def test_unsupported_signal_is_recorded_as_unsupported(self):
        self._review()
        self._snapshot(relations=[_relation(collection_status="UNSUPPORTED")])
        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT)
        self.assertEqual(stored["relations"][0]["collection_status"], "UNSUPPORTED")

    # -- review lifecycle -------------------------------------------------

    def test_review_persists_with_no_decision(self):
        review = self._review()
        self.assertIsNone(review["decision"])
        self.assertEqual(review["lifecycle_state"], "RECEIVED")

    def test_lifecycle_state_is_independent_of_decision(self):
        self._review()
        self.store.transition_review(ORG_A, REPO_A, SHARED_REVIEW,
                                     "WAITING_FOR_METADATA", reason="metadata requested")
        review = self.store.get_review(ORG_A, REPO_A, SHARED_REVIEW)
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_METADATA")
        self.assertIsNone(review["decision"])

    def test_transition_is_idempotent_and_logged(self):
        self._review()
        first = self.store.transition_review(ORG_A, REPO_A, SHARED_REVIEW,
                                             "CODE_ANALYSIS_COMPLETE")
        repeat = self.store.transition_review(ORG_A, REPO_A, SHARED_REVIEW,
                                              "CODE_ANALYSIS_COMPLETE")
        self.assertTrue(first["transition_applied"])
        self.assertFalse(repeat["transition_applied"])
        states = [t["to_state"] for t in
                  self.store.review_transitions(ORG_A, REPO_A, SHARED_REVIEW)]
        self.assertEqual(states, ["RECEIVED", "CODE_ANALYSIS_COMPLETE"])

    def test_unknown_lifecycle_state_is_rejected(self):
        self._review()
        with self.assertRaises(ValueError):
            self.store.transition_review(ORG_A, REPO_A, SHARED_REVIEW, "NOT_A_STATE")

    def test_attempts_are_preserved_across_recomputation(self):
        self._review()
        self.store.record_review_decision(
            ORG_A, REPO_A, SHARED_REVIEW, decision="WARN", evidence_coverage="INCOMPLETE",
            health=100, attempt=1, trigger="initial")
        self.store.record_review_decision(
            ORG_A, REPO_A, SHARED_REVIEW, decision="ALLOW", evidence_coverage="COMPLETE",
            health=100, attempt=2, trigger="metadata_snapshot",
            snapshot_id=SHARED_SNAPSHOT)
        attempts = self.store.review_attempts(ORG_A, REPO_A, SHARED_REVIEW)
        self.assertEqual([a["attempt"] for a in attempts], [1, 2])
        self.assertEqual([a["decision"] for a in attempts], ["WARN", "ALLOW"])
        # the earlier attempt is preserved verbatim, not overwritten
        self.assertEqual(attempts[0]["evidence_coverage"], "INCOMPLETE")
        self.assertEqual(attempts[0]["trigger"], "initial")

    def test_coverage_health_and_decision_are_stored_separately(self):
        self._review()
        self.store.record_review_decision(
            ORG_A, REPO_A, SHARED_REVIEW, decision="WARN",
            evidence_coverage="INCOMPLETE", health=100, attempt=1)
        review = self.store.get_review(ORG_A, REPO_A, SHARED_REVIEW)
        self.assertEqual(review["decision"], "WARN")
        self.assertEqual(review["evidence_coverage"], "INCOMPLETE")
        self.assertEqual(review["health"], 100)

    def test_evidence_states_record_the_three_product_states(self):
        self._review()
        self.store.record_evidence_states(ORG_A, REPO_A, SHARED_REVIEW, 1, {
            "base_manifest": ("required", "EVALUATED", "base_code", None),
            "head_manifest": ("required", "EVALUATED", "head_code", None),
            "production_metadata": ("required", "MISSING", "production",
                                    "collector did not respond"),
        })
        states = {r["evidence_source"]: r for r in
                  self.store.evidence_states(ORG_A, REPO_A, SHARED_REVIEW, 1)}
        self.assertEqual(states["production_metadata"]["state"], "MISSING")
        self.assertEqual(states["production_metadata"]["evidence_state_group"], "production")
        self.assertEqual(states["base_manifest"]["evidence_state_group"], "base_code")

    def test_sticky_publication_identity_is_remembered(self):
        self._review()
        self.store.record_review_publication(
            ORG_A, REPO_A, SHARED_REVIEW, comment_id=555, check_run_id=777)
        # a later call must not clobber a known id with None
        self.store.record_review_publication(ORG_A, REPO_A, SHARED_REVIEW, comment_id=None)
        review = self.store.get_review(ORG_A, REPO_A, SHARED_REVIEW)
        self.assertEqual(review["github_comment_id"], "555")
        self.assertEqual(review["github_check_run_id"], "777")

    # -- collection requests ----------------------------------------------

    def test_collection_request_is_bounded_to_named_targets(self):
        self._review()
        request = self._request()
        self.assertEqual(len(request["targets"]), 1)
        self.assertEqual(request["targets"][0]["relation_name"], "analytics.orders")
        self.assertEqual(request["state"], "PENDING")

    def test_collection_request_requires_at_least_one_target(self):
        """A request naming nothing would invite a full warehouse scan."""
        self._review()
        with self.assertRaises(ValueError):
            self._request(targets=[])

    def test_head_derived_target_is_marked_distinctly(self):
        """A column produced by the head graph inside this PR is not an
        external production dependency and must be distinguishable."""
        self._review()
        request = self._request(targets=[
            {"relation_name": "analytics.orders", "dependency_kind": "external"},
            {"relation_name": "analytics.fct_orders", "dependency_kind": "head_derived"},
        ])
        kinds = {t["relation_name"]: t["dependency_kind"] for t in request["targets"]}
        self.assertEqual(kinds["analytics.orders"], "external")
        self.assertEqual(kinds["analytics.fct_orders"], "head_derived")

    def test_expired_request_is_not_offered_to_a_collector(self):
        self._review()
        self._request(request_id="req-expired",
                      expires_at=_now() - timedelta(minutes=1))
        pending = self.store.pending_collection_requests(ORG_A, REPO_A, environment=ENV)
        self.assertEqual([p["request_id"] for p in pending], [])
        stored = self.store.get_collection_request(ORG_A, REPO_A, "req-expired")
        self.assertEqual(stored["state"], "EXPIRED")

    def test_expired_request_cannot_be_acknowledged(self):
        self._review()
        self._request(request_id="req-expired",
                      expires_at=_now() - timedelta(minutes=1))
        self.assertIsNone(self.store.acknowledge_collection_request(
            ORG_A, REPO_A, "req-expired", collector_id="c1"))

    def test_collector_cannot_see_another_tenants_requests(self):
        self._review(org=ORG_A, repo=REPO_A)
        self._request(org=ORG_A, repo=REPO_A, request_id="req-a")
        pending_b = self.store.pending_collection_requests(ORG_B, REPO_B, environment=ENV)
        self.assertEqual(pending_b, [])

    def test_requests_are_prioritised_critical_first(self):
        self._review()
        self._request(request_id="req-standard", priority="standard")
        self._request(request_id="req-critical", priority="critical")
        pending = self.store.pending_collection_requests(ORG_A, REPO_A, environment=ENV)
        self.assertEqual(pending[0]["request_id"], "req-critical")

    def test_acknowledge_then_close_request(self):
        self._review()
        self._request(request_id="req-1")
        acknowledged = self.store.acknowledge_collection_request(
            ORG_A, REPO_A, "req-1", collector_id="collector-1")
        self.assertEqual(acknowledged["state"], "ACKNOWLEDGED")
        closed = self.store.close_collection_request(
            ORG_A, REPO_A, "req-1", state="COMPLETED")
        self.assertEqual(closed["state"], "COMPLETED")

    # -- collector identity -----------------------------------------------

    def test_collector_registration_and_revocation(self):
        collector = self.store.register_collector(
            ORG_A, REPO_A, ENV, collector_id="collector-1",
            collector_version="0.1.0", adapter_type="postgres")
        self.assertFalse(collector["revoked"])
        revoked = self.store.revoke_collector(
            ORG_A, REPO_A, "collector-1", reason="rotated")
        self.assertTrue(revoked["revoked"])
        self.assertIsNotNone(revoked["revoked_at"])

    def test_collector_is_tenant_scoped(self):
        self.store.register_collector(ORG_A, REPO_A, ENV, collector_id="collector-1")
        self.assertIsNone(self.store.get_collector(ORG_B, REPO_B, "collector-1"))

    # -- durable recomputation --------------------------------------------

    def test_snapshot_arrival_enqueues_recomputation(self):
        self._review()
        self._snapshot()
        job = self.store.enqueue_review_recomputation(
            ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW)
        self.assertIsNotNone(job)
        self.assertEqual(job["subject_type"], "review")
        self.assertEqual(job["subject_id"], SHARED_REVIEW)
        self.assertEqual(job["state"], "PENDING")

    def test_duplicate_snapshot_causes_one_recomputation(self):
        self._review()
        self._snapshot()
        self._snapshot()  # idempotent replay
        self.store.enqueue_review_recomputation(ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW)
        self.store.enqueue_review_recomputation(ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW)
        jobs = self.store.review_recomputation_jobs(ORG_A, REPO_A, review_id=SHARED_REVIEW)
        self.assertEqual(len(jobs), 1)

    def test_new_evidence_enqueues_another_recomputation(self):
        """A review whose production state moves must be recomputed again.

        Uniqueness keyed on the review alone froze a decision at its first
        snapshot: the outbox row stayed COMPLETED and every later enqueue was
        discarded, so a warehouse that degraded after the first collection was
        never re-decided. Exactly-once has to mean per unit of evidence.
        """
        self._review()
        claimed = []
        for snapshot_id in ("snap-a", "snap-b", "snap-c"):
            self.store.enqueue_review_recomputation(
                ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW,
                event_type="metadata.review_recompute_requested",
                payload={"snapshot_id": snapshot_id}, dedup_key=snapshot_id)
            event = self.store.claim_outbox(ORG_A, REPO_A, ENV, "worker-under-test")
            self.assertIsNotNone(event, f"{snapshot_id} produced no job")
            claimed.append(event["payload"]["snapshot_id"])
            self.store.complete_outbox(ORG_A, REPO_A, event["event_id"])

        self.assertEqual(claimed, ["snap-a", "snap-b", "snap-c"])

    def test_redelivered_evidence_still_causes_one_recomputation(self):
        """The duplicate-suppression guarantee must survive the fix."""
        self._review()
        for _ in range(3):
            self.store.enqueue_review_recomputation(
                ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW,
                event_type="metadata.review_recompute_requested",
                payload={"snapshot_id": "snap-same"}, dedup_key="snap-same")

        jobs = [j for j in self.store.review_recomputation_jobs(
            ORG_A, REPO_A, review_id=SHARED_REVIEW)
            if j["event_type"] == "metadata.review_recompute_requested"]
        self.assertEqual(len(jobs), 1)

    def test_binding_a_second_snapshot_enqueues_its_own_recomputation(self):
        """The real submission path, not a direct enqueue."""
        from agent.metadata_evidence.review_lifecycle import validate_and_bind_snapshot

        self._review()
        seen = []
        for index, snapshot_id in enumerate(("snap-first", "snap-second")):
            self._snapshot(snapshot_id=snapshot_id,
                           idempotency_key=f"idem-{index}",
                           payload_hash=f"ph-{index}")
            stored = self.store.get_snapshot(ORG_A, REPO_A, snapshot_id)
            validate_and_bind_snapshot(
                self.store, organization_id=ORG_A, repository_id=REPO_A,
                environment=ENV, review_id=SHARED_REVIEW, snapshot=stored)
            event = self.store.claim_outbox(ORG_A, REPO_A, ENV, "worker-under-test")
            self.assertIsNotNone(event, f"{snapshot_id} produced no job")
            seen.append(event["payload"]["snapshot_id"])
            self.store.complete_outbox(ORG_A, REPO_A, event["event_id"])

        self.assertEqual(seen, ["snap-first", "snap-second"])

    def test_recomputation_job_is_claimable_by_the_worker(self):
        """The review job must be claimable through the SAME outbox claim path
        the deployment jobs use - one durable queue, not two."""
        self._review()
        self.store.enqueue_review_recomputation(ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW)
        claimed = self.store.claim_outbox(ORG_A, REPO_A, ENV, "worker-1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["subject_type"], "review")
        self.assertEqual(claimed["subject_id"], SHARED_REVIEW)

    def test_review_recomputation_is_tenant_scoped(self):
        self._review(org=ORG_A, repo=REPO_A)
        self._review(org=ORG_B, repo=REPO_B)
        self.store.enqueue_review_recomputation(ORG_A, REPO_A, ENV, review_id=SHARED_REVIEW)
        self.assertEqual(
            self.store.review_recomputation_jobs(ORG_B, REPO_B, review_id=SHARED_REVIEW), [])

    def test_deployment_outbox_still_works_after_generalisation(self):
        """The subject generalisation must not regress the deployment queue."""
        self.store.create_deployment(ORG_A, REPO_A, ENV, {
            "deployment_id": "dep-1", "models": ["fct_orders"]})
        jobs = self.store.connection.execute(
            "SELECT subject_type, subject_id, deployment_id FROM outbox_events "
            "WHERE organization_id=%s AND repository_id=%s", (ORG_A, REPO_A)).fetchall()
        self.assertTrue(jobs)
        self.assertEqual(jobs[0]["subject_type"], "deployment")
        self.assertEqual(jobs[0]["subject_id"], "dep-1")
        self.assertEqual(jobs[0]["deployment_id"], "dep-1")

    # -- privacy -----------------------------------------------------------

    def test_no_raw_rows_or_unbounded_text_persist(self):
        self._review()
        oversized = "x" * 5000
        self._snapshot(relations=[_relation(columns=[
            {"column_name": "note", "data_type": "text", "max_value": oversized},
        ])])
        stored = self.store.get_snapshot(ORG_A, REPO_A, SHARED_SNAPSHOT)
        value = stored["relations"][0]["columns"][0]["max_value"]
        self.assertLessEqual(len(value), 256)
        self.assertTrue(value.endswith("..."))

    def test_metric_text_is_bounded_by_a_database_constraint(self):
        """Even a direct write must not be able to store an unbounded blob."""
        self._review()
        self._snapshot()
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.connection.execute(
                "INSERT INTO snapshot_metrics (organization_id, repository_id, snapshot_id, "
                "metric_index, metric_name, metric_text) VALUES (%s,%s,%s,%s,%s,%s)",
                (ORG_A, REPO_A, SHARED_SNAPSHOT, 99, "leak", "y" * 5000),
            )


if __name__ == "__main__":
    unittest.main()
