"""An in-memory lifecycle store for the review-lifecycle regression suites.

Why this exists
---------------
The authoritative lifecycle suites run against real PostgreSQL, because the
properties they assert -- tenant-scoped SQL, ON CONFLICT idempotence, CHECK
constraints -- are properties of the database and no fake can prove them.

The properties asserted HERE are different. They are properties of the
lifecycle's own control flow:

    does a workspace that cannot supply warehouse evidence still get a
    decision, or does it wait forever for a delivery that can never arrive

That is decided entirely in ``begin_review``, ``build_collection_plan`` and
``evaluate_metadata_decision``, and it must be provable on any machine -- the
bug it covers shipped to production, and a regression test that silently skips
wherever PostgreSQL is absent is how it would ship again.

So this implements exactly the surface those functions touch, faithfully:
``upsert_pr_review`` really is create-or-return, transitions really are
recorded in order, and a request really is keyed by id. It deliberately
implements nothing else, so it cannot drift into a second implementation of
the store.
"""
from __future__ import annotations

from datetime import datetime, timezone


class InMemoryLifecycleStore:
    """The subset of ``PostgresLifecycleStore`` the review lifecycle calls."""

    def __init__(self, *, billing=None, tenants=None):
        #: tenant_id -> the billing row a Polar webhook would have written.
        self.billing = dict(billing or {})
        #: (organization_id, repository_id) -> tenant_id
        self.tenants = dict(tenants or {})
        self.reviews = {}
        self.transitions = []
        self.collection_requests = {}
        self.decisions = []
        self.evidence_states = {}
        self.audit = []
        self.manifest_evidence = {}
        self.outbox = []
        self.snapshots = {}

    # -- tenancy -----------------------------------------------------------

    def ensure_tenant(self, organization_id, repository_id, environment):
        return {"organization_id": organization_id,
                "repository_id": repository_id, "environment": environment}

    def tenant_for_repository_slug(self, organization_id, repository_id):
        return self.tenants.get((organization_id, repository_id))

    def billing_for_tenant(self, tenant_id):
        return self.billing.get(tenant_id)

    # -- reviews -----------------------------------------------------------

    def upsert_pr_review(self, organization_id, repository_id, environment, *,
                         review_id, pull_number=None, base_sha=None, head_sha=None,
                         base_manifest_hash=None, head_manifest_hash=None,
                         enforcement_mode=None, policy_version=None,
                         policy_hash=None, github_delivery_id=None,
                         metadata_required=False, lifecycle_state="RECEIVED",
                         payload=None):
        key = (organization_id, repository_id, review_id)
        existing = self.reviews.get(key)
        if existing is None:
            self.reviews[key] = {
                "review_id": review_id, "organization_id": organization_id,
                "repository_id": repository_id, "environment": environment,
                "pull_number": pull_number, "commit_sha": head_sha,
                "decision": None, "enforcement_mode": enforcement_mode,
                "evidence_coverage": "UNKNOWN", "lifecycle_state": lifecycle_state,
                "base_sha": base_sha, "head_sha": head_sha,
                "base_manifest_hash": base_manifest_hash,
                "head_manifest_hash": head_manifest_hash,
                "policy_version": policy_version, "policy_hash": policy_hash,
                "github_delivery_id": github_delivery_id,
                "metadata_required": bool(metadata_required),
                "payload": dict(payload or {}), "attempt": 1, "health": None,
            }
            self.transitions.append((review_id, None, lifecycle_state,
                                     "review received"))
        elif (existing["lifecycle_state"] == "WAITING_FOR_MANIFEST"
                and lifecycle_state != "WAITING_FOR_MANIFEST"):
            # The same fill-in the real store performs once CI delivers the
            # exact artifact: the SAME review gains its analysis binding.
            existing.update({
                "base_manifest_hash": base_manifest_hash,
                "head_manifest_hash": head_manifest_hash,
                "enforcement_mode": enforcement_mode,
                "policy_version": policy_version, "policy_hash": policy_hash,
                "metadata_required": bool(metadata_required),
                "payload": dict(payload or {}),
            })
        return dict(self.reviews[key])

    def get_review(self, organization_id, repository_id, review_id):
        review = self.reviews.get((organization_id, repository_id, review_id))
        return dict(review) if review else None

    def transition_review(self, organization_id, repository_id, review_id,
                          state, reason=None):
        review = self.reviews[(organization_id, repository_id, review_id)]
        self.transitions.append((review_id, review["lifecycle_state"], state, reason))
        review["lifecycle_state"] = state
        return dict(review)

    def record_review_decision(self, organization_id, repository_id, review_id, *,
                               decision, evidence_coverage, health, attempt,
                               trigger, enforcement_mode=None, snapshot_id=None,
                               policy_version=None, policy_hash=None, payload=None,
                               semantic_evidence=None, metadata_comparison=None):
        review = self.reviews[(organization_id, repository_id, review_id)]
        review.update({"decision": decision, "evidence_coverage": evidence_coverage,
                       "health": health, "attempt": attempt})
        row = {"review_id": review_id, "attempt": attempt, "decision": decision,
               "evidence_coverage": evidence_coverage, "health": health,
               "trigger": trigger, "snapshot_id": snapshot_id,
               "payload": dict(payload or {}),
               "semantic_evidence": semantic_evidence,
               "metadata_comparison": metadata_comparison}
        self.decisions.append(row)
        return dict(row)

    def review_attempts(self, organization_id, repository_id, review_id):
        return [dict(d) for d in self.decisions if d["review_id"] == review_id]

    def record_evidence_states(self, organization_id, repository_id, review_id,
                               attempt, rows):
        self.evidence_states[(review_id, attempt)] = dict(rows)
        return dict(rows)

    def record_review_publication(self, organization_id, repository_id, review_id,
                                  comment_id=None, check_run_id=None):
        review = self.reviews[(organization_id, repository_id, review_id)]
        review["comment_id"] = comment_id
        review["check_run_id"] = check_run_id
        return dict(review)

    # -- collection requests ----------------------------------------------

    def get_collection_request(self, organization_id, repository_id, request_id):
        request = self.collection_requests.get(request_id)
        return dict(request) if request else None

    def create_collection_request(self, organization_id, repository_id, environment, *,
                                  request_id, review_id, reason, expires_at, targets,
                                  base_sha=None, head_sha=None, base_manifest_hash=None,
                                  head_manifest_hash=None, priority="standard",
                                  required_evidence_level="profile", plan=None):
        row = {"request_id": request_id, "review_id": review_id, "reason": reason,
               "expires_at": expires_at, "targets": [dict(t) for t in targets],
               "state": "PENDING", "priority": priority,
               "required_evidence_level": required_evidence_level,
               "plan": dict(plan or {})}
        self.collection_requests[request_id] = row
        return dict(row)

    def requests_for_review(self, review_id):
        return [dict(r) for r in self.collection_requests.values()
                if r["review_id"] == review_id]

    # -- snapshots ---------------------------------------------------------

    def latest_accepted_snapshot(self, organization_id, repository_id, review_id):
        return self.snapshots.get(review_id)

    # -- manifest evidence -------------------------------------------------

    def submit_manifest_evidence(self, organization_id, repository_id, *, commit_sha,
                                 manifest, manifest_hash, idempotency_key,
                                 payload_hash):
        row = {"commit_sha": commit_sha, "manifest": manifest,
               "manifest_hash": manifest_hash}
        self.manifest_evidence[(organization_id, repository_id, commit_sha)] = row
        return dict(row)

    def get_manifest_evidence(self, organization_id, repository_id, commit_sha):
        row = self.manifest_evidence.get((organization_id, repository_id, commit_sha))
        return dict(row) if row else None

    # -- outbox ------------------------------------------------------------

    def enqueue_review_recomputation(self, organization_id, repository_id, environment,
                                     *, review_id, event_type, payload=None,
                                     dedup_key=None):
        row = {"review_id": review_id, "event_type": event_type,
               "payload": dict(payload or {}), "dedup_key": dedup_key}
        self.outbox.append(row)
        return dict(row)

    # -- audit -------------------------------------------------------------

    def append_audit(self, organization_id, repository_id, *, actor, event_type,
                     reference_type=None, reference_id=None, payload=None):
        self.audit.append({"actor": actor, "event_type": event_type,
                           "reference_id": reference_id,
                           "payload": dict(payload or {})})
        return self.audit[-1]


def active_billing_row(plan):
    """The billing row a signature-verified Polar webhook writes for a plan."""
    return {"plan": plan, "subscription_status": "active",
            "past_due_at": None,
            "updated_at": datetime.now(timezone.utc)}
