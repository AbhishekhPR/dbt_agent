"""Real local A -> B proof for the production metadata snapshot comparison.

Nothing here is simulated. It runs against a real PostgreSQL server, through:

  - the real collector ingest route  POST /api/metadata-snapshots
  - the real snapshot storage path   PostgresLifecycleStore.submit_metadata_snapshot
  - the real review lifecycle        validate_and_bind_snapshot / recompute_review
  - the real dashboard API           GET /api/reviews/{review_id}/attempts

The controlled change between the two observations is:

    snapshot A: row_count = 1000, null_rate = 0.01, column exists = true
    snapshot B: row_count =  800, null_rate = 0.12, column exists = false

Two columns carry it, because one column cannot: a column the collector could
not find in the catalogue is reported with no metrics at all (see
agent/collector/warehouse.py), so "this column vanished AND its null rate rose
to 12%" is not a state production can be in. `customer_id` disappears;
`email` stays and its null rate rises. Both signals are exercised, and neither
is faked.

and the proof asserts that A stays byte-for-byte what it was, that the
comparison binds exactly A -> B, and that the API returns the expected
deterministic evidence with nothing else attached.

Usage:
    RELIUM_TEST_POSTGRES_DSN=... python scripts/production_metadata_comparison_proof.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")
if not DSN:
    sys.exit("RELIUM_TEST_POSTGRES_DSN is required; this proof refuses to fake a store")

ORG, REPO, ENV = "relium-proof", "analytics", "production"
T0 = datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)

checks: list[tuple[str, bool, str]] = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition), detail))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


def snapshot_body(*, observed_at, row_count, null_rate, customer_id_exists,
                  distinct_emails, review_id, idempotency_key):
    """Exactly the payload the collector posts, no more."""
    return {
        "organization_id": ORG,
        "repository_id": REPO,
        "environment": ENV,
        "idempotency_key": idempotency_key,
        "review_id": review_id,
        "observed_at": observed_at.isoformat(),
        "collected_at": observed_at.isoformat(),
        "completeness": "COMPLETE",
        "freshness_state": "CURRENT",
        "relations": [{
            "relation_name": "orders",
            "relation_database": "warehouse",
            "relation_schema": "analytics",
            "model_unique_id": "model.jaffle.orders",
            "relation_type": "table",
            "exists_in_production": True,
            "collection_status": "COLLECTED",
            "schema_fingerprint": "sha-orders-v1",
            "row_count": row_count,
            "freshness_lag_seconds": 300,
            "columns": [
                # A column the collector cannot find is reported as absent
                # with no metrics — exactly what agent/collector/warehouse.py
                # emits. Inventing a null rate for it would be inventing a
                # measurement of something that is not there.
                {
                    "column_name": "customer_id",
                    "exists": customer_id_exists,
                    "collection_status": "COLLECTED",
                    "data_type": "BIGINT" if customer_id_exists else None,
                } if not customer_id_exists else {
                    "column_name": "customer_id",
                    "exists": True,
                    "collection_status": "COLLECTED",
                    "data_type": "BIGINT",
                    "is_nullable": True,
                    "distinct_count": 400,
                },
                {
                    "column_name": "email",
                    "exists": True,
                    "collection_status": "COLLECTED",
                    "data_type": "VARCHAR",
                    "is_nullable": True,
                    "null_rate": null_rate,
                    "null_count": int(row_count * null_rate),
                    "distinct_count": distinct_emails,
                    # The ratio the collector actually computes:
                    # distinct_count / row_count. A: 370/1000 = 0.37, which is
                    # exactly the value BIGINT storage used to round to 0.
                    "cardinality": distinct_emails / row_count,
                },
            ],
        }],
        # A bounded scalar metric, so the immutability check on
        # snapshot_metrics has an actual row to refuse. An UPDATE that matches
        # zero rows fires no row-level trigger and succeeds trivially, which
        # would make that check prove nothing.
        "metrics": [{
            "metric_name": "orders_total",
            "relation_name": "orders",
            "model_unique_id": "model.jaffle.orders",
            "metric_value": float(row_count),
        }],
    }


def main():
    import psycopg
    from starlette.testclient import TestClient

    from agent.api.auth import generate_token, hash_secret
    from agent.api.pool import StorePool
    from agent.api.session_crypto import generate_key, load_key
    from agent.api.sessions import SessionManager
    from agent.github_app.http_app import create_http_app
    from agent.metadata_evidence.recompute import recompute_review
    from agent.metadata_evidence.review_lifecycle import validate_and_bind_snapshot
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    print("\n== 0. clean database, migrations 0001-0012 via the real migrator ==")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
    with pool.acquire() as store:
        versions = [r["version"] for r in store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    check("migrations 0001-0012 applied", versions == list(range(1, 13)), str(versions))

    app = create_http_app(
        webhook_secret="proof-webhook-secret", job_queue=_StubQueue(),
        max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
        clock=lambda: 0.0, store_pool=pool,
        session_manager=SessionManager(
            client_id="proof", client_secret="proof",
            encryption_key=load_key(generate_key()), identity=None),
        cors_allowed_origins=("https://app.relium.test",),
    )
    client = TestClient(app)
    client.__enter__()

    def issue(scope):
        token_id, secret, presented = generate_token()
        with pool.acquire() as store:
            store.ensure_tenant(ORG, REPO, ENV)
            store.create_service_token(token_id, hash_secret(secret), ORG, REPO,
                                       environment=ENV, description="proof",
                                       scope=scope)
        return {"Authorization": f"Bearer {presented}"}

    collector_auth = issue("collector")
    read_auth = issue("operator_read")

    print("\n== 1. a real review, waiting for production metadata ==")
    review_id = "rev-proof-1"
    with pool.acquire() as store:
        store.upsert_pr_review(
            ORG, REPO, ENV, review_id=review_id, pull_number=4211,
            base_sha="a" * 40, head_sha="b" * 40,
            base_manifest_hash="bh", head_manifest_hash="hh",
            enforcement_mode="enforce", policy_version="v1", policy_hash="ph",
            metadata_required=True, payload={"plan": {"targets": []}})
        store.record_review_decision(
            ORG, REPO, review_id, decision=None, evidence_coverage="UNKNOWN",
            health=100, attempt=1, trigger="initial")
        waiting = store.connection.execute(
            "SELECT metadata_comparison FROM review_attempts WHERE review_id=%s "
            "AND attempt=1", (review_id,)).fetchone()
    check("an attempt with no snapshot stores SQL NULL, not a clean result",
          waiting["metadata_comparison"] is None)

    print("\n== 2. snapshot A through the real collector ingest route ==")
    response = client.post("/api/metadata-snapshots", headers=collector_auth,
                           json=snapshot_body(
                               observed_at=T0, row_count=1000, null_rate=0.01,
                               customer_id_exists=True, distinct_emails=370,
                               review_id=review_id, idempotency_key="proof-a"))
    # 202: the ingest route accepts evidence and returns the snapshot identity.
    check("POST /api/metadata-snapshots accepted A",
          response.status_code == 202, f"HTTP {response.status_code}")
    snapshot_a_id = response.json()["snapshot_id"]
    print(f"       snapshot A = {snapshot_a_id}")

    with pool.acquire() as store:
        validate_and_bind_snapshot(
            store, organization_id=ORG, repository_id=REPO, environment=ENV,
            review_id=review_id,
            snapshot=store.get_snapshot(ORG, REPO, snapshot_a_id))
        first = recompute_review(store, organization_id=ORG, repository_id=REPO,
                                 environment=ENV, review_id=review_id)
    check("A is the first observation, so the comparison reports no_baseline",
          first["metadata_comparison"]["status"] == "no_baseline",
          first["metadata_comparison"]["status"])
    check("no_baseline invents no before-values",
          first["metadata_comparison"]["changes"] == []
          and first["metadata_comparison"]["baseline_snapshot_id"] is None)

    print("\n== 3. snapshot A is immutable, header AND observations ==")
    where = "WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s"
    mutations = [
        ("parent UPDATE", f"UPDATE metadata_snapshots SET completeness='FAILED' {where}"),
        ("parent DELETE", f"DELETE FROM metadata_snapshots {where}"),
        ("relation UPDATE", f"UPDATE snapshot_relations SET row_count=1 {where}"),
        ("relation DELETE", f"DELETE FROM snapshot_relations {where}"),
        ("column UPDATE", f"UPDATE snapshot_columns SET null_rate=0.99 {where}"),
        ("column DELETE", f"DELETE FROM snapshot_columns {where}"),
        ("metric UPDATE", f"UPDATE snapshot_metrics SET metric_value=0 {where}"),
        ("metric DELETE", f"DELETE FROM snapshot_metrics {where}"),
    ]
    with pool.acquire() as store:
        frozen = store.get_snapshot(ORG, REPO, snapshot_a_id)
        for label, sql in mutations:
            try:
                store.connection.execute(sql, (ORG, REPO, snapshot_a_id))
                rejected, why = False, "the database ACCEPTED it"
            except Exception as exc:
                rejected, why = "immutable" in str(exc), "rejected as immutable"
                store.connection.rollback()
            check(f"A: direct {label} is rejected", rejected, why)
        check("A is byte-for-byte unchanged after every refused mutation",
              store.get_snapshot(ORG, REPO, snapshot_a_id) == frozen)

    email_cardinality = next(c["cardinality"] for c in frozen["relations"][0]["columns"]
                             if c["column_name"] == "email")
    check("A persisted a fractional cardinality, not a rounded 0",
          abs(email_cardinality - 0.37) < 1e-9, str(email_cardinality))

    print("\n== 4. snapshot B: the controlled change ==")
    response = client.post("/api/metadata-snapshots", headers=collector_auth,
                           json=snapshot_body(
                               observed_at=T0 + timedelta(hours=6), row_count=800,
                               null_rate=0.12, customer_id_exists=False,
                               distinct_emails=336, review_id=review_id,
                               idempotency_key="proof-b"))
    check("POST /api/metadata-snapshots accepted B",
          response.status_code == 202, f"HTTP {response.status_code}")
    snapshot_b_id = response.json()["snapshot_id"]
    print(f"       snapshot B = {snapshot_b_id}")

    with pool.acquire() as store:
        validate_and_bind_snapshot(
            store, organization_id=ORG, repository_id=REPO, environment=ENV,
            review_id=review_id,
            snapshot=store.get_snapshot(ORG, REPO, snapshot_b_id))
        second = recompute_review(store, organization_id=ORG, repository_id=REPO,
                                  environment=ENV, review_id=review_id)
        after = store.get_snapshot(ORG, REPO, snapshot_a_id)

    check("snapshot A is unchanged after B arrived and was compared",
          after == frozen)
    check("A still reports row_count 1000",
          after["relations"][0]["row_count"] == 1000,
          str(after["relations"][0]["row_count"]))

    comparison = second["metadata_comparison"]
    check("the comparison binds exactly A -> B",
          comparison["baseline_snapshot_id"] == snapshot_a_id
          and comparison["current_snapshot_id"] == snapshot_b_id)
    check("status is evaluated", comparison["status"] == "evaluated",
          comparison["status"])

    by_kind = {c["kind"]: c for c in comparison["changes"]}
    check("row_count 1000 -> 800 with -200 absolute and -0.2 relative",
          by_kind.get("row_count_changed", {}).get("before") == 1000
          and by_kind["row_count_changed"]["after"] == 800
          and by_kind["row_count_changed"]["absolute_delta"] == -200
          and by_kind["row_count_changed"]["relative_delta"] == -0.2)
    check("null_rate 0.01 -> 0.12 as +11.0 PERCENTAGE POINTS",
          by_kind.get("null_rate_changed", {}).get("percentage_point_delta") == 11.0,
          str(by_kind.get("null_rate_changed", {}).get("percentage_point_delta")))
    check("customer_id present -> missing",
          by_kind.get("column_availability_changed", {}).get("before") is True
          and by_kind["column_availability_changed"]["after"] is False
          and by_kind["column_availability_changed"]["column"] == "customer_id")
    check("a vanished column reports no invented metrics",
          not [c for c in comparison["changes"]
               if c["column"] == "customer_id" and c["signal"] != "column_exists"])
    check("cardinality 0.37 -> 0.42 as +5.0 PERCENTAGE POINTS, never 0",
          abs(by_kind.get("cardinality_changed", {}).get("before", 0) - 0.37) < 1e-9
          and abs(by_kind["cardinality_changed"]["after"] - 0.42) < 1e-9
          and abs(by_kind["cardinality_changed"]["percentage_point_delta"] - 5.0) < 1e-6,
          str(by_kind.get("cardinality_changed", {}).get("percentage_point_delta")))
    check("cardinality is a rate, so it carries no count-shaped delta",
          "absolute_delta" not in by_kind.get("cardinality_changed", {}))
    check("distinct_count keeps count semantics alongside it",
          by_kind.get("distinct_count_changed", {}).get("absolute_delta") == -34,
          str(by_kind.get("distinct_count_changed", {}).get("absolute_delta")))
    check("no severity, verdict or threshold appears in the evidence",
          not ({"severity", "decision", "verdict", "threshold", "finding"}
               & set().union(*[set(c) for c in comparison["changes"]])))

    print("\n== 5. idempotent recomputation does not move the baseline ==")
    with pool.acquire() as store:
        repeat = recompute_review(store, organization_id=ORG, repository_id=REPO,
                                  environment=ENV, review_id=review_id)
        stored = store.connection.execute(
            "SELECT metadata_comparison FROM review_attempts WHERE review_id=%s "
            "AND attempt=%s", (review_id, second["attempt"])).fetchone()
    check("a repeat recomputation produces no new attempt",
          repeat["status"] == "already_recomputed", repeat["status"])
    check("the stored evidence still names A -> B",
          stored["metadata_comparison"]["baseline_snapshot_id"] == snapshot_a_id
          and stored["metadata_comparison"]["current_snapshot_id"] == snapshot_b_id)

    print("\n== 6. through the real dashboard API ==")
    response = client.get(f"/api/reviews/{review_id}/attempts", headers=read_auth)
    check("GET /api/reviews/{id}/attempts", response.status_code == 200,
          f"HTTP {response.status_code}")
    payload = response.json()
    attempts = {a["attempt"]: a for a in payload["attempts"]}

    check("attempt 1 (waiting) exposes null, not an empty comparison",
          attempts[1]["metadata_comparison"] is None)
    api_first = attempts[first["attempt"]]["metadata_comparison"]
    check("the first recomputed attempt still reports no_baseline over the API",
          api_first["status"] == "no_baseline")
    api_view = attempts[second["attempt"]]["metadata_comparison"]
    check("the API preserves the A -> B binding",
          api_view["baseline_snapshot_id"] == snapshot_a_id
          and api_view["current_snapshot_id"] == snapshot_b_id)
    check("the API exposes exactly the allowlisted top-level fields",
          set(api_view) == {"status", "baseline_snapshot_id", "current_snapshot_id",
                            "baseline_observed_at", "current_observed_at",
                            "changes", "change_count", "coverage"},
          str(sorted(api_view)))

    blob = json.dumps(payload)
    leaks = [term for term in ("evidence_hash", "idempotency_key", "payload_hash",
                               "provenance", "collector_id", "collector_version",
                               "min_value", "max_value", "adapter_type",
                               "Bearer", "postgresql://")
             if term in blob]
    check("no raw snapshot internals, credentials or SQL in the response",
          not leaks, ", ".join(leaks) or "clean")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "production-metadata-comparison-proof.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"snapshot_a": snapshot_a_id, "snapshot_b": snapshot_b_id,
                   "attempts": payload["attempts"]}, handle, indent=2)
    print(f"\n  API response written to {os.path.normpath(out)}")

    client.__exit__(None, None, None)
    pool.close()

    failed = [name for name, ok, _ in checks if not ok]
    print("\n" + "=" * 62)
    print(f"  {len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    print("  REAL LOCAL A -> B PROOF: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
