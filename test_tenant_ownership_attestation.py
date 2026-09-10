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


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class AttestedRootProvenanceAgreementTests(AttestationStoreTests):
    """Coexistence of provenance kinds is not a conflict. DISAGREEMENT is.

    The audit flagged `mapped_root_provenance_conflict` the moment a same-tenant
    attestation succeeded, because it treated "some repository under this root
    has no derived proof" as a conflict on every basis. For a derived mapping
    that is genuinely suspicious. For an attestation it is the normal case and
    the whole reason the basis exists -- a partially provable root is exactly
    what an operator attests.

    Every test here pins the boundary: same tenant corroborates, a different
    tenant conflicts, and several tenants conflict.
    """

    def _attest_directly(self, organization_id, tenant_id):
        """Record an attestation without the store's guards.

        Cases 2 and 3 cannot be reached through `attest_tenant_operational_root`
        -- it refuses them up front. They model a root that becomes
        contradictory AFTER an attestation was legitimately recorded, which is
        the state the audit and the gate actually have to survive.
        """
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, verified_at) "
            "VALUES (%s, %s, 'operator_attested_legacy', now())",
            (organization_id, tenant_id))
        self.store.connection.commit()

    def _conflicts(self):
        report = self.store.tenant_operational_ownership_audit()
        return [entry["kind"]
                for entry in report["cross_tenant_inconsistencies"]]

    # -- case 1: partial CI evidence agreeing with the attestation ---------

    def test_case_1_same_tenant_partial_evidence_is_allowed_and_audits_clean(self):
        tenant_id = self._proven_repository(
            "case1", 501, 5001, "Case1Root", "proven", "token-case1")
        self.store.ensure_repository("Case1Root", "unproven")

        result = self.store.attest_tenant_operational_root(
            organization_id="Case1Root", tenant_id=tenant_id,
            reason="reviewed by operator")

        self.assertEqual(result["status"], "attested")
        self.assertEqual(self._conflicts(), [])
        report = self.store.tenant_operational_ownership_audit()
        self.assertEqual(report["ambiguous_roots"], [])

    # -- case 2: CI evidence naming a different tenant ---------------------

    def test_case_2_evidence_for_another_tenant_is_refused_up_front(self):
        self._proven_repository(
            "case2-owner", 502, 5002, "Case2Root", "proven", "token-case2")
        self.store.ensure_repository("Case2Root", "unproven")
        claimant = self._tenant("case2-claimant")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="Case2Root", tenant_id=claimant,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "cross_tenant_inconsistency")
        self.assertIsNone(self._mapping("Case2Root"))

    def test_case_2_evidence_appearing_later_is_reported_as_a_conflict(self):
        # The attestation was legitimate when recorded; contradicting evidence
        # arrived afterwards. The audit must say so.
        claimant = self._tenant("case2-late-claimant")
        self.store.ensure_repository("Case2LateRoot", "unproven")
        self._attest_directly("Case2LateRoot", claimant)
        self.assertEqual(self._conflicts(), [])

        self._proven_repository(
            "case2-late-owner", 503, 5003, "Case2LateRoot", "proven",
            "token-case2-late")

        self.assertEqual(self._conflicts(), ["mapped_root_provenance_conflict"])

    # -- case 3: CI evidence across several tenants ------------------------

    def test_case_3_evidence_across_several_tenants_is_inconsistent(self):
        first = self._proven_repository(
            "case3-a", 504, 5004, "Case3Root", "first", "token-case3-a")
        self._proven_repository(
            "case3-b", 505, 5005, "Case3Root", "second", "token-case3-b")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="Case3Root", tenant_id=first,
                reason="reviewed by operator")
        self.assertEqual(str(raised.exception), "ambiguous_candidate_tenants")

        # And if such a state is reached anyway, the audit reports it.
        self._attest_directly("Case3Root", first)
        self.assertEqual(self._conflicts(), ["mapped_root_provenance_conflict"])

    # -- case 4: a fully provable root -------------------------------------

    def test_case_4_a_fully_provable_root_still_refuses_attestation(self):
        tenant_id = self._proven_repository(
            "case4", 506, 5006, "Case4Root", "only", "token-case4")

        with self.assertRaises(ValueError) as raised:
            self.store.attest_tenant_operational_root(
                organization_id="Case4Root", tenant_id=tenant_id,
                reason="reviewed by operator")

        self.assertEqual(str(raised.exception), "provable_without_attestation")

    def test_case_4_partial_and_complete_are_distinguished_by_the_same_rule(self):
        # Why production was allowed to attest: the refusal fires only when
        # EVERY repository is proven. One of four is not "provable".
        tenant_id = self._proven_repository(
            "case4b", 507, 5007, "Case4bRoot", "proven", "token-case4b")
        self.store.ensure_repository("Case4bRoot", "unproven")

        self.assertEqual(
            self.store.attest_tenant_operational_root(
                organization_id="Case4bRoot", tenant_id=tenant_id,
                reason="reviewed by operator")["status"],
            "attested")

    # -- case 5: the exact production shape ---------------------------------

    def test_case_5_the_production_shape_audits_clean_after_attestation(self):
        # AbhishekhPR: four repositories, one CI-proven, attested to that same
        # tenant. Reported `mapped_root_provenance_conflict` before this fix.
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="pre-tenant legacy operational data reviewed by operator")

        report = self.store.tenant_operational_ownership_audit()

        self.assertEqual(report["cross_tenant_inconsistencies"], [])
        self.assertEqual(report["ambiguous_roots"], [])
        self.assertEqual(
            [row["organization_id"] for row in report["mapped_roots"]],
            ["LegacyRoot"])

    def test_case_5_a_derived_mapping_that_went_partial_is_still_flagged(self):
        # The clause is not removed, only made basis-aware: a ci_token_binding
        # mapping whose chain no longer covers every repository stays a conflict.
        tenant_id = self._proven_repository(
            "case5b", 508, 5008, "Case5bRoot", "proven", "token-case5b")
        self.store.reconcile_tenant_operational_roots(apply=True)
        self.assertEqual(self._conflicts(), [])

        self.store.ensure_repository("Case5bRoot", "added-later")

        self.assertEqual(self._conflicts(), ["mapped_root_provenance_conflict"])
        self.assertEqual(self._mapping("Case5bRoot")["mapping_basis"],
                         "ci_token_binding")
        self.assertIsNotNone(tenant_id)

    # -- case 6: the destructive lifecycle gate ------------------------------

    def test_case_6_the_deletion_gate_accepts_agreeing_evidence(self):
        tenant_id = self._production_shape()
        self.store.attest_tenant_operational_root(
            organization_id="LegacyRoot", tenant_id=tenant_id,
            reason="reviewed by operator")

        inventory = self.store.tenant_operational_inventory(tenant_id)

        self.assertEqual(inventory["ownership_status"], "complete")
        self.assertEqual(inventory["attested_operational_roots"], ["LegacyRoot"])

    def test_case_6_the_deletion_gate_fails_closed_on_another_tenant(self):
        claimant = self._tenant("case6-claimant")
        self.store.ensure_repository("Case6Root", "unproven")
        self._attest_directly("Case6Root", claimant)
        self._proven_repository(
            "case6-owner", 509, 5009, "Case6Root", "proven", "token-case6")

        inventory = self.store.tenant_operational_inventory(claimant)

        self.assertEqual(inventory["ownership_status"], "inconsistent")
        with self.assertRaises(ValueError) as raised:
            self.store.begin_workspace_deletion(
                tenant_id=claimant,
                initiated_by_clerk_user_id="operator",
                confirmation_verified_at=datetime.now(timezone.utc))
        self.assertEqual(str(raised.exception),
                         "operational_ownership_inconsistent")

    def test_case_6_the_deletion_gate_fails_closed_on_several_tenants(self):
        first = self._proven_repository(
            "case6-multi-a", 510, 5010, "Case6MultiRoot", "first",
            "token-case6-a")
        self._proven_repository(
            "case6-multi-b", 511, 5011, "Case6MultiRoot", "second",
            "token-case6-b")
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, verified_at) "
            "VALUES ('Case6MultiRoot', %s, 'operator_attested_legacy', now())",
            (first,))
        self.store.connection.commit()

        inventory = self.store.tenant_operational_inventory(first)

        self.assertEqual(inventory["ownership_status"], "inconsistent")
        with self.assertRaises(ValueError) as raised:
            self.store.begin_workspace_deletion(
                tenant_id=first,
                initiated_by_clerk_user_id="operator",
                confirmation_verified_at=datetime.now(timezone.utc))
        self.assertEqual(str(raised.exception),
                         "operational_ownership_inconsistent")


if __name__ == "__main__":
    unittest.main()
