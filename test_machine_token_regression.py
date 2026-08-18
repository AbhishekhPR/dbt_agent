"""Machine-principal authorization must be byte-for-byte unchanged by Clerk.

Clerk introduced a second kind of HUMAN principal. It must not have touched
machine principals at all — not their capabilities, not their scopes, not the
routes they authenticate on.

These tests are deliberately paranoid and deliberately redundant with
test_dashboard_auth.py and test_public_api.py. Their job is to fail loudly if a
future change to the identity-provider machinery leaks into the machine path,
which is the specific regression the Clerk work could plausibly cause.

The capability tests need no database. The route tests use a real PostgreSQL,
because "an existing service-token route still works" is only worth asserting
against the served application.
"""
from __future__ import annotations

import os
import unittest

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


def _machine(scope, *, organization_id="acme", repository_id="analytics",
             environment="prod"):
    from agent.api.auth import TenantScope

    return TenantScope(organization_id=organization_id,
                       repository_id=repository_id, environment=environment,
                       token_id="tok-regression", scope=scope)


def _github_human(*, may_govern):
    from agent.api.sessions import HumanPrincipal

    return HumanPrincipal(
        organization_id="acme", repository_id="analytics", environment="prod",
        github_login="maya", github_permission="push" if may_govern else "pull",
        may_govern=may_govern, session_id_hash="hash")


def _clerk_human(*, tenant_id="ten_" + "a" * 32):
    from agent.api.clerk_identity import ClerkPrincipal

    return ClerkPrincipal(clerk_user_id="user_2abc",
                          clerk_organization_id="org_2acme",
                          tenant_id=tenant_id)


class MachineCapabilityShapeTests(unittest.TestCase):
    """The capability table itself, asserted field by field.

    An earlier draft of this work gave every capability a default
    ``human_identities`` of {"github"}, including the machine-only ones. That
    was harmless at runtime — the ``human`` flag refuses every human first —
    but it stated something false about COLLECTOR_INGEST and CI_MANIFEST_INGEST
    in a table people read to understand the policy. These tests pin the shape
    so it cannot drift back.
    """

    def test_machine_only_capabilities_carry_no_identity_providers(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTOR_INGEST, PIPELINE_INGEST,
        )

        for capability in (COLLECTOR_INGEST, PIPELINE_INGEST, CI_MANIFEST_INGEST):
            self.assertFalse(capability.human,
                             f"{capability.name} must not be a human capability")
            self.assertEqual(
                capability.human_identities, frozenset(),
                f"{capability.name} is machine-only and must name no identity "
                f"provider; a provider there implies a human could hold it")

    def test_machine_scopes_are_exactly_what_they_were(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTION_REQUEST_READ, COLLECTOR_INGEST,
            DASHBOARD_READ, GOVERNANCE_WRITE, PIPELINE_INGEST,
        )

        self.assertEqual(COLLECTOR_INGEST.token_scopes, frozenset({"collector"}))
        self.assertEqual(PIPELINE_INGEST.token_scopes, frozenset({"collector"}))
        self.assertEqual(CI_MANIFEST_INGEST.token_scopes, frozenset({"ci"}))
        self.assertEqual(DASHBOARD_READ.token_scopes, frozenset({"operator_read"}))
        self.assertEqual(COLLECTION_REQUEST_READ.token_scopes,
                         frozenset({"collector", "operator_read"}))
        # No machine scope grants governance. A leaked machine credential must
        # not be able to approve an exception.
        self.assertEqual(GOVERNANCE_WRITE.token_scopes, frozenset())

    def test_human_capabilities_keep_their_identity_providers(self):
        from agent.api.authorization import (
            COLLECTION_REQUEST_READ, DASHBOARD_READ, GOVERNANCE_WRITE,
            ONBOARDING_READ, ONBOARDING_WRITE,
        )

        for capability in (DASHBOARD_READ, GOVERNANCE_WRITE, COLLECTION_REQUEST_READ):
            self.assertEqual(capability.human_identities, frozenset({"github"}),
                             f"{capability.name} changed identity providers")
        for capability in (ONBOARDING_READ, ONBOARDING_WRITE):
            self.assertEqual(capability.human_identities, frozenset({"clerk"}))

    def test_a_machine_principal_declares_no_identity_provider(self):
        for scope in ("collector", "operator_read", "ci"):
            principal = _machine(scope)
            self.assertFalse(principal.is_human)
            self.assertIsNone(principal.identity_provider)


