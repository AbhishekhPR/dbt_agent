"""Upgrade tests for migration 0004 over a populated 169708c schema.

Migration 0004 generalises the outbox from deployment-only to subject-based.
That is the riskiest part of this release: the outbox already carries live
work in several states, and a careless upgrade could strand a claimed job,
lose a dead letter, or silently reinterpret a deployment job as a review job.

These tests seed a 169708c-shaped database with outbox rows in EVERY state,
apply 0004, and assert nothing was lost or misreinterpreted.

They require a real PostgreSQL server via RELIUM_TEST_POSTGRES_DSN and are
skipped (not failed) when it is unset.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG, REPO, ENV = "org-upgrade", "repo-upgrade", "production"


def _all_migration_versions():
    """Every migration version on disk, ascending.

    Derived rather than hardcoded so adding a migration does not fail tests
    that are about upgrade BEHAVIOUR rather than about the migration count.
    """
    from agent.postgres_migrate import _migration_files, _version_of

    return sorted(_version_of(path) for path in _migration_files())


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


def _apply_through(conn, last_version):
    """Apply migrations up to and including ``last_version`` only.

    This reproduces the exact pre-upgrade schema shipped as 169708c so the
    upgrade is tested against reality rather than against a fresh database.
    """
    import hashlib

    from agent import postgres_migrate

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for path in postgres_migrate._migration_files():
        version = postgres_migrate._version_of(path)
        if version > last_version:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (version, hashlib.sha256(sql.encode()).hexdigest()),
            )


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class Migration0004UpgradeTests(unittest.TestCase):
    """Seed a 169708c database with live outbox work, then upgrade it."""

    def setUp(self):
        _reset_schema(DSN)
        self.conn = _connect()
        self.addCleanup(self.conn.close)
        _apply_through(self.conn, 3)

        # 169708c-shaped tenant and deployments
        self.conn.execute("INSERT INTO organizations (organization_id) VALUES (%s)", (ORG,))
        self.conn.execute(
            "INSERT INTO repositories (organization_id, repository_id) VALUES (%s, %s)",
            (ORG, REPO))
        self.conn.execute(
            "INSERT INTO environments (organization_id, repository_id, environment) "
            "VALUES (%s, %s, %s)", (ORG, REPO, ENV))

        now = datetime.now(timezone.utc)
        self.seeded = {
            # state, deployment id, attempts, lease owner, lease expiry, next attempt
            "pending": ("PENDING", "dep-pending", 0, None, None, now),
            "claimed": ("CLAIMED", "dep-claimed", 1, "worker-live",
                        now + timedelta(minutes=5), now),
            "retrying": ("PENDING", "dep-retry", 3, None, None,
                         now + timedelta(minutes=10)),
            "completed": ("COMPLETED", "dep-done", 1, None, None, now),
            "dead": ("DEAD_LETTER", "dep-dead", 5, None, None, now),
        }
        for key, (state, dep, attempts, owner, lease, nxt) in self.seeded.items():
            self.conn.execute(
                "INSERT INTO deployments (deployment_id, organization_id, repository_id, "
                "environment, status, payload) VALUES (%s,%s,%s,%s,'reviewed','{}'::jsonb)",
                (dep, ORG, REPO, ENV))
            self.conn.execute(
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, "
                "environment, deployment_id, event_type, payload, state, lease_owner, "
                "lease_expires_at, attempts, next_attempt_at, last_error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (f"evt-{key}", ORG, REPO, ENV, dep, "deployment.reviewed",
                 '{"seed": true}', state, owner, lease, attempts, nxt,
                 "boom" if state == "DEAD_LETTER" else None))

        # A dead letter row too, so its inspectability survives the upgrade.
        self.conn.execute(
            "INSERT INTO outbox_dead_letters (event_id, organization_id, repository_id, "
            "environment, deployment_id, event_type, payload, attempts, last_error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("evt-dead", ORG, REPO, ENV, "dep-dead", "deployment.reviewed",
             '{"seed": true}', 5, "boom"))

        self.before = self._snapshot_outbox()

    def _snapshot_outbox(self):
        rows = self.conn.execute(
            "SELECT event_id, state, attempts, lease_owner, lease_expires_at, "
            "next_attempt_at, deployment_id, event_type, last_error FROM outbox_events "
            "WHERE organization_id=%s ORDER BY event_id", (ORG,)).fetchall()
        return {r["event_id"]: dict(r) for r in rows}

    def _upgrade(self):
        from agent.postgres_migrate import apply_migrations
        return apply_migrations(self.conn)

    # -- the upgrade itself ------------------------------------------------

    def test_0004_applies_over_populated_169708c_schema(self):
        applied = self._upgrade()
        # The claim is that 0004 was pending on this schema and applied, not
        # that 0004 is the last migration that will ever exist. Pinning the
        # exact list made every later migration fail this test for no reason.
        self.assertIn(4, applied)
        versions = sorted(r["version"] for r in self.conn.execute(
            "SELECT version FROM schema_migrations").fetchall())
        self.assertEqual(versions, _all_migration_versions())

    def test_no_outbox_record_is_lost(self):
        self._upgrade()
        after = self._snapshot_outbox()
        self.assertEqual(set(self.before), set(after))
        self.assertEqual(len(after), 5)

    def test_state_attempts_and_leases_are_preserved(self):
        self._upgrade()
        after = self._snapshot_outbox()
        for event_id, before_row in self.before.items():
            with self.subTest(event=event_id):
                self.assertEqual(after[event_id]["state"], before_row["state"])
                self.assertEqual(after[event_id]["attempts"], before_row["attempts"])
                self.assertEqual(after[event_id]["lease_owner"], before_row["lease_owner"])
                self.assertEqual(after[event_id]["lease_expires_at"],
                                 before_row["lease_expires_at"])
                self.assertEqual(after[event_id]["next_attempt_at"],
                                 before_row["next_attempt_at"])

    def test_subject_columns_are_backfilled_correctly(self):
        self._upgrade()
        rows = self.conn.execute(
            "SELECT event_id, subject_type, subject_id, deployment_id FROM outbox_events "
            "WHERE organization_id=%s", (ORG,)).fetchall()
        for row in rows:
            with self.subTest(event=row["event_id"]):
                self.assertEqual(row["subject_type"], "deployment")
                self.assertEqual(row["subject_id"], row["deployment_id"])

    def test_no_deployment_job_becomes_a_review_job(self):
        self._upgrade()
        leaked = self.conn.execute(
            "SELECT count(*) AS n FROM review_recomputation_jobs WHERE organization_id=%s",
            (ORG,)).fetchone()["n"]
        self.assertEqual(leaked, 0)

    def test_dead_letters_remain_inspectable(self):
        self._upgrade()
        rows = self.conn.execute(
            "SELECT * FROM outbox_dead_letters WHERE organization_id=%s", (ORG,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 5)
        self.assertEqual(rows[0]["last_error"], "boom")
        dead = self.conn.execute(
            "SELECT state, last_error FROM outbox_events WHERE event_id='evt-dead'"
        ).fetchone()
        self.assertEqual(dead["state"], "DEAD_LETTER")

    def test_pending_deployment_job_remains_claimable(self):
        self._upgrade()
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.close)
        claimed = store.claim_outbox(ORG, REPO, ENV, "worker-after-upgrade")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["subject_type"], "deployment")
        # 'dep-pending' is the only immediately-claimable row: 'dep-retry' is
        # scheduled into the future and 'dep-claimed' holds a live lease.
        self.assertEqual(claimed["deployment_id"], "dep-pending")

    def test_retry_scheduled_job_is_not_claimed_early(self):
        self._upgrade()
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.close)
        seen = []
        while True:
            row = store.claim_outbox(ORG, REPO, ENV, "worker-drain")
            if row is None:
                break
            seen.append(row["deployment_id"])
        self.assertNotIn("dep-retry", seen)
        self.assertNotIn("dep-done", seen)
        self.assertNotIn("dep-dead", seen)

    def test_review_and_deployment_jobs_coexist(self):
        self._upgrade()
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.close)
        store.upsert_pr_review(ORG, REPO, ENV, review_id="rev-1", pull_number=1,
                               base_sha="a" * 40, head_sha="b" * 40)
        store.enqueue_review_recomputation(ORG, REPO, ENV, review_id="rev-1")

        counts = self.conn.execute(
            "SELECT subject_type, count(*) AS n FROM outbox_events "
            "WHERE organization_id=%s GROUP BY subject_type ORDER BY subject_type",
            (ORG,)).fetchall()
        counts = {r["subject_type"]: r["n"] for r in counts}
        self.assertEqual(counts["deployment"], 5)
        self.assertEqual(counts["review"], 1)

        review_jobs = self.conn.execute(
            "SELECT review_id FROM review_recomputation_jobs WHERE organization_id=%s",
            (ORG,)).fetchall()
        self.assertEqual([r["review_id"] for r in review_jobs], ["rev-1"])

    def test_unsupported_subject_type_is_rejected(self):
        """An unknown subject type must fail loudly rather than sit in the
        queue as work nothing can ever complete."""
        self._upgrade()
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, "
                "environment, subject_type, subject_id, event_type, payload) "
                "VALUES ('evt-bad',%s,%s,%s,'wormhole','x','some.event','{}'::jsonb)",
                (ORG, REPO, ENV))

    def test_deployment_subject_still_requires_a_deployment_id(self):
        self._upgrade()
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, "
                "environment, subject_type, subject_id, deployment_id, event_type, payload) "
                "VALUES ('evt-nodep',%s,%s,%s,'deployment','x',NULL,'some.event','{}'::jsonb)",
                (ORG, REPO, ENV))

    def test_migration_is_idempotent(self):
        self._upgrade()
        before = self._snapshot_outbox()
        from agent.postgres_migrate import apply_migrations

        again = apply_migrations(self.conn)
        self.assertEqual(again, [])
        self.assertEqual(self._snapshot_outbox(), before)

    def test_applies_from_an_empty_database(self):
        _reset_schema(DSN)
        conn = _connect()
        self.addCleanup(conn.close)
        from agent.postgres_migrate import apply_migrations

        # Every migration on disk applies, in ascending order, from empty.
        self.assertEqual(apply_migrations(conn), _all_migration_versions())
        n = conn.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='metadata_snapshots'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)
        collector_columns = {
            row["column_name"] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='collector_identities'"
            ).fetchall()
        }
        self.assertTrue(
            {"last_verified_at", "last_failed_at", "verification_status",
             "verification_error_category"}.issubset(collector_columns))


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class BackupRestoreAfterUpgradeTests(unittest.TestCase):
    """A dump taken after 0004 must restore with the evidence plane intact."""

    def test_backup_and_restore_round_trip(self):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        if not shutil.which("pg_dump") or not shutil.which("psql"):
            self.skipTest("pg_dump/psql not on PATH")

        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        store.ensure_tenant(ORG, REPO, ENV)
        store.upsert_pr_review(ORG, REPO, ENV, review_id="rev-dump", pull_number=3,
                               base_sha="a" * 40, head_sha="b" * 40,
                               base_manifest_hash="bh", head_manifest_hash="hh")
        store.create_collection_request(
            ORG, REPO, ENV, request_id="req-dump", review_id="rev-dump",
            reason="pr_review",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            targets=[{"relation_name": "analytics.orders"}])
        store.submit_metadata_snapshot(
            ORG, REPO, ENV, snapshot_id="snap-dump", idempotency_key="k-dump",
            payload_hash="p", evidence_hash="e",
            observed_at=datetime.now(timezone.utc),
            collected_at=datetime.now(timezone.utc),
            review_id="rev-dump",
            relations=[{"relation_name": "analytics.orders",
                        "columns": [{"column_name": "order_id"}]}])
        store.close()

        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "dump.sql"
            result = subprocess.run(["pg_dump", "--dbname", DSN, "--file", str(dump)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[:400])
            self.assertTrue(dump.stat().st_size > 0)

            _reset_schema(DSN)
            restore = subprocess.run(["psql", "--dbname", DSN, "--file", str(dump),
                                      "-v", "ON_ERROR_STOP=1"],
                                     capture_output=True, text=True)
            self.assertEqual(restore.returncode, 0, restore.stderr[:400])

        conn = _connect()
        self.addCleanup(conn.close)
        review = conn.execute(
            "SELECT * FROM reviews WHERE organization_id=%s AND review_id='rev-dump'",
            (ORG,)).fetchone()
        self.assertIsNotNone(review)
        self.assertEqual(review["head_sha"], "b" * 40)
        self.assertIsNone(review["decision"])
        snapshot = conn.execute(
            "SELECT * FROM metadata_snapshots WHERE organization_id=%s "
            "AND snapshot_id='snap-dump'", (ORG,)).fetchone()
        self.assertIsNotNone(snapshot)
        columns = conn.execute(
            "SELECT count(*) AS n FROM snapshot_columns WHERE organization_id=%s", (ORG,)
        ).fetchone()["n"]
        self.assertEqual(columns, 1)

        # Immutability must survive a restore, not just an initial migration.
        import psycopg
        with self.assertRaises(psycopg.errors.RestrictViolation):
            conn.execute("UPDATE metadata_snapshots SET evidence_hash='x' "
                         "WHERE organization_id=%s AND snapshot_id='snap-dump'", (ORG,))


if __name__ == "__main__":
    unittest.main()
