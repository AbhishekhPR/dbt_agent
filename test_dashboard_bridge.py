"""Entering the dashboard after onboarding — Phase 6 security properties.

The claim under test:

    A Clerk session does not become dashboard access. The dashboard session is
    minted only when GitHub, asked live and as that human, confirms access to
    the tenant's own repository — and it carries exactly the authority GitHub
    reports, no more.

Real PostgreSQL and the real served application. GitHub is scripted so a
read-only user, a removed user and an outage can each be produced deliberately.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ISSUER = "https://bridge-test.clerk.accounts.test"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
INSTALLATION = 660001
REPO_ID = 770001
OTHER_REPO_ID = 770002
ALICE_GITHUB_ID = 4242

ADMIN = {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True}
WRITE = {"admin": False, "maintain": False, "push": True, "triage": True, "pull": True}
READ = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}
NONE = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": False}


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(doc):
    return _b64url(json.dumps(doc, separators=(",", ":")).encode("utf-8"))


class _Signer:
    def __init__(self, kid="bridge-key"):
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537,
                                                    key_size=2048)

    def token(self, **claims):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = {"iss": ISSUER, "sub": "user_alice", "sid": "sess_1",
                   "exp": int((NOW + timedelta(minutes=10)).timestamp()),
                   "iat": int(NOW.timestamp())}
        payload.update({k: v for k, v in claims.items() if v is not None})
        header = {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        signing = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
        return (f"{signing.decode('ascii')}."
                f"{_b64url(self.private_key.sign(signing, padding.PKCS1v15(), hashes.SHA256()))}")


class _StubJwks:
    def __init__(self, signer):
        self._keys = {signer.kid: signer.private_key.public_key()}

    def key_for(self, kid):
        from agent.api.clerk_identity import ClerkVerificationError

        key = self._keys.get(kid)
        if key is None:
            raise ClerkVerificationError("unknown key")
        return key


class _FakeClient:
    def with_token(self, token):
        return self

    def get_app(self, app_jwt):
        return {"id": 1, "slug": "relium-bridge-test"}

    def get_installation(self, installation_id, app_jwt):
        from agent.github_app.client import GitHubNotFoundError

        if installation_id != INSTALLATION:
            raise GitHubNotFoundError("unknown")
        return {"id": INSTALLATION, "app_id": 1,
                "account": {"id": 1, "login": "acme", "type": "Organization"}}

    def list_installation_repositories(self, token, *, page=1, per_page=100):
        if page > 1:
            return {"total_count": 1, "repositories": []}
        return {"total_count": 1, "repositories": [
            {"id": REPO_ID, "name": "analytics", "private": True,
             "default_branch": "main", "owner": {"login": "acme"}}]}

    def get_file(self, owner, repository, path, ref):
        return b"name: analytics\n"


class _FakeIdentity:
    """GitHub, as a user. Permissions are per (token, owner/repo)."""

    def __init__(self):
        self.permissions = {("alice-token", "acme", "analytics"): ADMIN}
        self.viewer = {"login": "alice", "user_id": ALICE_GITHUB_ID}
        self.unavailable = False

    def fetch_viewer(self, access_token, **kwargs):
        from agent.api.github_identity import GitHubIdentityError

        if self.unavailable:
            raise GitHubIdentityError("unreachable")
        return self.viewer

    def fetch_repository_permissions(self, access_token, owner, repository,
                                     **kwargs):
        from agent.api.github_identity import GitHubIdentityError

        if self.unavailable:
            raise GitHubIdentityError("unreachable")
        return self.permissions.get((access_token, owner, repository))

    def user_can_access_installation(self, access_token, installation_id):
        return installation_id == INSTALLATION

    def authorize_url(self, client_id, redirect_uri, state):
        return f"https://github.com/login/oauth/authorize?state={state}"


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the bridge needs PostgreSQL")
class DashboardBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from starlette.testclient import TestClient

        from agent.api.auth_routes import create_auth_routes
        from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
        from agent.api.dashboard_bridge import DashboardSessionBridge
        from agent.api.pool import StorePool
        from agent.api.repository_onboarding import RepositoryOnboardingService
        from agent.api.session_crypto import generate_key, load_key
        from agent.api.sessions import SessionManager
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        cls.signer = _Signer()
        cls.key = load_key(generate_key())
        cls.identity = _FakeIdentity()
        cls.github = _FakeClient()
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)

        # Movable, so the existing permission TTL can actually be crossed.
        cls.now = [NOW]
        cls.sessions = SessionManager(
            client_id="c", client_secret="s", encryption_key=cls.key,
            identity=cls.identity, clock=lambda: cls.now[0])
        cls.repositories = RepositoryOnboardingService(
            client=cls.github, jwt_factory=lambda: "app-jwt",
            installation_token_factory=lambda i: f"tok-{i}", clock=lambda: NOW)
        cls.bridge = DashboardSessionBridge(
            session_manager=cls.sessions, session_key=cls.key,
            repository_service=cls.repositories, environment="production",
            github_identity=cls.identity)

        cls.app = create_http_app(
            webhook_secret="bridge-secret", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool,
            clerk_verifier=ClerkVerifier(
                ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks"),
                jwks=_StubJwks(cls.signer), clock=lambda: NOW),
            session_manager=cls.sessions,
            auth_routes=create_auth_routes(
                store_pool=cls.pool, session_manager=cls.sessions,
                dashboard_url="https://app.relium.test",
                callback_url="https://api.relium.test/auth/github/callback",
                organization_id="acme", repository_id="analytics",
                environment="production", secure_cookies=False),
            repository_service=cls.repositories,
            dashboard_bridge=cls.bridge,
            # http in tests, so Secure would prevent the client storing the
            # cookie at all. The flag itself is asserted from headers below.
            secure_cookies=False)
        cls.http = TestClient(cls.app, follow_redirects=False)
        cls.http.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        from agent.api.session_crypto import encrypt

        self.http.cookies.clear()
        type(self).now[0] = NOW
        self.identity.permissions = {("alice-token", "acme", "analytics"): ADMIN}
        self.identity.unavailable = False

        with self.pool.acquire() as store:
            for table in ("dashboard_sessions", "tenant_repositories",
                          "tenant_github_installations", "github_installations",
                          "github_installation_states", "clerk_github_identities",
                          "api_service_tokens", "tenant_onboarding_state",
                          "tenants"):
                store.connection.execute(f"DELETE FROM {table}")

            self.acme = store.upsert_tenant_for_clerk_organization(
                "org_2acme", organization_name="Acme")["tenant_id"]
            self.globex = store.upsert_tenant_for_clerk_organization(
                "org_2globex", organization_name="Globex")["tenant_id"]
            store.upsert_clerk_github_identity(
                "user_alice", github_user_id=ALICE_GITHUB_ID,
                github_login="alice",
                access_token=encrypt(self.key, "alice-token",
                                     associated="user_alice"))

    def _auth(self, **claims):
        claims.setdefault("org_id", "org_2acme")
        return {"Authorization": f"Bearer {self.signer.token(**claims)}"}

    def _onboard(self, tenant_id=None):
        """Bring a tenant to completed, entirely through the store."""
        tenant_id = tenant_id or self.acme
        with self.pool.acquire() as store:
            store.record_github_installation(
                INSTALLATION, github_account_id=1, github_account_login="acme",
                github_account_type="Organization")
            store.bind_github_installation_to_tenant(
                INSTALLATION, tenant_id=tenant_id,
                bound_by_clerk_user_id="user_alice",
                verified_github_user_id=ALICE_GITHUB_ID)
            store.select_tenant_repository(
                REPO_ID, tenant_id=tenant_id, github_installation_id=INSTALLATION,
                owner_login="acme", name="analytics", default_branch="main")
            store.configure_tenant_repository(
                REPO_ID, tenant_id=tenant_id, project_dir=".",
                manifest_path="target/manifest.json", enforcement_mode="shadow",
                configured_at=NOW)
            store.record_tenant_repository_ci_token(
                REPO_ID, tenant_id=tenant_id, ci_token_id="tok-1",
                delivery="display_once", issued_at=NOW)
            store.complete_tenant_onboarding(
                tenant_id, completed_at=NOW, repository_id=REPO_ID,
                clerk_user_id="user_alice")

    def _establish(self, **claims):
        return self.http.post("/api/onboarding/dashboard-session",
                              headers=self._auth(**claims), json={})

    # -- the happy path -----------------------------------------------------

    def test_a_completed_tenant_can_enter_the_dashboard(self):
        self._onboard()
        response = self._establish()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["established"], True)

        # And the ordinary session endpoint now recognises them.
        session = self.http.get("/auth/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["repository"], "acme/analytics")

    def test_the_scope_comes_from_the_tenant_not_configuration(self):
        """The pilot's configured repository is not in the access path."""
        self._onboard()
        self._establish()
        self.assertEqual(
            self.http.get("/auth/session").json()["repository"], "acme/analytics")

    def test_the_dashboard_api_is_reachable_afterwards(self):
        self._onboard()
        self._establish()
        response = self.http.get("/api/reviews")
        self.assertNotIn(response.status_code, (401, 403), response.text[:200])

    # -- authority is GitHub's, not Clerk's ---------------------------------

    def test_a_read_only_github_user_gets_read_but_not_governance(self):
        """The central authority claim."""
        self.identity.permissions = {("alice-token", "acme", "analytics"): READ}
        self._onboard()
        self._establish()

        session = self.http.get("/auth/session").json()
        self.assertIs(session["may_govern"], False)
        self.assertEqual(session["github_permission"], "pull")

        # Read works; governance does not.
        self.assertNotIn(self.http.get("/api/reviews").status_code, (401, 403))
        governance = self.http.post(
            "/api/reviews/rev-1/exceptions",
            headers={"X-Relium-CSRF": self.http.cookies.get("relium_csrf") or ""},
            json={"reason": "no"})
        self.assertIn(governance.status_code, (403, 404, 422))

    def test_write_permission_grants_governance_only_when_github_confirms(self):
        for permissions, expected in ((ADMIN, True), (WRITE, True), (READ, False)):
            with self.subTest(permissions=permissions):
                self.setUp()
                self.identity.permissions = {
                    ("alice-token", "acme", "analytics"): permissions}
                self._onboard()
                self._establish()
                self.assertIs(
                    self.http.get("/auth/session").json()["may_govern"], expected)

    def test_a_user_without_repository_access_gets_no_session(self):
        """Their tenant owning the repository changes nothing."""
        self.identity.permissions = {("alice-token", "acme", "analytics"): NONE}
        self._onboard()
        response = self._establish()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"],
                         "github_repository_access_required")
        self.assertEqual(self.http.get("/auth/session").status_code, 401)

    def test_a_user_github_does_not_know_gets_no_session(self):
        self.identity.permissions = {}
        self._onboard()
        self.assertEqual(self._establish().status_code, 409)

    def test_losing_github_access_fails_once_the_permission_goes_stale(self):
        """The EXISTING re-verification model still governs, unchanged.

        A read reuses a verified permission for PERMISSION_TTL; only a
        governance write forces a live check. The bridge does not alter either,
        which is the point — it creates the same session, so it inherits the
        same behaviour rather than defining its own.
        """
        from agent.api.sessions import PERMISSION_TTL

        self._onboard()
        self._establish()
        self.assertNotIn(self.http.get("/api/reviews").status_code, (401, 403))

        # Access removed at GitHub. Within the TTL a read still succeeds --
        # existing, deliberate behaviour.
        self.identity.permissions = {}
        self.assertEqual(self.http.get("/auth/session").status_code, 200)

        # Past the TTL the permission is re-verified, and the session ends.
        type(self).now[0] = NOW + PERMISSION_TTL + timedelta(seconds=1)
        self.assertEqual(self.http.get("/auth/session").status_code, 401)
        self.assertIn(self.http.get("/api/reviews").status_code, (401, 403))

    # -- the chain refuses at every missing link ----------------------------

    def test_incomplete_onboarding_cannot_enter_the_dashboard(self):
        response = self._establish()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "onboarding_incomplete")

    def test_a_missing_github_identity_fails(self):
        self._onboard()
        with self.pool.acquire() as store:
            store.connection.execute("DELETE FROM clerk_github_identities")
        response = self._establish()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "github_identity_required")

    def test_a_revoked_github_credential_fails(self):
        self._onboard()
        with self.pool.acquire() as store:
            store.revoke_clerk_github_identity("user_alice")
        self.assertEqual(self._establish().json()["code"],
                         "github_identity_required")

    def test_an_expired_github_credential_fails(self):
        from agent.api.session_crypto import encrypt

        self._onboard()
        with self.pool.acquire() as store:
            store.upsert_clerk_github_identity(
                "user_alice", github_user_id=ALICE_GITHUB_ID,
                github_login="alice",
                access_token=encrypt(self.key, "alice-token",
                                     associated="user_alice"),
                access_expires_at=NOW - timedelta(minutes=1))
        self.assertEqual(self._establish().json()["code"],
                         "github_identity_unusable")

    def test_a_removed_installation_fails(self):
        self._onboard()
        with self.pool.acquire() as store:
            store.set_github_installation_status(
                INSTALLATION, "deleted", deleted_at=NOW)
        self.assertEqual(self._establish().json()["code"],
                         "github_installation_required")

    def test_github_unavailable_does_not_mint_a_session(self):
        self._onboard()
        self.identity.unavailable = True
        self.assertEqual(self._establish().status_code, 409)
        self.assertEqual(self.http.get("/auth/session").status_code, 401)

    # -- cross-tenant and substitution --------------------------------------

    def test_a_wrong_clerk_organization_gets_its_own_empty_tenant(self):
        """Globex is set up; Alice's token names Acme. She gets Acme's answer,
        which is 'not onboarded' — never Globex's session."""
        self._onboard(self.globex)
        response = self._establish(org_id="org_2acme")
        self.assertEqual(response.json()["code"], "onboarding_incomplete")
        self.assertEqual(self.http.get("/auth/session").status_code, 401)

    def test_a_tenant_cannot_obtain_a_session_scoped_to_another(self):
        self._onboard(self.acme)
        # Globex has a workspace but no installation or repository of its own.
        response = self._establish(org_id="org_2globex")
        self.assertEqual(response.status_code, 409)
        self.assertNotEqual(response.json()["code"], "established")
        self.assertEqual(self.http.get("/auth/session").status_code, 401)

    def test_the_request_body_cannot_substitute_a_repository_or_tenant(self):
        """The endpoint takes no input, so there is nothing to substitute."""
        self._onboard()
        response = self.http.post(
            "/api/onboarding/dashboard-session", headers=self._auth(),
            json={"repository_id": OTHER_REPO_ID, "tenant_id": self.globex,
                  "installation_id": 999999, "owner": "attacker",
                  "repository": "evil"})
        self.assertEqual(response.status_code, 200)
        # Scope still came from the tenant's own record.
        self.assertEqual(
            self.http.get("/auth/session").json()["repository"], "acme/analytics")

    def test_a_repository_removed_from_the_installation_stops_working(self):
        """Previously known is not the same as currently authorised."""
        self._onboard()
        original = self.github.list_installation_repositories
        self.github.list_installation_repositories = (
            lambda token, *, page=1, per_page=100: {"total_count": 0,
                                                    "repositories": []})
        try:
            self.assertEqual(self._establish().json()["code"],
                             "repository_not_configured")
        finally:
            self.github.list_installation_repositories = original

    # -- session properties -------------------------------------------------

    def test_the_session_cookie_is_httponly_and_samesite_lax(self):
        self._onboard()
        response = self._establish()
        headers = response.headers.get_list("set-cookie")
        session_cookie = [h for h in headers if h.startswith("relium_session=")]
        self.assertEqual(len(session_cookie), 1)
        self.assertIn("HttpOnly", session_cookie[0])
        self.assertIn("SameSite=lax", session_cookie[0])

    def test_the_session_cookie_is_secure_when_configured(self):
        """Asserted on its own app, because a test client over http will not
        store a Secure cookie -- which is exactly the behaviour we want in a
        browser, and exactly why it cannot be asserted inline."""
        from starlette.testclient import TestClient

        from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
        from agent.github_app.http_app import create_http_app

        secure_app = create_http_app(
            webhook_secret="s", job_queue=_StubQueue(), max_body_bytes=65536,
            shutdown_timeout_seconds=1.0, clock=lambda: 0.0,
            store_pool=self.pool,
            clerk_verifier=ClerkVerifier(
                ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks"),
                jwks=_StubJwks(self.signer), clock=lambda: NOW),
            session_manager=self.sessions,
            repository_service=self.repositories,
            dashboard_bridge=self.bridge, secure_cookies=True)

        self._onboard()
        with TestClient(secure_app) as client:
            response = client.post("/api/onboarding/dashboard-session",
                                   headers=self._auth(), json={})
            cookie = [h for h in response.headers.get_list("set-cookie")
                      if h.startswith("relium_session=")][0]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_the_csrf_cookie_is_readable_and_the_session_is_not(self):
        self._onboard()
        response = self._establish()
        headers = " ".join(response.headers.get_list("set-cookie"))
        csrf = [h for h in response.headers.get_list("set-cookie")
                if h.startswith("relium_csrf=")]
        self.assertEqual(len(csrf), 1)
        self.assertNotIn("HttpOnly", csrf[0])
        self.assertIn("relium_session", headers)

    def test_the_response_carries_no_credential(self):
        self._onboard()
        text = self._establish().text
        for forbidden in ("alice-token", "app-jwt", "access_token",
                          "session_id", "csrf_token", self.acme):
            self.assertNotIn(forbidden, text)

    def test_the_session_row_holds_no_plaintext_github_token(self):
        self._onboard()
        self._establish()
        with self.pool.acquire() as store:
            row = store.connection.execute(
                "SELECT github_access_token FROM dashboard_sessions").fetchone()
        self.assertIsNotNone(row["github_access_token"])
        self.assertNotIn(b"alice-token", bytes(row["github_access_token"]))

    def test_no_clerk_token_appears_in_any_url(self):
        self._onboard()
        response = self._establish()
        self.assertNotIn("Bearer", str(response.url))
        self.assertNotIn("eyJ", str(response.url))

    # -- idempotency, concurrency, sign-out ---------------------------------

    def test_establishing_twice_is_safe(self):
        self._onboard()
        first = self._establish()
        second = self._establish()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # The session rotates rather than duplicating a usable identity.
        self.assertEqual(self.http.get("/auth/session").status_code, 200)

    def test_concurrent_establishment_is_safe(self):
        self._onboard()
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def attempt():
            try:
                barrier.wait(timeout=30)
                response = self.http.post("/api/onboarding/dashboard-session",
                                          headers=self._auth(), json={})
                with lock:
                    outcomes.append(response.status_code)
            except Exception as exc:
                with lock:
                    outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(outcomes, [200] * 5, outcomes)

    def test_sign_out_invalidates_the_dashboard_session(self):
        self._onboard()
        self._establish()
        self.assertEqual(self.http.get("/auth/session").status_code, 200)

        self.http.post("/auth/logout")
        self.assertEqual(self.http.get("/auth/session").status_code, 401)

    def test_a_returning_user_re_establishes_without_re_onboarding(self):
        self._onboard()
        self._establish()
        self.http.post("/auth/logout")

        # Same tenant, same everything. No new records.
        with self.pool.acquire() as store:
            before = store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenants").fetchone()["c"]
        self.assertEqual(self._establish().status_code, 200)
        with self.pool.acquire() as store:
            after = store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenants").fetchone()["c"]
        self.assertEqual(before, after)

    def test_an_unauthenticated_request_is_refused(self):
        self._onboard()
        self.assertEqual(
            self.http.post("/api/onboarding/dashboard-session",
                           json={}).status_code, 401)

    def test_a_service_token_cannot_mint_a_dashboard_session(self):
        self._onboard()
        response = self.http.post(
            "/api/onboarding/dashboard-session",
            headers={"Authorization": "Bearer rlm_deadbeef.secret"}, json={})
        self.assertEqual(response.status_code, 401)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ExistingDashboardPathRegressionTests(unittest.TestCase):
    """The GitHub sign-in path must behave exactly as it did before.

    The bridge added a second way to CREATE the session. It must not have
    changed what a session IS, what it authorises, or how it is verified.
    """

    def test_the_capability_model_is_untouched(self):
        from agent.api.authorization import (
            DASHBOARD_READ, GOVERNANCE_WRITE, CapabilityError, authorize,
        )
        from agent.api.sessions import HumanPrincipal

        governing = HumanPrincipal(
            organization_id="acme", repository_id="analytics",
            environment="production", github_login="maya",
            github_permission="push", may_govern=True, session_id_hash="h")
        reader = HumanPrincipal(
            organization_id="acme", repository_id="analytics",
            environment="production", github_login="sam",
            github_permission="pull", may_govern=False, session_id_hash="h")

        authorize(governing, DASHBOARD_READ)
        authorize(governing, GOVERNANCE_WRITE)
        authorize(reader, DASHBOARD_READ)
        with self.assertRaises(CapabilityError):
            authorize(reader, GOVERNANCE_WRITE)

    def test_a_bridged_session_is_the_same_principal_shape(self):
        """No new principal type, no new field, no new authority."""
        from agent.api.sessions import HumanPrincipal

        self.assertEqual(HumanPrincipal.identity_provider, "github")
        self.assertIs(HumanPrincipal.is_human, True)
        self.assertEqual(HumanPrincipal.scope, "human")

    def test_the_bridge_writes_the_same_columns_as_github_sign_in(self):
        """One session system, not two."""
        import inspect

        from agent.api.sessions import SessionManager

        bridged = inspect.getsource(
            SessionManager.establish_from_verified_identity)
        for column in ("organization_id", "repository_id", "environment",
                       "github_login", "github_user_id", "github_permission",
                       "may_govern", "permission_checked_at", "csrf_token",
                       "expires_at", "github_access_token"):
            self.assertIn(column, bridged, column)

    def test_the_bridge_does_not_bypass_the_permission_check(self):
        import inspect

        from agent.api.sessions import SessionManager

        source = inspect.getsource(SessionManager.establish_from_verified_identity)
        self.assertIn("fetch_repository_permissions", source)
        self.assertIn("may_read(permissions)", source)
        self.assertIn("may_govern(permissions)", source)


if __name__ == "__main__":
    unittest.main()