class CollectorTokenRegressionTests(unittest.TestCase):
    """A collector token has exactly the capabilities it had before Clerk."""

    def setUp(self):
        self.principal = _machine("collector")

    def test_it_still_holds_its_capabilities(self):
        from agent.api.authorization import (
            COLLECTION_REQUEST_READ, COLLECTOR_INGEST, PIPELINE_INGEST, authorize,
        )

        authorize(self.principal, COLLECTOR_INGEST)
        authorize(self.principal, PIPELINE_INGEST)
        authorize(self.principal, COLLECTION_REQUEST_READ)

    def test_it_still_lacks_the_ones_it_never_had(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, DASHBOARD_READ, GOVERNANCE_WRITE, ONBOARDING_READ,
            ONBOARDING_WRITE, CapabilityError, authorize,
        )

        for capability in (DASHBOARD_READ, GOVERNANCE_WRITE, CI_MANIFEST_INGEST,
                           ONBOARDING_READ, ONBOARDING_WRITE):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(self.principal, capability)


class CiTokenRegressionTests(unittest.TestCase):
    """A CI token has exactly the capabilities it had before Clerk."""

    def setUp(self):
        self.principal = _machine("ci", environment=None)

    def test_it_still_holds_manifest_ingest(self):
        from agent.api.authorization import CI_MANIFEST_INGEST, authorize

        authorize(self.principal, CI_MANIFEST_INGEST)

    def test_it_still_lacks_everything_else(self):
        from agent.api.authorization import (
            COLLECTION_REQUEST_READ, COLLECTOR_INGEST, DASHBOARD_READ,
            GOVERNANCE_WRITE, ONBOARDING_READ, ONBOARDING_WRITE, PIPELINE_INGEST,
            CapabilityError, authorize,
        )

        for capability in (COLLECTOR_INGEST, PIPELINE_INGEST, DASHBOARD_READ,
                           GOVERNANCE_WRITE, COLLECTION_REQUEST_READ,
                           ONBOARDING_READ, ONBOARDING_WRITE):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(self.principal, capability)


class OperatorReadTokenRegressionTests(unittest.TestCase):
    def setUp(self):
        self.principal = _machine("operator_read")

    def test_it_still_reads_the_dashboard_and_nothing_more(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTION_REQUEST_READ, COLLECTOR_INGEST,
            DASHBOARD_READ, GOVERNANCE_WRITE, ONBOARDING_READ, PIPELINE_INGEST,
            CapabilityError, authorize,
        )

        authorize(self.principal, DASHBOARD_READ)
        authorize(self.principal, COLLECTION_REQUEST_READ)
        for capability in (COLLECTOR_INGEST, PIPELINE_INGEST, CI_MANIFEST_INGEST,
                           GOVERNANCE_WRITE, ONBOARDING_READ):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(self.principal, capability)


