"""Onboarding identity, tenancy and authorization — Phase 1.

Two layers:

  * The capability policy, which needs no database. It asserts the boundary
    that Clerk introduced: a Clerk session is a human, but not the *kind* of
    human that existing dashboard and governance capabilities were written for.

  * The served routes, against a REAL PostgreSQL. Idempotency under
    concurrency, tenant isolation and the constraint behaviour being relied on
    are properties of the database, and a mock cannot demonstrate any of them.

NO REAL CREDENTIAL APPEARS IN THIS FILE. RSA keys are generated per run;
issuers are .test hostnames; no token is printed, and the assertions that
inspect a refusal check that it does NOT contain the token.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ISSUER = "https://onboarding-test.clerk.accounts.test"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- token forging

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(document) -> str:
    return _b64url(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def _b64url_uint(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class _Signer:
    def __init__(self, kid="onboarding-test-key"):
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537,
                                                    key_size=2048)

    def token(self, **claims):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = {
            "iss": ISSUER,
            "sub": "user_2default",
            "sid": "sess_2default",
            "exp": int((NOW + timedelta(minutes=10)).timestamp()),
            "iat": int(NOW.timestamp()),
        }
        payload.update({k: v for k, v in claims.items() if v is not None})
        header = {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(),
                                          hashes.SHA256())
        return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


class _StubJwks:
    def __init__(self, *signers):
        self._keys = {s.kid: s.private_key.public_key() for s in signers}

    def key_for(self, kid):
        from agent.api.clerk_identity import ClerkVerificationError

        key = self._keys.get(kid)
        if key is None:
            raise ClerkVerificationError("token key is not recognised")
        return key


# ------------------------------------------------------- capability policy

class OnboardingCapabilityTests(unittest.TestCase):
    """The separation between a Clerk identity and GitHub authority.

    These are the assertions that stop Clerk quietly becoming an authorization
    system. They need no database because the policy is pure.
    """

    def setUp(self):
        from agent.api.clerk_identity import ClerkPrincipal

        self.clerk = ClerkPrincipal(
            clerk_user_id="user_2abc", clerk_organization_id="org_2acme",
            tenant_id="ten_" + "a" * 32)

    def _github_principal(self, *, may_govern):
        from agent.api.sessions import HumanPrincipal

        return HumanPrincipal(
            organization_id="acme", repository_id="analytics", environment="prod",
            github_login="maya", github_permission="push" if may_govern else "pull",
            may_govern=may_govern, session_id_hash="hash")

    def _machine_principal(self, scope="collector"):
        from agent.api.auth import TenantScope

        return TenantScope(organization_id="acme", repository_id="analytics",
                           environment="prod", token_id="tok", scope=scope)

    # -- what a Clerk session may NOT do ---------------------------------

    def test_a_clerk_session_cannot_perform_governance(self):
        """The central rule. Administering a Clerk organization must not confer
        the authority to change what happens to a pull request."""
        from agent.api.authorization import GOVERNANCE_WRITE, CapabilityError, authorize

        with self.assertRaises(CapabilityError):
            authorize(self.clerk, GOVERNANCE_WRITE)

    def test_a_clerk_session_cannot_read_the_dashboard(self):
        """Dashboard reads are scoped by a verified GitHub repository
        permission. A Clerk session has none, so it must not inherit the
        capability merely by being a human."""
        from agent.api.authorization import DASHBOARD_READ, CapabilityError, authorize

        with self.assertRaises(CapabilityError):
            authorize(self.clerk, DASHBOARD_READ)

    def test_a_clerk_session_cannot_ingest_collector_data(self):
        from agent.api.authorization import COLLECTOR_INGEST, CapabilityError, authorize

        with self.assertRaises(CapabilityError):
            authorize(self.clerk, COLLECTOR_INGEST)

    def test_a_clerk_session_cannot_submit_manifest_evidence(self):
        from agent.api.authorization import CI_MANIFEST_INGEST, CapabilityError, authorize

        with self.assertRaises(CapabilityError):
            authorize(self.clerk, CI_MANIFEST_INGEST)

    def test_a_forged_may_govern_attribute_does_not_grant_governance(self):
        """Even if something contrives to present a Clerk-provider principal
        claiming may_govern, the identity-provider check refuses first."""
        from agent.api.authorization import GOVERNANCE_WRITE, CapabilityError, authorize

        class _Liar:
            is_human = True
            identity_provider = "clerk"
            may_govern = True
            github_permission = "admin"

        with self.assertRaises(CapabilityError):
            authorize(_Liar(), GOVERNANCE_WRITE)

    # -- what a Clerk session MAY do --------------------------------------

    def test_a_clerk_session_may_read_and_write_onboarding(self):
        from agent.api.authorization import ONBOARDING_READ, ONBOARDING_WRITE, authorize

        authorize(self.clerk, ONBOARDING_READ)
        authorize(self.clerk, ONBOARDING_WRITE)

    # -- what everybody else may NOT do -----------------------------------

    def test_a_github_session_cannot_use_onboarding_routes(self):
        """Onboarding is Clerk's. A GitHub dashboard session is scoped to a
        repository, which during first-run setup does not exist yet."""
        from agent.api.authorization import (
            ONBOARDING_READ, ONBOARDING_WRITE, CapabilityError, authorize,
        )

        principal = self._github_principal(may_govern=True)
        for capability in (ONBOARDING_READ, ONBOARDING_WRITE):
            with self.assertRaises(CapabilityError):
                authorize(principal, capability)

    def test_a_machine_token_cannot_use_onboarding_routes(self):
        """No service credential may mint a tenant or enumerate setup."""
        from agent.api.authorization import (
            ONBOARDING_READ, ONBOARDING_WRITE, CapabilityError, authorize,
        )

        for scope in ("collector", "operator_read", "ci"):
            principal = self._machine_principal(scope)
            for capability in (ONBOARDING_READ, ONBOARDING_WRITE):
                with self.assertRaises(CapabilityError):
                    authorize(principal, capability)

    # -- nothing that worked before stopped working ------------------------

    def test_existing_github_capabilities_are_unchanged(self):
        from agent.api.authorization import (
            DASHBOARD_READ, GOVERNANCE_WRITE, CapabilityError, authorize,
        )

        governing = self._github_principal(may_govern=True)
        authorize(governing, DASHBOARD_READ)
        authorize(governing, GOVERNANCE_WRITE)

        reader = self._github_principal(may_govern=False)
        authorize(reader, DASHBOARD_READ)
        with self.assertRaises(CapabilityError):
            authorize(reader, GOVERNANCE_WRITE)

    def test_existing_machine_capabilities_are_unchanged(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTOR_INGEST, DASHBOARD_READ, CapabilityError,
            authorize,
        )

        authorize(self._machine_principal("collector"), COLLECTOR_INGEST)
        authorize(self._machine_principal("ci"), CI_MANIFEST_INGEST)
        authorize(self._machine_principal("operator_read"), DASHBOARD_READ)
        with self.assertRaises(CapabilityError):
            authorize(self._machine_principal("collector"), CI_MANIFEST_INGEST)

    def test_pre_clerk_human_capabilities_stay_github_only(self):
        """A human capability written before Clerk must not have silently
        acquired a second identity provider.

        Machine-only capabilities are asserted separately, in
        test_machine_token_regression.py: they carry no identity provider at
        all, because naming one would imply a human could hold them.
        """
        from agent.api.authorization import (
            COLLECTION_REQUEST_READ, DASHBOARD_READ, GOVERNANCE_WRITE,
        )

        for capability in (DASHBOARD_READ, GOVERNANCE_WRITE, COLLECTION_REQUEST_READ):
            self.assertTrue(capability.human, capability.name)
            self.assertEqual(capability.human_identities, frozenset({"github"}),
                             f"{capability.name} changed identity providers")


# ---------------------------------------------------------- served routes

def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; onboarding API requires a real PostgreSQL")
class OnboardingRouteTests(unittest.TestCase):
    """The served endpoints, over a real database and the real application."""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.signer = _Signer()
        cls.foreign_signer = _Signer(kid="not-our-clerk")
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.verifier = ClerkVerifier(
            ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks"),
            jwks=_StubJwks(cls.signer), clock=lambda: NOW)
        cls.app = create_http_app(
            webhook_secret="onboarding-test-webhook-secret",
            job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024,
            shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0,
            store_pool=cls.pool,
            clerk_verifier=cls.verifier,
        )
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        with self.pool.acquire() as store:
            store.connection.execute("DELETE FROM tenants")

    def _auth(self, **claims):
        return {"Authorization": f"Bearer {self.signer.token(**claims)}"}

    # -- authentication ----------------------------------------------------

    def test_no_authorization_header_is_unauthorized(self):
        response = self.client.get("/api/onboarding/state")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], "unauthorized")

    def test_a_malformed_bearer_header_is_unauthorized(self):
        for header in ("", "Bearer", "Bearer ", "Basic abc", "Token abc",
                       "Bearer not.a.jwt", "bearer", "Bearer a.b"):
            response = self.client.get("/api/onboarding/state",
                                       headers={"Authorization": header})
            self.assertEqual(response.status_code, 401, header)

    def test_a_token_from_another_clerk_instance_is_unauthorized(self):
        """Correctly signed, by the wrong signer. Must not authenticate."""
        token = self.foreign_signer.token(sub="user_2attacker")
        response = self.client.get("/api/onboarding/state",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)

    def test_an_expired_token_is_unauthorized(self):
        expired = self.signer.token(
            exp=int((NOW - timedelta(hours=1)).timestamp()))
        response = self.client.get("/api/onboarding/state",
                                   headers={"Authorization": f"Bearer {expired}"})
        self.assertEqual(response.status_code, 401)

    def test_a_wrong_issuer_is_unauthorized(self):
        token = self.signer.token(iss="https://evil.clerk.accounts.test")
        response = self.client.get("/api/onboarding/state",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)

    def test_a_tampered_token_is_unauthorized(self):
        """Rewriting org_id to another organization must not authenticate."""
        token = self.signer.token(org_id="org_2mine")
        header, _, signature = token.split(".")
        forged_payload = _b64url_json({
            "iss": ISSUER, "sub": "user_2attacker", "org_id": "org_2victim",
            "exp": int((NOW + timedelta(minutes=10)).timestamp())})
        response = self.client.get(
            "/api/onboarding/state",
            headers={"Authorization": f"Bearer {header}.{forged_payload}.{signature}"})
        self.assertEqual(response.status_code, 401)

    def test_an_unauthorized_response_does_not_echo_the_token(self):
        token = self.signer.token(exp=int((NOW - timedelta(days=1)).timestamp()))
        response = self.client.get("/api/onboarding/state",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(token, response.text)
        # And nothing about which check failed, which would be a probing oracle.
        self.assertEqual(set(response.json()) - {"request_id"}, {"status"})

    def test_a_relium_service_token_cannot_use_onboarding(self):
        """A machine credential must not authenticate on a Clerk route."""
        response = self.client.get(
            "/api/onboarding/state",
            headers={"Authorization": "Bearer rlm_deadbeef.some-secret-value"})
        self.assertEqual(response.status_code, 401)

    # -- state -------------------------------------------------------------

    def test_a_new_user_is_told_the_workspace_step_is_required(self):
        response = self.client.get("/api/onboarding/state",
                                   headers=self._auth(org_id="org_2fresh"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["complete"], False)
        self.assertEqual(body["current_step"], "workspace")
        self.assertIsNone(body["workspace"])

    # -- the Clerk organization bootstrap ---------------------------------
    #
    # Clerk owns organizations, and the application uses them, so a signed-in
    # user may still have no active organization. That is a different problem
    # from "no Relium tenant yet" and gets a different answer, because the fix
    # lives in a different system.

    def test_no_active_organization_is_reported_as_its_own_step(self):
        response = self.client.get("/api/onboarding/state", headers=self._auth())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["current_step"], "organization")
        self.assertEqual(body["code"], "clerk_organization_required")
        self.assertIsNone(body["workspace"])

    def test_no_active_organization_is_distinct_from_no_tenant(self):
        """Two different states, two different answers.

        Collapsing them would send a user with no Clerk organization to a
        Relium form that cannot succeed.
        """
        without = self.client.get(
            "/api/onboarding/state", headers=self._auth()).json()
        with_org = self.client.get(
            "/api/onboarding/state", headers=self._auth(org_id="org_2fresh")).json()
        self.assertEqual(without["current_step"], "organization")
        self.assertEqual(with_org["current_step"], "workspace")
        self.assertNotIn("code", with_org)

    def test_creating_a_workspace_without_an_organization_is_a_conflict(self):
        """409 with a stable code, not 422.

        The body is fine; the session state is not. The code is what routes the
        user into Clerk's organization selection/creation flow.
        """
        response = self.client.put("/api/tenants", headers=self._auth(),
                                   json={"organization_name": "Just Me"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "clerk_organization_required")

    def test_no_tenant_is_created_for_a_session_without_an_organization(self):
        """Relium must not invent an organization, or a tenant standing in for
        one. A second Clerk organization is exactly the duplicate this design
        exists to avoid."""
        self.client.put("/api/tenants", headers=self._auth(),
                        json={"organization_name": "Just Me"})
        self.assertEqual(self._tenant_count(), 0)

    def test_the_same_user_succeeds_once_an_organization_becomes_active(self):
        """The recovery path: the user picks an organization in Clerk, the
        frontend gets a refreshed token carrying org_id, and the same request
        now succeeds."""
        refused = self.client.put("/api/tenants", headers=self._auth(sub="user_2joiner"),
                                  json={"organization_name": "Acme"})
        self.assertEqual(refused.status_code, 409)

        accepted = self.client.put(
            "/api/tenants",
            headers=self._auth(sub="user_2joiner", org_id="org_2joined"),
            json={"organization_name": "Acme"})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["clerk_organization_id"], "org_2joined")

    def test_the_github_section_reports_only_verified_bindings(self):
        """A fresh tenant has no installation, and says so factually.

        `not_connected` here is a fact about stored bindings, not a guess:
        nothing reaches this table without passing the three verifications in
        agent/api/github_installation.py.
        """
        self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                        json={"organization_name": "Acme"})
        body = self.client.get("/api/onboarding/state",
                               headers=self._auth(org_id="org_2acme")).json()
        self.assertEqual(body["github"]["status"], "not_connected")
        self.assertEqual(body["github"]["installations"], [])
        self.assertIs(body["github"]["identity"]["linked"], False)

    def test_phase_three_state_is_null_rather_than_invented(self):
        """dbt configuration is not implemented yet. Null says so; a shape
        would assert a configuration nothing has checked."""
        self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                        json={"organization_name": "Acme"})
        body = self.client.get("/api/onboarding/state",
                               headers=self._auth(org_id="org_2acme")).json()
        self.assertIsNone(body["configuration"])

    def test_state_after_workspace_creation_advances_the_step(self):
        self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                        json={"organization_name": "Acme Analytics"})
        body = self.client.get("/api/onboarding/state",
                               headers=self._auth(org_id="org_2acme")).json()
        self.assertEqual(body["current_step"], "github")
        self.assertIs(body["complete"], False)
        self.assertEqual(body["workspace"]["organization_name"], "Acme Analytics")

    # -- workspace creation ------------------------------------------------

    def test_workspace_creation_returns_a_relium_tenant_id(self):
        response = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2acme"),
            json={"organization_name": "Acme Analytics", "role": "Analytics engineer",
                  "team_size": "6-20"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["id"].startswith("ten_"))
        self.assertEqual(body["clerk_organization_id"], "org_2acme")
        self.assertEqual(body["role"], "Analytics engineer")

    def test_the_tenant_id_is_not_the_clerk_organization_id(self):
        body = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2acme"),
            json={"organization_name": "Acme"}).json()
        self.assertNotEqual(body["id"], "org_2acme")
        self.assertNotIn("org_2acme", body["id"])

    # A session with no active Clerk organization is covered by the bootstrap
    # tests above: it is a 409 with a routing code, not a validation failure.

    def test_a_blank_organization_name_is_refused(self):
        for name in ("", "   ", "\t\n"):
            response = self.client.put("/api/tenants",
                                       headers=self._auth(org_id="org_2acme"),
                                       json={"organization_name": name})
            self.assertEqual(response.status_code, 422, repr(name))

    def test_a_non_string_organization_name_is_refused(self):
        for name in (123, None, {"a": 1}, ["x"], True):
            response = self.client.put("/api/tenants",
                                       headers=self._auth(org_id="org_2acme"),
                                       json={"organization_name": name})
            self.assertEqual(response.status_code, 422, repr(name))

    def test_an_over_long_organization_name_is_refused(self):
        response = self.client.put("/api/tenants",
                                   headers=self._auth(org_id="org_2acme"),
                                   json={"organization_name": "x" * 5000})
        self.assertEqual(response.status_code, 422)

    def test_a_non_object_body_is_refused(self):
        for body in ("[]", '"a string"', "null", "not json at all", ""):
            response = self.client.put(
                "/api/tenants",
                headers={**self._auth(org_id="org_2acme"),
                         "Content-Type": "application/json"},
                content=body)
            self.assertEqual(response.status_code, 400, repr(body))

    # -- identity spoofing --------------------------------------------------

    def test_a_body_supplied_clerk_organization_id_is_ignored(self):
        """The organization comes from the verified token. A body field must
        not be able to create or address another organization's tenant."""
        response = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2mine"),
            json={"organization_name": "Mine",
                  "clerk_organization_id": "org_2victim"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["clerk_organization_id"], "org_2mine")

    def test_a_body_supplied_tenant_id_is_ignored(self):
        first = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2mine"),
            json={"organization_name": "Mine"}).json()
        second = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2mine"),
            json={"organization_name": "Mine", "id": "ten_" + "f" * 32,
                  "tenant_id": "ten_" + "e" * 32}).json()
        self.assertEqual(second["id"], first["id"])

    def test_a_query_supplied_tenant_id_is_ignored(self):
        first = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2mine"),
            json={"organization_name": "Mine"}).json()
        body = self.client.get(
            f"/api/onboarding/state?tenant_id=ten_{'f' * 32}",
            headers=self._auth(org_id="org_2mine")).json()
        self.assertEqual(body["workspace"]["id"], first["id"])

    # -- tenant isolation ---------------------------------------------------

    def test_two_organizations_get_separate_tenants(self):
        acme = self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                               json={"organization_name": "Acme"}).json()
        globex = self.client.put("/api/tenants", headers=self._auth(org_id="org_2globex"),
                                 json={"organization_name": "Globex"}).json()
        self.assertNotEqual(acme["id"], globex["id"])

    def test_one_organization_cannot_see_another_organizations_state(self):
        self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                        json={"organization_name": "Acme Analytics"})
        body = self.client.get("/api/onboarding/state",
                               headers=self._auth(org_id="org_2globex")).json()
        self.assertIsNone(body["workspace"])
        self.assertEqual(body["current_step"], "workspace")
        self.assertNotIn("Acme", json.dumps(body))

    def test_a_different_user_in_the_same_organization_sees_the_same_tenant(self):
        """Tenancy follows the organization, not the individual."""
        created = self.client.put(
            "/api/tenants",
            headers=self._auth(sub="user_2founder", org_id="org_2acme"),
            json={"organization_name": "Acme"}).json()
        body = self.client.get(
            "/api/onboarding/state",
            headers=self._auth(sub="user_2colleague", org_id="org_2acme")).json()
        self.assertEqual(body["workspace"]["id"], created["id"])

    # -- idempotency --------------------------------------------------------

    def test_repeated_creation_returns_the_same_tenant(self):
        first = self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                                json={"organization_name": "Acme"}).json()
        second = self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                                 json={"organization_name": "Acme"}).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self._tenant_count(), 1)

    def test_repeated_creation_updates_mutable_fields_in_place(self):
        first = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2acme"),
            json={"organization_name": "Acme", "role": "Analyst"}).json()
        second = self.client.put(
            "/api/tenants", headers=self._auth(org_id="org_2acme"),
            json={"organization_name": "Acme Analytics",
                  "role": "Analytics engineer"}).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["organization_name"], "Acme Analytics")
        self.assertEqual(second["role"], "Analytics engineer")
        self.assertEqual(self._tenant_count(), 1)

    def test_renaming_does_not_create_a_second_tenant(self):
        """The display name is not identity."""
        first = self.client.put("/api/tenants", headers=self._auth(org_id="org_2acme"),
                                json={"organization_name": "Acme"}).json()
        for name in ("Acme Inc", "Acme Analytics", "ACME"):
            again = self.client.put("/api/tenants",
                                    headers=self._auth(org_id="org_2acme"),
                                    json={"organization_name": name}).json()
            self.assertEqual(again["id"], first["id"])
        self.assertEqual(self._tenant_count(), 1)

    def test_two_organizations_may_share_a_display_name(self):
        """Names are not unique, and must not be treated as identity."""
        a = self.client.put("/api/tenants", headers=self._auth(org_id="org_2first"),
                            json={"organization_name": "Acme"}).json()
        b = self.client.put("/api/tenants", headers=self._auth(org_id="org_2second"),
                            json={"organization_name": "Acme"}).json()
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self._tenant_count(), 2)

    def test_concurrent_creation_produces_exactly_one_tenant(self):
        """The race a read-then-write would lose.

        Eight simultaneous first-time requests for one organization. Every one
        must return the same tenant id and the table must hold one row: the
        UNIQUE constraint resolves this, not application logic.
        """
        results = []
        errors = []
        barrier = threading.Barrier(8)

        def create():
            try:
                barrier.wait(timeout=20)
                response = self.client.put(
                    "/api/tenants", headers=self._auth(org_id="org_2concurrent"),
                    json={"organization_name": "Concurrent Ltd"})
                results.append((response.status_code, response.json()))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for status, _ in results:
            self.assertEqual(status, 200)
        identifiers = {body["id"] for _, body in results}
        self.assertEqual(len(identifiers), 1, f"tenant ids diverged: {identifiers}")
        self.assertEqual(self._tenant_count(), 1)

    def test_concurrent_creation_for_different_organizations_is_not_serialised_wrongly(self):
        """Distinct organizations must each get their own tenant."""
        results = {}
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def create(index):
            barrier.wait(timeout=20)
            response = self.client.put(
                "/api/tenants", headers=self._auth(org_id=f"org_2parallel{index}"),
                json={"organization_name": f"Parallel {index}"})
            with lock:
                results[index] = response.json()["id"]

        threads = [threading.Thread(target=create, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(set(results.values())), 4)
        self.assertEqual(self._tenant_count(), 4)

    # -- helpers ------------------------------------------------------------

    def _tenant_count(self):
        with self.pool.acquire() as store:
            return store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenants").fetchone()["c"]


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class OnboardingUnconfiguredClerkTests(unittest.TestCase):
    """A deployment with no Clerk configuration.

    The routes must still be served — a vanishing endpoint is indistinguishable
    from one that was never deployed, and the API contract requires a stable
    route table — but they must authenticate nobody.
    """

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=2)
        cls.app = create_http_app(
            webhook_secret="unconfigured-test", job_queue=_StubQueue(),
            max_body_bytes=1024, shutdown_timeout_seconds=1.0, clock=lambda: 0.0,
            store_pool=cls.pool)
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def test_the_routes_are_still_served(self):
        from agent.api.contract import served_routes

        served = {(e["method"], e["path"]) for e in served_routes(self.app)}
        self.assertIn(("GET", "/api/onboarding/state"), served)
        self.assertIn(("PUT", "/api/tenants"), served)

    def test_state_reports_unavailable_rather_than_authenticating(self):
        response = self.client.get("/api/onboarding/state",
                                   headers={"Authorization": "Bearer anything"})
        self.assertEqual(response.status_code, 503)

    def test_workspace_creation_reports_unavailable(self):
        response = self.client.put("/api/tenants",
                                   headers={"Authorization": "Bearer anything"},
                                   json={"organization_name": "Acme"})
        self.assertEqual(response.status_code, 503)


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


if __name__ == "__main__":
    unittest.main()
