"""Migrations 0028 and 0029 over a database that already holds evidence.

The interesting case is not a fresh schema, it is an upgrade: rows written by
the previous release are already there, the table is immutable by trigger, and
canonicalisation is Python so nothing can be backfilled in SQL. This walks the
real upgrade -- migrate to 0027, write evidence the way the previous release
wrote it, then apply the two new migrations -- and asserts that the commit is
usable afterwards, that the stored row is untouched, and that the widened
review-state CHECK still refuses a state nobody defined.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

MIGRATIONS = Path("agent/migrations/postgres")
MIGRATION = MIGRATIONS / "0028_manifest_evidence_semantic_identity.sql"
CONFLICT_MIGRATION = MIGRATIONS / "0029_review_manifest_conflict_state.sql"
DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "migration-org"
REPO = "migration-repo"
SHA = "0f34be6d424c6e6078cf3b9fdc546a57b9813d27"


def _manifest(*, generated_at, invocation_id, created_at):
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.8.0",
            "project_name": "relium",
            "generated_at": generated_at,
            "invocation_id": invocation_id,
        },
        "nodes": {
            "model.relium.fct_revenue": {
                "unique_id": "model.relium.fct_revenue",
                "resource_type": "model",
                "name": "fct_revenue",
                "schema": "public",
                "raw_code": "select 1 as revenue",
                "created_at": created_at,
            },
        },
        "sources": {},
    }


LEGACY_MANIFEST = _manifest(generated_at="2026-08-18T10:00:00Z",
                            invocation_id="run-1", created_at=1756720000.0)
RECOMPILED_MANIFEST = _manifest(generated_at="2026-09-18T09:15:00Z",
                                invocation_id="run-2", created_at=1758181234.0)


class MigrationContractTests(unittest.TestCase):
    """What the file itself promises, without a database."""

    def setUp(self):
        self.sql = MIGRATION.read_text(encoding="utf-8")

    def test_it_adds_both_columns_idempotently(self):
        self.assertIn("ADD COLUMN IF NOT EXISTS semantic_manifest_hash TEXT",
                      self.sql)
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS canonicalization_version INTEGER "
            "NOT NULL DEFAULT 1", self.sql)

    def test_it_creates_no_table_and_drops_nothing(self):
        self.assertNotIn("CREATE TABLE", self.sql.upper())
        self.assertNotIn("DROP TABLE", self.sql.upper())
        self.assertNotIn("DROP COLUMN", self.sql.upper())

    def test_it_does_not_rewrite_or_unprotect_existing_evidence(self):
        """No backfill, and no hole punched in the immutability trigger.

        An UPDATE over this table would have to disable the trigger that makes
        evidence append-only. The store re-derives a legacy row's identity
        from the manifest it already holds instead.
        """
        upper = self.sql.upper()
        self.assertNotIn("UPDATE MANIFEST_EVIDENCE", upper)
        self.assertNotIn("DISABLE TRIGGER", upper)
        self.assertNotIn("DROP TRIGGER", upper)
        self.assertNotIn("TRG_MANIFEST_EVIDENCE_IMMUTABLE", upper)

    def test_the_semantic_hash_stays_nullable(self):
        """A rolling deploy still has the previous release writing rows."""
        self.assertNotIn("semantic_manifest_hash TEXT NOT NULL", self.sql)


#: Every review lifecycle state that existed before 0029. Pinned as a literal
#: so that removing one from the CHECK fails here rather than silently
#: invalidating rows that already hold it.
PRE_EXISTING_REVIEW_STATES = (
    "RECEIVED", "WAITING_FOR_MANIFEST", "CODE_ANALYSIS_COMPLETE",
    "METADATA_NOT_REQUIRED", "METADATA_REQUESTED", "WAITING_FOR_METADATA",
    "METADATA_PARTIAL", "METADATA_COMPLETE", "METADATA_STALE",
    "DECISION_READY", "PUBLISHED", "FAILED",
)


class ConflictStateMigrationContractTests(unittest.TestCase):
    """Migration 0029: one more review lifecycle state, and only that."""

    def setUp(self):
        self.sql = CONFLICT_MIGRATION.read_text(encoding="utf-8")

    def test_it_adds_the_conflict_state(self):
        self.assertIn("'MANIFEST_CONFLICT'", self.sql)

    def test_it_keeps_every_state_that_already_existed(self):
        """Additive. An existing row must not become invalid."""
        for state in PRE_EXISTING_REVIEW_STATES:
            self.assertIn(f"'{state}'", self.sql, state)

    def test_it_touches_no_table_and_no_row(self):
        upper = self.sql.upper()
        self.assertNotIn("CREATE TABLE", upper)
        self.assertNotIn("DROP TABLE", upper)
        self.assertNotIn("DROP COLUMN", upper)
        self.assertNotIn("UPDATE REVIEWS", upper)
        self.assertNotIn("DELETE FROM", upper)

    def test_the_application_and_the_database_agree_on_the_state_set(self):
        """The CHECK and REVIEW_LIFECYCLE_STATES must not drift."""
        import re

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        in_sql = set(re.findall(r"'([A-Z_]+)'", self.sql))
        self.assertEqual(in_sql, set(PostgresLifecycleStore.REVIEW_LIFECYCLE_STATES))

    def test_the_handoff_names_the_same_state(self):
        from agent.metadata_evidence.manifest_handoff import CONFLICT_STATE
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.assertEqual(CONFLICT_STATE, "MANIFEST_CONFLICT")
        self.assertIn(CONFLICT_STATE,
                      PostgresLifecycleStore.REVIEW_LIFECYCLE_STATES)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; a migration is a database property")
class MigrationOverExistingEvidenceTests(unittest.TestCase):
    """The upgrade path, in order."""

    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        from agent import postgres_migrate

        self.Jsonb = Jsonb
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        # Everything the PREVIOUS release had, and nothing more.
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, staging, True)
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "0028_":
                shutil.copy(path, Path(staging) / path.name)
        staged = {p.name for p in Path(staging).glob("*.sql")}
        self.assertNotIn("0028_manifest_evidence_semantic_identity.sql", staged)
        self.assertNotIn("0029_review_manifest_conflict_state.sql", staged)

        self.connection = psycopg.connect(DSN, autocommit=True,
                                          row_factory=dict_row)
        self.addCleanup(self.connection.close)

        original = postgres_migrate.MIGRATIONS_DIR
        postgres_migrate.MIGRATIONS_DIR = Path(staging)
        try:
            postgres_migrate.apply_migrations(self.connection)
        finally:
            postgres_migrate.MIGRATIONS_DIR = original

        self.connection.execute(
            "INSERT INTO organizations (organization_id) VALUES (%s)", (ORG,))
        self.connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) "
            "VALUES (%s, %s)", (ORG, REPO))

    def _columns(self):
        return {row["column_name"] for row in self.connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='manifest_evidence'").fetchall()}

    def _write_legacy_evidence(self):
        """Exactly the INSERT the previous release issued."""
        self.connection.execute(
            "INSERT INTO manifest_evidence (organization_id, repository_id, "
            "evidence_id, commit_sha, manifest_hash, manifest, "
            "idempotency_key, payload_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (ORG, REPO, "manifest-legacy-0001", SHA, "a" * 64,
             self.Jsonb(LEGACY_MANIFEST),
             f"github-actions:1355704784:{SHA}", "b" * 64))

    def _apply_0028(self):
        """Apply everything the previous release was missing (0028 and 0029)."""
        from agent import postgres_migrate

        return postgres_migrate.apply_migrations(self.connection)

    def test_the_previous_release_schema_has_neither_column(self):
        self.assertNotIn("semantic_manifest_hash", self._columns())
        self.assertNotIn("canonicalization_version", self._columns())

    def test_the_migration_applies_over_existing_evidence(self):
        self._write_legacy_evidence()
        applied = self._apply_0028()

        self.assertIn(28, applied)
        self.assertIn(29, applied)
        self.assertIn("semantic_manifest_hash", self._columns())
        self.assertIn("canonicalization_version", self._columns())

    def test_existing_evidence_survives_byte_for_byte(self):
        self._write_legacy_evidence()
        self._apply_0028()

        row = self.connection.execute(
            "SELECT * FROM manifest_evidence WHERE commit_sha=%s",
            (SHA,)).fetchone()
        self.assertEqual(row["evidence_id"], "manifest-legacy-0001")
        self.assertEqual(row["manifest"], LEGACY_MANIFEST)
        self.assertEqual(row["manifest_hash"], "a" * 64)
        self.assertIsNone(row["semantic_manifest_hash"])
        self.assertEqual(row["canonicalization_version"], 1)

    def test_the_evidence_table_is_still_immutable_afterwards(self):
        self._write_legacy_evidence()
        self._apply_0028()

        with self.assertRaises(Exception) as caught:
            self.connection.execute(
                "UPDATE manifest_evidence SET manifest_hash=%s WHERE commit_sha=%s",
                ("c" * 64, SHA))
        self.assertIn("immutable", str(caught.exception))

    def test_the_legacy_commit_becomes_usable_again(self):
        """The customer-visible outcome.

        The base SHA from the failing run, its legacy evidence still in place,
        recompiled by a later CI run under the same idempotency key. Before the
        fix this was the 409.
        """
        self._write_legacy_evidence()
        self._apply_0028()

        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        from agent.metadata_evidence.collection_plan import manifest_hash

        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.connection.close)
        row, created = store.submit_manifest_evidence(
            ORG, REPO, commit_sha=SHA, manifest=RECOMPILED_MANIFEST,
            manifest_hash=manifest_hash(RECOMPILED_MANIFEST),
            idempotency_key=f"github-actions:1355704784:{SHA}",
            payload_hash="d" * 64)

        self.assertFalse(created)
        self.assertEqual(row["evidence_id"], "manifest-legacy-0001")

    def test_a_genuine_change_on_the_legacy_commit_is_still_refused(self):
        self._write_legacy_evidence()
        self._apply_0028()

        from agent.postgres_lifecycle_store import (
            ManifestEvidenceConflict, PostgresLifecycleStore,
        )
        from agent.metadata_evidence.collection_plan import manifest_hash

        changed = dict(RECOMPILED_MANIFEST)
        changed["nodes"] = {"model.relium.fct_revenue": dict(
            RECOMPILED_MANIFEST["nodes"]["model.relium.fct_revenue"],
            raw_code="select 2 as revenue")}

        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.connection.close)
        with self.assertRaises(ManifestEvidenceConflict):
            store.submit_manifest_evidence(
                ORG, REPO, commit_sha=SHA, manifest=changed,
                manifest_hash=manifest_hash(changed),
                idempotency_key=f"github-actions:1355704784:{SHA}",
                payload_hash="e" * 64)

    def test_applying_the_migrations_twice_is_a_no_op(self):
        self._write_legacy_evidence()
        self._apply_0028()
        self.assertEqual(self._apply_0028(), [])

    def test_the_conflict_state_is_accepted_and_the_old_ones_still_are(self):
        """The CHECK after the upgrade, exercised rather than read."""
        self._apply_0028()
        self.connection.execute(
            "INSERT INTO environments (organization_id, repository_id, "
            "environment, connected) VALUES (%s, %s, %s, TRUE) "
            "ON CONFLICT DO NOTHING", (ORG, REPO, "production"))

        for index, state in enumerate(
                ("MANIFEST_CONFLICT", "WAITING_FOR_MANIFEST", "PUBLISHED")):
            with self.subTest(state=state):
                self.connection.execute(
                    "INSERT INTO reviews (review_id, organization_id, "
                    "repository_id, environment, evidence_coverage, "
                    "lifecycle_state) VALUES (%s, %s, %s, %s, 'UNKNOWN', %s)",
                    (f"gh-state-{index}", ORG, REPO, "production", state))
                row = self.connection.execute(
                    "SELECT lifecycle_state FROM reviews WHERE review_id=%s",
                    (f"gh-state-{index}",)).fetchone()
                self.assertEqual(row["lifecycle_state"], state)

    def test_an_unknown_lifecycle_state_is_still_refused(self):
        """The CHECK was widened by exactly one value, not removed."""
        self._apply_0028()
        self.connection.execute(
            "INSERT INTO environments (organization_id, repository_id, "
            "environment, connected) VALUES (%s, %s, %s, TRUE) "
            "ON CONFLICT DO NOTHING", (ORG, REPO, "production"))

        with self.assertRaises(Exception) as caught:
            self.connection.execute(
                "INSERT INTO reviews (review_id, organization_id, "
                "repository_id, environment, evidence_coverage, "
                "lifecycle_state) VALUES (%s, %s, %s, %s, 'UNKNOWN', %s)",
                ("gh-state-bogus", ORG, REPO, "production", "NOT_A_STATE"))
        self.assertIn("reviews_lifecycle_state_check", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
