"""The served GitHub onboarding routes and the installation webhook.

Route-level counterpart to test_github_installation_binding.py: the same
guarantees, exercised through the real application over a real PostgreSQL, so
the wiring is covered and not just the service objects.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ISSUER = "https://routes-test.clerk.accounts.test"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
WEBHOOK_SECRET = "routes-test-webhook-secret"
APP_URL = "https://app.relium.test"
APP_ID = 900001
APP_SLUG = "relium-production-test"
INSTALLATION = 555001
ALICE_GITHUB_ID = 4242


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(document) -> str:
    return _b64url(json.dumps(document, separators=(",", ":")).encode("utf-8"))


class _Signer:
    def __init__(self, kid="routes-test-key"):
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
        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(),
                                          hashes.SHA256())
        return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


class _StubJwks:
    def __init__(self, signer):
        self._keys = {signer.kid: signer.private_key.public_key()}

    def key_for(self, kid):
        from agent.api.clerk_identity import ClerkVerificationError

        key = self._keys.get(kid)
        if key is None:
            raise ClerkVerificationError("token key is not recognised")
        return key


class _FakeClient:
    def get_app(self, app_jwt):
        return {"id": APP_ID, "slug": APP_SLUG, "name": "Relium"}

    def get_installation(self, installation_id, app_jwt):
        from agent.github_app.client import GitHubNotFoundError

        if installation_id != INSTALLATION:
            raise GitHubNotFoundError("installation not found")
        return {"id": INSTALLATION, "app_id": APP_ID,
                "repository_selection": "selected",
                "account": {"id": 5001, "login": "acme-analytics",
                            "type": "Organization"}}


class _FakeIdentity:
    def __init__(self):
        self.access = {"alice-token": {INSTALLATION}}

    def user_can_access_installation(self, access_token, installation_id):
        return installation_id in self.access.get(access_token, set())

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


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; served routes need PostgreSQL")
class GitHubOnboardingRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
        from agent.api.github_installation import (
            GitHubAppIdentity, GitHubIdentityLinker, InstallationBinder,
        )
        from agent.api.pool import StorePool
        from agent.api.session_crypto import generate_key, load_key
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.signer = _Signer()
        cls.key = load_key(generate_key())
        cls.client_stub = _FakeClient()
        cls.identity = _FakeIdentity()
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)

        app_identity = GitHubAppIdentity(cls.client_stub, lambda: "app-jwt",
                                         clock=lambda: NOW)
        cls.binder = InstallationBinder(
            app_identity=app_identity, client=cls.client_stub,
            jwt_factory=lambda: "app-jwt", session_key=cls.key,
            github_identity=cls.identity, clock=lambda: NOW)
        cls.linker = GitHubIdentityLinker(
            client_id="client", client_secret="secret",
            redirect_uri=f"{APP_URL}/auth/github/link/callback",
            session_key=cls.key, github_identity=cls.identity,
            clock=lambda: NOW)

        cls.app = create_http_app(
            webhook_secret=WEBHOOK_SECRET, job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool,
            clerk_verifier=ClerkVerifier(
                ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks"),
                jwks=_StubJwks(cls.signer), clock=lambda: NOW),
            installation_binder=cls.binder, identity_linker=cls.linker,
            app_url=APP_URL)
        cls.http = TestClient(cls.app, follow_redirects=False)
        cls.http.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        from agent.api.session_crypto import encrypt

        with self.pool.acquire() as store:
            store.connection.execute("DELETE FROM tenant_github_installations")
            store.connection.execute("DELETE FROM github_installations")
            store.connection.execute("DELETE FROM github_installation_states")
            store.connection.execute("DELETE FROM clerk_github_identities")
            store.connection.execute("DELETE FROM tenants")
            store.upsert_tenant_for_clerk_organization(
                "org_2acme", organization_name="Acme")
            store.upsert_clerk_github_identity(
                "user_alice", github_user_id=ALICE_GITHUB_ID,
                github_login="alice",
                access_token=encrypt(self.key, "alice-token",
                                     associated="user_alice"))
        self.identity.access = {"alice-token": {INSTALLATION}}

    def _auth(self, **claims):
        claims.setdefault("org_id", "org_2acme")
        return {"Authorization": f"Bearer {self.signer.token(**claims)}"}

    def _start_install(self, **claims):
        import urllib.parse

        response = self.http.post("/api/onboarding/github/install",
                                  headers=self._auth(**claims))
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["install_url"]
        query = urllib.parse.urlparse(url).query
        return urllib.parse.parse_qs(query)["state"][0]

    def _bindings(self):
        with self.pool.acquire() as store:
            return store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenant_github_installations"
            ).fetchone()["c"]

    # -- install start ------------------------------------------------------

    def test_install_start_requires_a_clerk_session(self):
        self.assertEqual(
            self.http.post("/api/onboarding/github/install").status_code, 401)

    def test_install_start_refuses_a_service_token(self):
        response = self.http.post(
            "/api/onboarding/github/install",
            headers={"Authorization": "Bearer rlm_deadbeef.some-secret"})
        self.assertEqual(response.status_code, 401)

    def test_install_start_returns_the_apps_own_slug(self):
        response = self.http.post("/api/onboarding/github/install",
                                  headers=self._auth())
        self.assertEqual(response.status_code, 200)
        url = response.json()["install_url"]
        self.assertIn(f"github.com/apps/{APP_SLUG}/installations/new", url)
        self.assertNotIn("relium-e2e", url)

    def test_install_start_requires_a_workspace(self):
        response = self.http.post("/api/onboarding/github/install",
                                  headers=self._auth(org_id="org_2nothing"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "workspace_required")

    def test_install_start_requires_a_linked_github_identity(self):
        """Told before the customer installs, not after."""
        with self.pool.acquire() as store:
            store.connection.execute("DELETE FROM clerk_github_identities")
        response = self.http.post("/api/onboarding/github/install",
                                  headers=self._auth())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "github_identity_required")

    # -- the setup redirect -------------------------------------------------

    def test_a_verified_redirect_binds_and_returns_to_the_app(self):
        state = self._start_install()
        response = self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state,
            "setup_action": "install"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("github_installed=1", response.headers["location"])
        self.assertEqual(self._bindings(), 1)

    def test_a_redirect_with_no_state_binds_nothing(self):
        """The bare spoof: an installation id and nothing else."""
        response = self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "setup_action": "install"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("github_error=installation_state_invalid",
                      response.headers["location"])
        self.assertEqual(self._bindings(), 0)

    def test_a_redirect_with_a_forged_installation_id_binds_nothing(self):
        state = self._start_install()
        self.identity.access = {"alice-token": set()}   # Alice cannot see it
        response = self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        self.assertIn("github_error=installation_not_authorized",
                      response.headers["location"])
        self.assertEqual(self._bindings(), 0)

    def test_a_redirect_naming_an_unknown_installation_binds_nothing(self):
        state = self._start_install()
        response = self.http.get("/github/setup", params={
            "installation_id": "99999999", "state": state})
        self.assertIn("github_error=installation_unknown",
                      response.headers["location"])
        self.assertEqual(self._bindings(), 0)

    def test_a_non_numeric_installation_id_binds_nothing(self):
        state = self._start_install()
        for hostile in ("abc", "1; DROP TABLE tenants", "-1", "0",
                        "9" * 40, "1e5", " 555001 ", ""):
            response = self.http.get("/github/setup", params={
                "installation_id": hostile, "state": state})
            self.assertEqual(response.status_code, 302, hostile)
        self.assertEqual(self._bindings(), 0)

    def test_the_redirect_never_echoes_the_state(self):
        """A state reflected into a URL would land in logs and history."""
        state = self._start_install()
        response = self.http.get("/github/setup", params={
            "installation_id": "99999999", "state": state})
        self.assertNotIn(state, response.headers["location"])

    def test_a_pending_approval_is_not_reported_as_a_failure(self):
        """setup_action=request means an owner must still approve. Real,
        common, and not an error."""
        state = self._start_install()
        response = self.http.get("/github/setup", params={
            "state": state, "setup_action": "request"})
        self.assertIn("github_pending=approval", response.headers["location"])
        self.assertEqual(self._bindings(), 0)

    def test_the_redirect_target_stays_on_the_app_origin(self):
        state = self._start_install()
        response = self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        self.assertTrue(response.headers["location"].startswith(APP_URL),
                        response.headers["location"])

    # -- state reuse through the route --------------------------------------

    def test_a_state_cannot_be_replayed_through_the_route(self):
        state = self._start_install()
        self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        response = self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        self.assertIn("github_error=installation_state_invalid",
                      response.headers["location"])
        self.assertEqual(self._bindings(), 1)

    # -- onboarding state ---------------------------------------------------

    def test_onboarding_state_reports_the_binding(self):
        state = self._start_install()
        self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        body = self.http.get("/api/onboarding/state", headers=self._auth()).json()
        self.assertEqual(body["github"]["status"], "connected")
        self.assertEqual(len(body["github"]["installations"]), 1)
        self.assertEqual(body["github"]["installations"][0]["account_login"],
                         "acme-analytics")
        self.assertIs(body["github"]["identity"]["linked"], True)

    def test_onboarding_state_never_exposes_a_credential(self):
        state = self._start_install()
        self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        text = self.http.get("/api/onboarding/state", headers=self._auth()).text
        for forbidden in ("alice-token", "app-jwt", "access_token"):
            self.assertNotIn(forbidden, text)

    def test_another_tenant_does_not_see_the_installation(self):
        state = self._start_install()
        self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        with self.pool.acquire() as store:
            store.upsert_tenant_for_clerk_organization(
                "org_2globex", organization_name="Globex")
        body = self.http.get("/api/onboarding/state",
                             headers=self._auth(org_id="org_2globex")).json()
        self.assertEqual(body["github"]["status"], "not_connected")
        self.assertEqual(body["github"]["installations"], [])

    # -- the webhook --------------------------------------------------------

    def _deliver(self, event, payload, *, delivery, secret=WEBHOOK_SECRET):
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body,
                             hashlib.sha256).hexdigest()
        return self.http.post("/github/webhook", content=body, headers={
            "X-Hub-Signature-256": f"sha256={signature}",
            "X-GitHub-Event": event, "X-GitHub-Delivery": delivery})

    def _installation_payload(self, action, **overrides):
        payload = {
            "action": action,
            "installation": {
                "id": INSTALLATION, "app_id": APP_ID,
                "repository_selection": "selected",
                "account": {"id": 5001, "login": "acme-analytics",
                            "type": "Organization"},
            },
            "sender": {"login": "alice"},
        }
        payload.update(overrides)
        return payload

    def test_an_installation_webhook_records_facts_but_no_tenant(self):
        response = self._deliver("installation",
                                 self._installation_payload("created"),
                                 delivery="d-created-1")
        self.assertEqual(response.status_code, 202)
        with self.pool.acquire() as store:
            self.assertIsNotNone(store.github_installation(INSTALLATION))
            self.assertIsNone(
                store.tenant_for_github_installation(INSTALLATION))
        self.assertEqual(self._bindings(), 0)

    def test_a_replayed_delivery_does_not_duplicate_the_row(self):
        for index in range(4):
            self._deliver("installation", self._installation_payload("created"),
                          delivery=f"d-replay-{index}")
        with self.pool.acquire() as store:
            count = store.connection.execute(
                "SELECT COUNT(*) AS c FROM github_installations").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_an_invalid_signature_is_refused(self):
        response = self._deliver("installation",
                                 self._installation_payload("created"),
                                 delivery="d-bad-sig", secret="wrong-secret")
        self.assertEqual(response.status_code, 401)
        with self.pool.acquire() as store:
            self.assertIsNone(store.github_installation(INSTALLATION))

    def test_suspension_and_unsuspension_are_recorded(self):
        self._deliver("installation", self._installation_payload("created"),
                      delivery="d-1")
        self._deliver("installation", self._installation_payload("suspend"),
                      delivery="d-2")
        with self.pool.acquire() as store:
            self.assertEqual(
                store.github_installation(INSTALLATION)["status"], "suspended")
        self._deliver("installation", self._installation_payload("unsuspend"),
                      delivery="d-3")
        with self.pool.acquire() as store:
            self.assertEqual(
                store.github_installation(INSTALLATION)["status"], "active")

    def test_deletion_is_a_soft_delete(self):
        self._deliver("installation", self._installation_payload("created"),
                      delivery="d-1")
        self._deliver("installation", self._installation_payload("deleted"),
                      delivery="d-2")
        with self.pool.acquire() as store:
            record = store.github_installation(INSTALLATION)
        self.assertEqual(record["status"], "deleted")
        self.assertIsNotNone(record["deleted_at"])

    def test_webhook_first_then_redirect_binds_once(self):
        """The order the real world usually produces."""
        self._deliver("installation", self._installation_payload("created"),
                      delivery="d-order-1")
        state = self._start_install()
        self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        self.assertEqual(self._bindings(), 1)

    def test_redirect_first_then_webhook_binds_once(self):
        state = self._start_install()
        self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state})
        self._deliver("installation", self._installation_payload("created"),
                      delivery="d-order-2")
        self.assertEqual(self._bindings(), 1)
        with self.pool.acquire() as store:
            self.assertEqual(
                store.tenant_for_github_installation(INSTALLATION),
                store.tenant_by_clerk_organization("org_2acme")["tenant_id"])

    def test_a_repository_selection_delivery_for_an_unknown_installation_creates_nothing(self):
        """An installation nobody has verified must not spring into existence
        because a selection changed."""
        self._deliver("installation_repositories",
                      self._installation_payload("added"),
                      delivery="d-repos-unknown")
        with self.pool.acquire() as store:
            self.assertIsNone(store.github_installation(INSTALLATION))

    def test_an_unsupported_installation_action_is_ignored(self):
        response = self._deliver(
            "installation", self._installation_payload("new_permissions_accepted"),
            delivery="d-ignored")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "ignored")

    def test_a_malformed_installation_payload_is_refused(self):
        response = self._deliver("installation",
                                 {"action": "created", "installation": {}},
                                 delivery="d-malformed")
        self.assertEqual(response.status_code, 400)

    def test_a_pull_request_delivery_still_reaches_the_queue(self):
        """The pre-existing path must be untouched."""
        payload = {
            "action": "opened",
            "repository": {"id": 1, "name": "analytics",
                           "full_name": "acme/analytics",
                           "owner": {"login": "acme"}},
            "installation": {"id": INSTALLATION},
            "pull_request": {"number": 7, "head": {"sha": "a" * 40},
                             "base": {"sha": "b" * 40}},
            "sender": {"login": "alice"},
        }
        response = self._deliver("pull_request", payload, delivery="d-pr-1")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class UnconfiguredGitHubTests(unittest.TestCase):
    """No GitHub App configured: routes served, nothing bound."""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=2)
        cls.app = create_http_app(
            webhook_secret="unconfigured", job_queue=_StubQueue(),
            max_body_bytes=1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool)
        cls.http = TestClient(cls.app, follow_redirects=False)
        cls.http.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        cls.pool.close()

    def test_the_routes_are_still_served(self):
        from agent.api.contract import served_routes

        served = {(e["method"], e["path"]) for e in served_routes(self.app)}
        for route in (("POST", "/api/onboarding/github/install"),
                      ("POST", "/api/onboarding/github/identity"),
                      ("GET", "/github/setup"),
                      ("GET", "/auth/github/link/callback")):
            self.assertIn(route, served)

    def test_the_setup_redirect_binds_nothing(self):
        response = self.http.get("/github/setup", params={
            "installation_id": "12345", "state": "anything"})
        self.assertEqual(response.status_code, 503)

    def test_install_start_reports_unavailable(self):
        response = self.http.post("/api/onboarding/github/install",
                                  headers={"Authorization": "Bearer anything"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
