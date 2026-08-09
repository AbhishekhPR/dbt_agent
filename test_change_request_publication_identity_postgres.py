"""PostgreSQL enforcement for durable change-request publication identity."""
from __future__ import annotations

import hashlib
import os
import unittest

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")
ORG, REPO, ENV = "org-publication-id", "repo-publication-id", "production"
FAILURE_REASON = "publication identity missing; publication success cannot be verified"


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


def _reset_schema():
    with _connect() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _apply_through(conn, last_version):
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
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(sql.encode()).hexdigest()),
            )


def _seed_tenant_and_reviews(conn, *review_ids):
    conn.execute("INSERT INTO organizations (organization_id) VALUES (%s)", (ORG,))
    conn.execute(
        "INSERT INTO repositories (organization_id, repository_id) VALUES (%s, %s)",
        (ORG, REPO))
    conn.execute(
        "INSERT INTO environments (organization_id, repository_id, environment) "
        "VALUES (%s, %s, %s)", (ORG, REPO, ENV))
    for index, review_id in enumerate(review_ids, start=1):
        conn.execute(
            "INSERT INTO reviews (review_id, organization_id, repository_id, "
            "environment, pull_number, decision, payload) "
            "VALUES (%s, %s, %s, %s, %s, 'BLOCK', '{}'::jsonb)",
            (review_id, ORG, REPO, ENV, index))


def _insert_request(conn, request_id, review_id, *, state="PENDING", remote_id=None):
    conn.execute(
        "INSERT INTO review_change_requests (organization_id, repository_id, "
        "change_request_id, review_id, attempt, environment, pull_number, "
        "head_sha, actor, message, state, remote_review_id, published_at) "
        "VALUES (%s,%s,%s,%s,1,%s,1,%s,'reviewer','Fix it',%s,%s, "
        "CASE WHEN %s='PUBLISHED' THEN now() END)",
        (ORG, REPO, request_id, review_id, ENV, "a" * 40, state, remote_id, state))


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class Migration0008PublicationIdentityTests(unittest.TestCase):
    def setUp(self):
        _reset_schema()
        self.conn = _connect()
        self.addCleanup(self.conn.close)
        _apply_through(self.conn, 7)
        self.invalid_remote_ids = {
            "null": None,
            "spaces": "   ",
            "controls": "\t\n",
            "text": "review-7",
            "fullwidth": "１２３",
            "zeros": "000",
            "negative": "-1",
            "decimal": "1.5",
        }
        review_ids = [f"review-{label}" for label in self.invalid_remote_ids]
        _seed_tenant_and_reviews(self.conn, *review_ids, "review-valid-leading-zero")
        for label, remote_id in self.invalid_remote_ids.items():
            _insert_request(
                self.conn, f"request-{label}", f"review-{label}",
                state="PUBLISHED", remote_id=remote_id)
        _insert_request(
            self.conn, "request-valid-leading-zero", "review-valid-leading-zero",
            state="PUBLISHED", remote_id="007")

    def test_upgrade_marks_unverifiable_legacy_publications_failed(self):
        from agent.postgres_migrate import apply_migrations

        applied = apply_migrations(self.conn)

        self.assertIn(8, applied)
        rows = {
            row["change_request_id"]: dict(row)
            for row in self.conn.execute(
                "SELECT change_request_id, state, remote_review_id, failure_reason, "
                "published_at FROM review_change_requests").fetchall()
        }
        self.assertEqual(len(rows), len(self.invalid_remote_ids) + 1)
        for label in self.invalid_remote_ids:
            with self.subTest(remote_review_id=self.invalid_remote_ids[label]):
                row = rows[f"request-{label}"]
                self.assertEqual(row["state"], "FAILED")
                self.assertEqual(row["failure_reason"], FAILURE_REASON)
                self.assertIsNone(row["published_at"])

        valid = rows["request-valid-leading-zero"]
        self.assertEqual(valid["state"], "PUBLISHED")
        self.assertEqual(valid["remote_review_id"], "007")
        self.assertIsNotNone(valid["published_at"])


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class PublicationIdentityConstraintTests(unittest.TestCase):
    def setUp(self):
        _reset_schema()
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.addCleanup(self.store.close)
        self.store.ensure_tenant(ORG, REPO, ENV)
        self.store.upsert_pr_review(
            ORG, REPO, ENV, review_id="review-constraint", pull_number=10,
            base_sha="a" * 40, head_sha="b" * 40)

    def _create_request(self, request_id="request-constraint", attempt=1):
        return self.store.create_change_request(
            ORG, REPO, ENV, change_request_id=request_id,
            review_id="review-constraint", attempt=attempt, pull_number=10,
            head_sha="b" * 40, actor="reviewer", message="Fix it")

    def test_database_rejects_invalid_published_insert_and_update(self):
        import psycopg

        invalid_ids = (
            None, "", "   ", "\t\n", "review-7", "１２３", "000", "-1", "1.5"
        )
        for index, remote_id in enumerate(invalid_ids, start=1):
            with self.subTest(operation="insert", remote_review_id=remote_id):
                request_id = f"invalid-insert-{index}"
                try:
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        _insert_request(
                            self.store.connection, request_id,
                            "review-constraint", state="PUBLISHED",
                            remote_id=remote_id)
                finally:
                    self.store.connection.execute(
                        "DELETE FROM review_change_requests "
                        "WHERE organization_id=%s AND repository_id=%s "
                        "AND change_request_id=%s", (ORG, REPO, request_id))

        self._create_request()
        for remote_id in invalid_ids:
            with self.subTest(operation="update", remote_review_id=remote_id):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.store.connection.execute(
                        "UPDATE review_change_requests SET state='PUBLISHED', "
                        "remote_review_id=%s WHERE organization_id=%s "
                        "AND repository_id=%s AND change_request_id=%s",
                        (remote_id, ORG, REPO, "request-constraint"))

    def test_database_accepts_a_published_row_with_an_identity(self):
        _insert_request(
            self.store.connection, "valid-insert", "review-constraint",
            state="PUBLISHED", remote_id="007")

        row = self.store.get_change_request(ORG, REPO, "valid-insert")
        self.assertEqual(row["state"], "PUBLISHED")
        self.assertEqual(row["remote_review_id"], "007")

    def test_store_rejects_invalid_publication_identity_from_direct_callers(self):
        invalid_ids = (
            None, "", "   ", True, False, 0, -1, {}, [], "review-7", "１２３"
        )
        self._create_request()

        for invalid_id in invalid_ids:
            with self.subTest(remote_review_id=invalid_id):
                with self.assertRaises(ValueError):
                    self.store.complete_change_request(
                        ORG, REPO, "request-constraint",
                        remote_review_id=invalid_id)
                row = self.store.get_change_request(ORG, REPO, "request-constraint")
                self.assertEqual(row["state"], "PENDING")
                self.assertIsNone(row["remote_review_id"])

    def test_store_normalizes_valid_publication_identity_to_text(self):
        for index, remote_id in enumerate((7001, "007002"), start=1):
            with self.subTest(remote_review_id=remote_id):
                request_id = f"valid-store-{index}"
                self._create_request(request_id=request_id, attempt=index)

                row = self.store.complete_change_request(
                    ORG, REPO, request_id, remote_review_id=remote_id)

                self.assertEqual(row["state"], "PUBLISHED")
                self.assertEqual(row["remote_review_id"], str(remote_id))


if __name__ == "__main__":
    unittest.main()
