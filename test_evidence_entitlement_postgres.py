"""Migration 0019: the database must accept NOT ENTITLED, and still refuse junk.

The Free lifecycle writes ``production_metadata = NOT ENTITLED`` on every
review of a workspace whose plan excludes warehouse evidence. That value was
outside the CHECK constraint 0004 declared, so without this migration the very
first Free review would fail on INSERT rather than on anything a reader could
diagnose.

Widening a CHECK is only safe if it stays a CHECK, so the second half of this
file proves the constraint still rejects a value nobody defined. Both are
properties of PostgreSQL and cannot be shown against a fake, so this suite is
database-gated like the other lifecycle suites.
"""
from __future__ import annotations

import os
import unittest

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG, REPO, ENV = "entitlement-org", "entitlement-repo", "production"
REVIEW = "review-not-entitled"
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; the constraint is a database property")
class EvidenceEntitlementMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.store = PostgresLifecycleStore(DSN)
        cls.store.ensure_tenant(ORG, REPO, ENV)
        cls.store.upsert_pr_review(
            ORG, REPO, ENV, review_id=REVIEW, pull_number=1,
            base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash="base", head_manifest_hash="head",
            enforcement_mode="enforce", policy_version="default-v1",
            policy_hash="policy", metadata_required=False,
            payload={"plan": {"targets": []}})

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_the_migration_is_applied_after_the_one_that_created_the_check(self):
        versions = [r["version"] for r in self.store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertIn(4, versions)
        self.assertIn(19, versions)
        self.assertLess(versions.index(4), versions.index(19))

    def test_a_not_entitled_evidence_row_is_accepted(self):
        rows = self.store.record_evidence_states(ORG, REPO, REVIEW, 1, {
            "base_manifest": ("required", "EVALUATED", "base_code", None),
            "head_manifest": ("required", "EVALUATED", "head_code", None),
            "production_metadata": (
                "optional", "NOT ENTITLED", "production",
                "Production warehouse evidence is not included on this "
                "workspace's plan."),
        })
        by_source = {r["evidence_source"]: r for r in rows}
        production = by_source["production_metadata"]
        self.assertEqual(production["state"], "NOT ENTITLED")
        self.assertEqual(production["requirement"], "optional")
        self.assertEqual(production["evidence_state_group"], "production")
        self.assertIn("not included on this workspace's plan",
                      production["detail"])

    def test_it_survives_a_read_back_through_the_store(self):
        self.store.record_evidence_states(ORG, REPO, REVIEW, 2, {
            "production_metadata": ("optional", "NOT ENTITLED", "production", None),
        })
        rows = self.store.evidence_states(ORG, REPO, REVIEW, 2)
        self.assertEqual([r["state"] for r in rows], ["NOT ENTITLED"])

    def test_the_states_the_constraint_already_allowed_still_pass(self):
        """Widening must not have replaced the vocabulary with a narrower one."""
        for state in ("EVALUATED", "MISSING", "FAILED", "NOT EVALUATED",
                      "UNSUPPORTED", "STALE", "PENDING",
                      "BLOCKED BY CREDENTIALS", "NOT ENTITLED"):
            with self.subTest(state=state):
                rows = self.store.record_evidence_states(ORG, REPO, REVIEW, 3, {
                    "production_metadata": ("optional", state, "production", None),
                })
                self.assertEqual(rows[0]["state"], state)

    def test_an_undefined_state_is_still_rejected(self):
        """It is still a CHECK, not a comment."""
        import psycopg

        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.record_evidence_states(ORG, REPO, REVIEW, 4, {
                "production_metadata": ("optional", "PROBABLY FINE", "production",
                                        None),
            })


if __name__ == "__main__":
    unittest.main()
