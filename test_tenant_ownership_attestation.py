"""Explicit operator attestation of pre-tenant legacy operational ownership.

Some legacy roots predate the tenant plane and carry no provider-issued
identifier of any kind -- `organizations` and `repositories` hold TEXT names and
nothing else. `ci_token_binding` correctly refuses them and Foundation 2
correctly reports `operational_ownership_incomplete`.

`operator_attested_legacy` records that a named human took responsibility for a
mapping the system cannot verify. These tests exist to keep the two apart: an
attestation must never be able to masquerade as derived evidence, never
overwrite one, and never resolve a state that is actually ambiguous.
"""
from __future__ import annotations

import io
import os
import unittest
from datetime import datetime, timezone

from click.testing import CliRunner


DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


class AttestationCliGuardTests(unittest.TestCase):
    """The guards that run BEFORE any database connection is opened.

    They are deliberately checked without a DSN: if one of them stopped firing
    first, `_admin_store` would exit(2) for a missing RELIUM_DATABASE_URL and
    the assertion below would fail with a different message.
    """

    def setUp(self):
        from agent.cli import cli

        self.cli = cli
        self.runner = CliRunner()
        self._previous = os.environ.pop("RELIUM_DATABASE_URL", None)

    def tearDown(self):
        if self._previous is not None:
            os.environ["RELIUM_DATABASE_URL"] = self._previous

    def _invoke(self, **overrides):
        arguments = {
            "--organization-id": "LegacyRoot",
            "--tenant-id": "ten_" + "a" * 32,
            "--reason": "pre-tenant legacy data reviewed by operator",
            "--confirm": "LegacyRoot",
        }
        arguments.update(overrides)
        argv = ["tenant-ownership-attest"]
        for flag, value in arguments.items():
            if value is not None:
                argv += [flag, value]
        return self.runner.invoke(self.cli, argv)

    def test_a_missing_confirmation_refuses_before_touching_the_database(self):
        result = self._invoke(**{"--confirm": None})
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--confirm", result.output)

    def test_a_mismatched_confirmation_refuses(self):
        result = self._invoke(**{"--confirm": "SomethingElse"})
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("must exactly equal", result.output)
        self.assertNotIn("RELIUM_DATABASE_URL", result.output)

    def test_the_confirmation_is_compared_exactly_not_loosely(self):
        # `Relium-site` and `relium-site` are different legacy rows. A
        # confirmation that folds case would defeat the point of the flag.
        for confirmation in ("legacyroot", "LEGACYROOT", " LegacyRoot",
                             "LegacyRoot "):
            with self.subTest(confirm=confirmation):
                result = self._invoke(**{"--confirm": confirmation})
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("must exactly equal", result.output)

    def test_an_empty_reason_refuses(self):
        for reason in ("", "   ", "\t"):
            with self.subTest(reason=repr(reason)):
                result = self._invoke(**{"--reason": reason})
                self.assertNotEqual(result.exit_code, 0)
                self.assertNotIn("RELIUM_DATABASE_URL", result.output)

    def test_a_missing_reason_refuses(self):
        result = self._invoke(**{"--reason": None})
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--reason", result.output)


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class AttestationStoreTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)

    def tearDown(self):
        self.store.close()

    # -- fixtures ---------------------------------------------------------

    def _tenant(self, suffix):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            f"clerk-{suffix}", organization_name=f"Tenant {suffix}")
        return tenant["tenant_id"]

    def _proven_repository(self, suffix, installation_id, github_repository_id,
                           organization_id, repository_id, token_id,
                           tenant_id=None):
        """One repository with the full derived CI proof chain."""
        tenant_id = tenant_id or self._tenant(suffix)
        self.store.record_github_installation(
            installation_id, github_app_id=1,
            github_account_id=installation_id + 9000,
            github_account_login=f"account-{suffix}",
            github_account_type="Organization",
            repository_selection="selected", status="active")
        self.store.bind_github_installation_to_tenant(
            installation_id, tenant_id=tenant_id,
            bound_by_clerk_user_id=f"user-{suffix}",
            verified_github_user_id=installation_id + 8000,
            bound_via_state_id=None)
        self.store.select_tenant_repository(
            github_repository_id, tenant_id=tenant_id,
            github_installation_id=installation_id,
            owner_login=f"display-{suffix}",
            name=f"display-repository-{suffix}")
        self.store.ensure_repository(organization_id, repository_id)
        self.store.create_service_token(
            token_id, "secret-hash-must-not-appear", organization_id,
            repository_id, environment=None, description="sensitive label",
            scope="ci")
        self.store.record_tenant_repository_ci_token(
            github_repository_id, tenant_id=tenant_id, ci_token_id=token_id,
            delivery="display_once", issued_at=datetime.now(timezone.utc))
        return tenant_id

    def _production_shape(self):
        """The real blocker: one proven repository, three with no proof at all."""
        tenant_id = self._proven_repository(
            "prod", 401, 4001, "LegacyRoot", "proven", "token-proven")
        for unproven in ("first", "second", "third"):
            self.store.ensure_repository("LegacyRoot", unproven)
        return tenant_id

    def _mapping(self, organization_id):
        row = self.store.connection.execute(
            "SELECT tenant_id, mapping_basis, source_ci_token_id, "
            "       source_github_repository_id, source_github_installation_id "
            "FROM tenant_operational_roots WHERE organization_id = %s",
            (organization_id,)).fetchone()
        return dict(row) if row else None

    # -- success ----------------------------------------------------------

    def test_an_unprovable_root_can_be_attested(self):
        tenant_id = self._production_shape()
        self.assertEqual(
            self.store.tenant_operational_inventory(tenant_id)["ownership_status"],
            "incomplete")

        result = self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="pre-tenant legacy operational data reviewed by operator")

        self.assertEqual(result["status"], "attested")
        self.assertEqual(result["mapping_basis"], "operator_attested_legacy")

    def test_the_mapping_records_no_provider_provenance(self):
        # An attestation has no provider evidence, and must not appear to.
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="reviewed by operator")

        mapping = self._mapping("LegacyRoot")
        self.assertEqual(mapping["mapping_basis"], "operator_attested_legacy")
        self.assertIsNone(mapping["source_ci_token_id"])
        self.assertIsNone(mapping["source_github_repository_id"])
        self.assertIsNone(mapping["source_github_installation_id"])

    def test_the_schema_refuses_an_attestation_wearing_provider_provenance(self):
        # Not merely convention: the CHECK constraint enforces it.
        import psycopg

        tenant_id = self._production_shape()
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.connection.execute(
                "INSERT INTO tenant_operational_roots "
                "(organization_id, tenant_id, mapping_basis, "
                " source_github_repository_id, source_ci_token_id, verified_at) "
                "VALUES ('LegacyRoot', %s, 'operator_attested_legacy', "
                "        4001, 'token-proven', now())", (tenant_id,))
        self.store.connection.rollback()

    def test_the_reason_is_recorded_durably(self):
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="pre-tenant legacy operational data reviewed by operator")

        records = self.store.tenant_operational_root_attestations(
            tenant_id=tenant_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["organization_id"], "LegacyRoot")
        self.assertEqual(records[0]["tenant_id"], tenant_id)
        self.assertEqual(records[0]["mapping_basis"], "operator_attested_legacy")
        self.assertIn("reviewed by operator", records[0]["reason"])
        self.assertIsNotNone(records[0]["attested_at"])

    # -- the lifecycle gate -----------------------------------------------

    def test_an_attested_root_satisfies_the_ownership_gate(self):
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="reviewed by operator")

        inventory = self.store.tenant_operational_inventory(tenant_id)

        self.assertEqual(inventory["ownership_status"], "complete")
        self.assertIn("LegacyRoot", inventory["operational_roots"])

    def test_the_weaker_provenance_stays_visible_to_every_caller(self):
        # The gate is satisfied, but nothing pretends this was proven.
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="reviewed by operator")

        inventory = self.store.tenant_operational_inventory(tenant_id)
        access = self.store.tenant_lifecycle_access_inventory(tenant_id)

        self.assertEqual(inventory["attested_operational_roots"], ["LegacyRoot"])
        self.assertEqual(access["attested_operational_roots"], ["LegacyRoot"])

    def test_a_ci_proven_root_is_not_reported_as_attested(self):
        tenant_id = self._proven_repository(
            "clean", 402, 4002, "CleanRoot", "only", "token-clean")
        self.store.reconcile_tenant_operational_roots(apply=True)

        inventory = self.store.tenant_operational_inventory(tenant_id)

        self.assertEqual(inventory["ownership_status"], "complete")
        self.assertEqual(inventory["attested_operational_roots"], [])

    # -- refusals ---------------------------------------------------------

    def test_a_root_mapped_to_another_tenant_is_refused(self):
        tenant_id = self._production_shape()
        other = self._tenant("other")
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_ci_token_id, verified_at) "
            "VALUES ('LegacyRoot', %s, 'ci_token_binding', 4001, "
            "        'token-proven', now())", (other,))
        self.store.connection.commit()

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="LegacyRoot", tenant_id=tenant_id,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "mapped_to_other_tenant")
        self.assertEqual(self._mapping("LegacyRoot")["tenant_id"], other)
        self.assertEqual(self._mapping("LegacyRoot")["mapping_basis"],
                         "ci_token_binding")

    def test_an_existing_authoritative_mapping_is_never_downgraded(self):
        tenant_id = self._proven_repository(
            "auth", 403, 4003, "ProvenRoot", "only", "token-auth")
        self.store.reconcile_tenant_operational_roots(apply=True)

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="ProvenRoot", tenant_id=tenant_id,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "already_mapped_authoritatively")
        self.assertEqual(self._mapping("ProvenRoot")["mapping_basis"],
                         "ci_token_binding")

    def test_a_root_provable_without_attestation_is_refused(self):
        # A weaker claim must not be recorded where a stronger one is available.
        tenant_id = self._proven_repository(
            "provable", 404, 4004, "ProvableRoot", "only", "token-provable")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="ProvableRoot", tenant_id=tenant_id,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "provable_without_attestation")
        self.assertIsNone(self._mapping("ProvableRoot"))

    def test_contradicting_derived_evidence_is_refused(self):
        # Evidence exists and points somewhere else. An attestation covers an
        # ABSENCE of evidence, never a disagreement with it.
        owner = self._proven_repository(
            "owner", 405, 4005, "ContestedRoot", "proven", "token-owner")
        self.store.ensure_repository("ContestedRoot", "unproven")
        claimant = self._tenant("claimant")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="ContestedRoot", tenant_id=claimant,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "cross_tenant_inconsistency")
        self.assertIsNone(self._mapping("ContestedRoot"))
        self.assertIsNotNone(owner)

    def test_multiple_candidate_tenants_are_refused(self):
        first = self._proven_repository(
            "multi-a", 406, 4006, "MixedRoot", "first", "token-multi-a")
        self._proven_repository(
            "multi-b", 407, 4007, "MixedRoot", "second", "token-multi-b")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="MixedRoot", tenant_id=first,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "ambiguous_candidate_tenants")
        self.assertIsNone(self._mapping("MixedRoot"))

    def test_an_unknown_organization_or_tenant_is_refused(self):
        tenant_id = self._production_shape()

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="NoSuchRoot", tenant_id=tenant_id,
                reason="reviewed by operator")
        self.assertEqual(str(raised.exception), "organization_not_found")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="LegacyRoot", tenant_id="ten_" + "z" * 32,
                reason="reviewed by operator")
        self.assertEqual(str(raised.exception), "tenant_not_found")

    def test_an_empty_reason_is_refused_at_the_store_too(self):
        tenant_id = self._production_shape()
        for reason in ("", "   ", None):
            with self.subTest(reason=repr(reason)):
                with self.assertRaises(ValueError):
                    self.store.attest_tenant_operational_root(
                        organization_id="LegacyRoot", tenant_id=tenant_id,
                        reason=reason)
        self.assertIsNone(self._mapping("LegacyRoot"))

    # -- idempotency and transaction safety --------------------------------

    def test_repeating_the_same_attestation_writes_nothing_further(self):
        tenant_id = self._production_shape()
        first = self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="reviewed by operator")

        second = self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="a different reason on the retry")

        self.assertEqual(first["status"], "attested")
        self.assertEqual(second["status"], "already_attested")
        self.assertEqual(
            len(self.store.tenant_operational_root_attestations(
                tenant_id=tenant_id)), 1)
        self.assertEqual(int(self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots "
            "WHERE organization_id = 'LegacyRoot'").fetchone()["n"]), 1)

    def test_a_refusal_leaves_no_partial_write(self):
        # Every refusal happens inside the SERIALIZABLE transaction, so the
        # mapping and its audit record are all-or-nothing together.
        tenant_id = self._production_shape()
        other = self._tenant("partial-other")
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_ci_token_id, verified_at) "
            "VALUES ('LegacyRoot', %s, 'ci_token_binding', 4001, "
            "        'token-proven', now())", (other,))
        self.store.connection.commit()

        with self.assertRaises(ValueError):
            self.store.attest_tenant_operational_root(
                organization_id="LegacyRoot", tenant_id=tenant_id,
                reason="reviewed by operator")

        self.assertEqual(
            self.store.tenant_operational_root_attestations(), [])
        self.assertEqual(self._mapping("LegacyRoot")["tenant_id"], other)

    def test_the_attestation_runs_at_serializable_isolation(self):
        tenant_id = self._production_shape()
        observed = []
        original = self.store.connection.execute

        def record(sql, *args, **kwargs):
            observed.append(str(sql))
            return original(sql, *args, **kwargs)

        self.store.connection.execute = record
        try:
            self.store.attest_tenant_operational_root(
                organization_id="LegacyRoot", tenant_id=tenant_id,
                reason="reviewed by operator")
        finally:
            self.store.connection.execute = original

        self.assertTrue(
            any("SERIALIZABLE" in statement for statement in observed),
            "attestation must run at SERIALIZABLE isolation")

    def test_the_attestation_trail_is_purged_with_the_workspace(self):
        # Durable for the life of the tenant, not beyond it: a completed
        # deletion must retain only a dissociated receipt, so a row naming the
        # deleted tenant and its legacy root cannot be left behind.
        from agent.tenant_operational_ownership import TENANT_OWNED_TABLES

        self.assertIn("tenant_operational_root_attestations",
                      TENANT_OWNED_TABLES)
        source = io.open(
            "agent/postgres_lifecycle_store.py", encoding="utf-8").read()
        purge = source.split("def purge_workspace_operational_data", 1)[1]
        purge = purge.split("def finalize_workspace_deletion", 1)[0]
        self.assertIn("DELETE FROM tenant_operational_root_attestations", purge)

    def test_the_reconciler_never_touches_an_attested_root(self):
        # --apply-unambiguous inserts only where no mapping exists, so an
        # attested root is left exactly as the operator recorded it.
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="reviewed by operator")

        self.store.reconcile_tenant_operational_roots(apply=True)

        self.assertEqual(self._mapping("LegacyRoot")["mapping_basis"],
                         "operator_attested_legacy")


if __name__ == "__main__":
    unittest.main()