class NoPrincipalMayImpersonateAnotherTests(unittest.TestCase):
    """The three principal kinds stay in their own lanes."""

    def test_a_clerk_human_cannot_use_any_machine_capability(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTOR_INGEST, PIPELINE_INGEST, CapabilityError,
            authorize,
        )

        principal = _clerk_human()
        for capability in (COLLECTOR_INGEST, PIPELINE_INGEST, CI_MANIFEST_INGEST):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(principal, capability)

    def test_a_github_human_cannot_impersonate_a_collector_or_ci_token(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTOR_INGEST, PIPELINE_INGEST, CapabilityError,
            authorize,
        )

        for principal in (_github_human(may_govern=True),
                          _github_human(may_govern=False)):
            for capability in (COLLECTOR_INGEST, PIPELINE_INGEST, CI_MANIFEST_INGEST):
                with self.assertRaises(CapabilityError, msg=capability.name):
                    authorize(principal, capability)

    def test_a_machine_cannot_acquire_a_capability_by_claiming_an_identity_provider(self):
        """A machine principal that grows an identity_provider attribute must
        still be refused: the human branch is gated on is_human, not on the
        provider."""
        from agent.api.authorization import (
            DASHBOARD_READ, GOVERNANCE_WRITE, ONBOARDING_WRITE, CapabilityError,
            authorize,
        )

        class _Liar:
            is_human = False
            identity_provider = "github"
            scope = "collector"
            may_govern = True

        for capability in (GOVERNANCE_WRITE, ONBOARDING_WRITE):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(_Liar(), capability)
        # DASHBOARD_READ does admit a machine, but only via operator_read.
        with self.assertRaises(CapabilityError):
            authorize(_Liar(), DASHBOARD_READ)

    def test_a_machine_scope_that_does_not_exist_grants_nothing(self):
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTOR_INGEST, DASHBOARD_READ, CapabilityError,
            authorize,
        )

        principal = _machine("invented_scope")
        for capability in (COLLECTOR_INGEST, CI_MANIFEST_INGEST, DASHBOARD_READ):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(principal, capability)


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; served-route regression needs PostgreSQL")
class ServiceTokenRouteRegressionTests(unittest.TestCase):
    """Existing service-token routes still authenticate service tokens.

    Clerk added its own authentication path. This asserts it did not intercept,
    shadow or alter the pre-existing one on the served application.
    """

    @classmethod
    def setUpClass(cls):
        import psycopg
        from starlette.testclient import TestClient

        from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)

        # Clerk IS configured here on purpose: the point is that a fully
        # Clerk-enabled deployment leaves service-token routes untouched.
        class _NoKeys:
            def key_for(self, kid):
                from agent.api.clerk_identity import ClerkVerificationError

                raise ClerkVerificationError("no keys in this test")

        cls.app = create_http_app(
            webhook_secret="regression-webhook-secret", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool,
            clerk_verifier=ClerkVerifier(
                ClerkSettings(issuer="https://regression.clerk.accounts.test",
                              jwks_url="https://regression.clerk.accounts.test/jwks"),
                jwks=_NoKeys()),
        )
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

        from agent.collector.provisioning import issue_collector_token, issue_ci_token

        with cls.pool.acquire() as store:
            store.ensure_tenant("acme", "analytics", "prod")
            _, cls.collector_token = issue_collector_token(
                store, organization_id="acme", repository_id="analytics",
                environment="prod")
            _, cls.ci_token = issue_ci_token(
                store, organization_id="acme", repository_id="analytics")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def _bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_a_collector_token_still_authenticates_on_an_existing_route(self):
        response = self.client.get("/api/collection-requests",
                                   headers=self._bearer(self.collector_token))
        self.assertNotIn(response.status_code, (401, 403),
                         f"collector token was refused: {response.text[:200]}")

    def test_a_ci_token_still_reaches_the_manifest_route(self):
        """Not a 401/403. A 422 here would mean it authenticated and the body
        was rejected, which is the pre-existing behaviour."""
        response = self.client.post("/api/manifest-evidence",
                                    headers=self._bearer(self.ci_token), json={})
        self.assertNotIn(response.status_code, (401, 403),
                         f"CI token was refused: {response.text[:200]}")

    def test_a_ci_token_is_still_refused_on_a_collector_route(self):
        """Scope separation predates Clerk and must survive it."""
        response = self.client.post("/api/monitoring/observations",
                                    headers=self._bearer(self.ci_token), json={})
        self.assertIn(response.status_code, (401, 403))

    def test_a_collector_token_is_still_refused_on_the_manifest_route(self):
        response = self.client.post("/api/manifest-evidence",
                                    headers=self._bearer(self.collector_token),
                                    json={})
        self.assertIn(response.status_code, (401, 403))

    def test_an_absent_credential_is_still_unauthorized(self):
        self.assertEqual(self.client.get("/api/collection-requests").status_code, 401)

    def test_a_service_token_is_refused_on_the_onboarding_routes(self):
        """The new routes must not accept the old credential."""
        for token in (self.collector_token, self.ci_token):
            self.assertEqual(
                self.client.get("/api/onboarding/state",
                                headers=self._bearer(token)).status_code, 401)
            self.assertEqual(
                self.client.put("/api/tenants", headers=self._bearer(token),
                                json={"organization_name": "Acme"}).status_code, 401)

    def test_the_webhook_still_uses_its_own_signature_authentication(self):
        response = self.client.post("/github/webhook", content=b"{}", headers={
            "X-Hub-Signature-256": "sha256=" + ("0" * 64),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "regression-delivery-1",
        })
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
