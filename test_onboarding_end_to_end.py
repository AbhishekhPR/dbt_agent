"""The complete first-user onboarding flow, over the served application.

    Clerk sign-in -> active Clerk organization -> Relium tenant
      -> GitHub installation -> repository -> dbt config -> CI setup
        -> onboarding complete -> dashboard

Every step goes through the real routes, the real authorization, and a real
PostgreSQL. GitHub and Clerk are scripted so the whole path can run without a
network, but nothing in the chain under test is stubbed.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import base64
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ISSUER = "https://e2e-test.clerk.accounts.test"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
APP_URL = "https://app.relium.test"
API_URL = "https://api.relium.test"
INSTALLATION = 770001
REPO_ID = 880001
OTHER_REPO_ID = 880002
ALICE_GITHUB_ID = 4242


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(doc) -> str:
    return _b64url(json.dumps(doc, separators=(",", ":")).encode("utf-8"))


class _Signer:
    def __init__(self, kid="e2e-key"):
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
            raise ClerkVerificationError("unknown key")
        return key


class _FakeClient:
    def __init__(self):
        self.token = None

    def with_token(self, token):
        clone = _FakeClient()
        clone.token = token
        return clone

    def get_app(self, app_jwt):
        return {"id": 1, "slug": "relium-production-test"}

    def get_installation(self, installation_id, app_jwt):
        from agent.github_app.client import GitHubNotFoundError

        if installation_id != INSTALLATION:
            raise GitHubNotFoundError("unknown")
        return {"id": INSTALLATION, "app_id": 1,
                "repository_selection": "selected",
                "account": {"id": 5001, "login": "acme-analytics",
                            "type": "Organization"}}

    def list_installation_repositories(self, token, *, page=1, per_page=100):
        if page > 1:
            return {"total_count": 2, "repositories": []}
        return {"total_count": 2, "repositories": [
            {"id": REPO_ID, "name": "analytics", "private": True,
             "default_branch": "main", "owner": {"login": "acme-analytics"}},
            {"id": OTHER_REPO_ID, "name": "warehouse", "private": True,
             "default_branch": "main", "owner": {"login": "acme-analytics"}},
        ]}

    def get_file(self, owner, repository, path, ref):
        from agent.github_app.client import GitHubNotFoundError

        if repository == "analytics" and path == "analytics/dbt_project.yml":
            return b"name: analytics\n"
        raise GitHubNotFoundError("no dbt project")


class _FakeIdentity:
    def user_can_access_installation(self, access_token, installation_id):
        return installation_id == INSTALLATION

    def authorize_url(self, client_id, redirect_uri, state):
        return f"https://github.com/login/oauth/authorize?state={state}"

    def exchange_code(self, *, client_id, client_secret, code, redirect_uri,
                      now=None):
        from agent.api.github_identity import GitHubIdentityError, UserCredential

        if code != "valid-code":
            raise GitHubIdentityError("GitHub refused the authorization")
        return UserCredential(access_token="alice-token", expires_at=None,
                              refresh_token=None, refresh_expires_at=None)

    def fetch_viewer(self, access_token, **kwargs):
        return {"login": "alice", "user_id": ALICE_GITHUB_ID, "name": "Alice"}


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


class _FlowHarness(unittest.TestCase):
    """Fixtures for the onboarding flow. Deliberately holds NO tests.

    The suites below inherit the harness, not each other. An earlier version
    had them subclass the flow suite, which re-ran every parent test under
    fixtures written for a different scenario — green for the wrong reason,
    then red for a reason that was not a product bug.
    """

    @classmethod
    def setUpClass(cls):
        if not DSN:
            raise unittest.SkipTest("RELIUM_TEST_POSTGRES_DSN not set")
        import psycopg
        from starlette.testclient import TestClient

        from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
        from agent.api.github_installation import (
            GitHubAppIdentity, GitHubIdentityLinker, InstallationBinder,
        )
        from agent.api.pool import StorePool
        from agent.api.repository_onboarding import RepositoryOnboardingService
        from agent.api.session_crypto import generate_key, load_key
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        cls.signer = _Signer()
        cls.key = load_key(generate_key())
        cls.github = _FakeClient()
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)

        app_identity = GitHubAppIdentity(cls.github, lambda: "app-jwt",
                                         clock=lambda: NOW)
        binder = InstallationBinder(
            app_identity=app_identity, client=cls.github,
            jwt_factory=lambda: "app-jwt", session_key=cls.key,
            github_identity=_FakeIdentity(), clock=lambda: NOW)
        repository_service = RepositoryOnboardingService(
            client=cls.github, jwt_factory=lambda: "app-jwt",
            installation_token_factory=lambda i: f"installation-token-{i}",
            clock=lambda: NOW)
        cls.identity = _FakeIdentity()
        cls.linker = GitHubIdentityLinker(
            client_id="client", client_secret="secret",
            redirect_uri=f"{API_URL}/auth/github/link/callback",
            session_key=cls.key, github_identity=cls.identity,
            clock=lambda: NOW)

        cls.app = create_http_app(
            webhook_secret="e2e-secret", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool,
            clerk_verifier=ClerkVerifier(
                ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks"),
                jwks=_StubJwks(cls.signer), clock=lambda: NOW),
            installation_binder=binder, identity_linker=cls.linker,
            repository_service=repository_service,
            app_url=APP_URL, api_url=API_URL,
            billing_settings=SimpleNamespace(past_due_grace=timedelta(0)))
        cls.http = TestClient(cls.app, follow_redirects=False)
        cls.http.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        from agent.api.session_crypto import encrypt

        with self.pool.acquire() as store:
            for table in ("tenant_repositories", "tenant_billing",
                          "tenant_github_installations",
                          "github_installations", "github_installation_states",
                          "clerk_github_identities", "api_service_tokens",
                          "tenant_onboarding_state", "tenants"):
                store.connection.execute(f"DELETE FROM {table}")
            store.upsert_clerk_github_identity(
                "user_alice", github_user_id=ALICE_GITHUB_ID,
                github_login="alice",
                access_token=encrypt(self.key, "alice-token",
                                     associated="user_alice"))

    def _auth(self, **claims):
        claims.setdefault("org_id", "org_2acme")
        return {"Authorization": f"Bearer {self.signer.token(**claims)}"}

    def _state(self):
        return self.http.get("/api/onboarding/state", headers=self._auth()).json()

    def _install(self):
        import urllib.parse

        response = self.http.post("/api/onboarding/github/install",
                                  headers=self._auth())
        self.assertEqual(response.status_code, 200, response.text)
        query = urllib.parse.urlparse(response.json()["install_url"]).query
        state = urllib.parse.parse_qs(query)["state"][0]
        redirect = self.http.get("/github/setup", params={
            "installation_id": str(INSTALLATION), "state": state,
            "setup_action": "install"})
        self.assertIn("github_installed=1", redirect.headers["location"])

    def _run_full_flow(self):
        """Walk the entire flow through the real routes, asserting each step."""
        # 1. Signed in to Clerk, organization active, no Relium tenant yet.
        first = self._state()
        self.assertIs(first["complete"], False)
        self.assertEqual(first["current_step"], "workspace")
        self.assertIsNone(first["workspace"])

        # 2. Create the workspace.
        workspace = self.http.put("/api/tenants", headers=self._auth(),
                                  json={"organization_name": "Acme Analytics",
                                        "role": "Analytics engineer",
                                        "team_size": "6-20"})
        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(self._state()["current_step"], "github")
        self.assertEqual(self._state()["github"]["status"], "not_connected")

        # 3. Install the GitHub App, verified.
        self._install()
        state = self._state()
        self.assertEqual(state["github"]["status"], "connected")
        self.assertIsNone(state["configuration"])

        # 4. List repositories — server-side, from the installation.
        listing = self.http.get("/api/onboarding/repositories",
                                headers=self._auth())
        self.assertEqual(listing.status_code, 200)
        repositories = {r["repository_id"]: r
                        for r in listing.json()["repositories"]}
        self.assertEqual(set(repositories), {REPO_ID, OTHER_REPO_ID})
        self.assertEqual(listing.json()["authorization"], {
            "authorized_count": 2,
            "github_installations": [{
                "installation_id": INSTALLATION,
                "account_login": "acme-analytics",
                "account_type": "Organization",
            }],
        })
        self.assertEqual(listing.json()["policy"], {
            "plan": "free",
            "repository_limit": 1,
            "connected_repository_count": 0,
        })

        # 5. Select one. dbt detection runs during selection.
        selected = self.http.put(f"/api/onboarding/repositories/{REPO_ID}",
                                 headers=self._auth())
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertIs(selected.json()["dbt_detected"], True)
        self.assertEqual(selected.json()["dbt_project_dir"], "analytics")

        # 6. Configure dbt.
        configured = self.http.put("/api/onboarding/dbt", headers=self._auth(),
                                   json={"repository_id": REPO_ID,
                                         "project_dir": "analytics",
                                         "manifest_path":
                                             "analytics/target/manifest.json",
                                         "enforcement_mode": "shadow"})
        self.assertEqual(configured.status_code, 200, configured.text)
        body = configured.json()
        self.assertIn("version: 1", body["relium_yml"])
        self.assertIn("manifest_path: analytics/target/manifest.json",
                      body["relium_yml"])
        self.assertNotIn("project_dir", body["relium_yml"])
        self.assertEqual(body["ci_variables"]["RELIUM_DBT_PROJECT_DIR"],
                         "analytics")
        self.assertEqual(body["ci_variables"]["RELIUM_MANIFEST_PATH"],
                         "analytics/target/manifest.json")
        self.assertIs(body["ci_token_issued"], False)

        # 7. Issue the CI credential.
        issued = self.http.post("/api/onboarding/ci-token", headers=self._auth(),
                                json={"repository_id": REPO_ID})
        self.assertEqual(issued.status_code, 200, issued.text)
        credential = issued.json()
        self.assertTrue(credential["token"].startswith("rlm_"))
        self.assertEqual(credential["secret_name"], "RELIUM_CI_TOKEN")

        # 8. Complete.
        completed = self.http.post("/api/onboarding/complete",
                                   headers=self._auth(), json={})
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertIs(completed.json()["complete"], True)

        # 9. The gate now passes: the dashboard is reachable.
        final = self._state()
        self.assertIs(final["complete"], True)
        self.assertEqual(final["current_step"], "ready")
        self.assertEqual(final["configuration"]["repository_id"], REPO_ID)
        self.assertIs(final["configuration"]["ci_token_issued"], True)

        # 10. And the issued token really works for manifest submission.
        evidence = self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": f"Bearer {credential['token']}"},
            json={})
        self.assertNotIn(evidence.status_code, (401, 403),
                         "the issued CI token was refused by the real API")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the flow needs PostgreSQL")
class FirstUserFlowTests(_FlowHarness):
    """The complete first-user path, and the boundaries around it."""

    def test_the_complete_first_user_flow(self):
        self._run_full_flow()

    def test_listing_keeps_every_authorized_repository_visible_for_every_plan(self):
        workspace = self.http.put(
            "/api/tenants", headers=self._auth(),
            json={"organization_name": "Acme"}).json()
        self._install()

        def listing():
            response = self.http.get(
                "/api/onboarding/repositories", headers=self._auth())
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        free = listing()
        self.assertEqual(
            {repo["repository_id"] for repo in free["repositories"]},
            {REPO_ID, OTHER_REPO_ID})
        self.assertEqual(free["policy"]["plan"], "free")
        self.assertEqual(free["policy"]["repository_limit"], 1)

        with self.pool.acquire() as store:
            result = store.upsert_billing_from_subscription(
                tenant_id=workspace["id"],
                polar_customer_id="live_customer",
                polar_subscription_id="live_subscription",
                polar_product_id="live_starter_product",
                plan="starter", subscription_status="active",
                current_period_end=NOW + timedelta(days=30),
                cancel_at_period_end=False, past_due_at=None,
                subscription_modified_at=NOW)
        self.assertEqual(result, "applied")

        starter = listing()
        self.assertEqual(
            {repo["repository_id"] for repo in starter["repositories"]},
            {REPO_ID, OTHER_REPO_ID})
        self.assertEqual(starter["policy"]["plan"], "starter")
        self.assertEqual(starter["policy"]["repository_limit"], 3)

        with self.pool.acquire() as store:
            result = store.upsert_billing_from_subscription(
                tenant_id=workspace["id"],
                polar_customer_id="live_customer",
                polar_subscription_id="live_subscription",
                polar_product_id="live_pro_product",
                plan="pro", subscription_status="active",
                current_period_end=NOW + timedelta(days=30),
                cancel_at_period_end=False, past_due_at=None,
                subscription_modified_at=NOW + timedelta(seconds=1))
        self.assertEqual(result, "applied")

        pro = listing()
        self.assertEqual(
            {repo["repository_id"] for repo in pro["repositories"]},
            {REPO_ID, OTHER_REPO_ID})
        self.assertEqual(pro["policy"]["plan"], "pro")
        self.assertIsNone(pro["policy"]["repository_limit"])

    def test_a_returning_user_is_already_complete(self):
        self._run_full_flow()
        self.assertIs(self._state()["complete"], True)

    def test_completion_is_idempotent_over_the_route(self):
        self._run_full_flow()
        again = self.http.post("/api/onboarding/complete", headers=self._auth(),
                               json={})
        self.assertEqual(again.status_code, 200)
        self.assertIs(again.json()["complete"], True)

    # -- security over the routes ------------------------------------------

    def test_every_onboarding_route_requires_a_clerk_session(self):
        for method, path in (("get", "/api/onboarding/repositories"),
                             ("put", f"/api/onboarding/repositories/{REPO_ID}"),
                             ("put", "/api/onboarding/dbt"),
                             ("post", "/api/onboarding/ci-token"),
                             ("post", "/api/onboarding/complete")):
            response = getattr(self.http, method)(path)
            self.assertEqual(response.status_code, 401, f"{method} {path}")

    def test_a_service_token_is_refused_on_every_onboarding_route(self):
        headers = {"Authorization": "Bearer rlm_deadbeef.some-secret"}
        for method, path in (("get", "/api/onboarding/repositories"),
                             ("put", "/api/onboarding/dbt"),
                             ("post", "/api/onboarding/complete")):
            response = getattr(self.http, method)(path, headers=headers)
            self.assertEqual(response.status_code, 401, f"{method} {path}")

    def test_a_spoofed_repository_id_is_a_non_disclosing_404(self):
        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        self._install()
        for hostile in ("999999999", "0", "-1", "abc", "1%20OR%201",
                        "99999999999999999999999"):
            response = self.http.put(
                f"/api/onboarding/repositories/{hostile}", headers=self._auth())
            self.assertIn(response.status_code, (404, 405),
                          f"{hostile} -> {response.status_code}")
            if response.status_code == 404:
                # Non-disclosing: no detail about whether it exists.
                self.assertNotIn("detail", response.json())

    def test_another_tenant_cannot_configure_this_tenants_repository(self):
        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        self._install()
        self.http.put(f"/api/onboarding/repositories/{REPO_ID}",
                      headers=self._auth())

        # A second Clerk organization, with a workspace but no installation.
        other = self._auth(org_id="org_2globex", sub="user_bob")
        self.http.put("/api/tenants", headers=other,
                      json={"organization_name": "Globex"})
        response = self.http.put("/api/onboarding/dbt", headers=other,
                                 json={"repository_id": REPO_ID,
                                       "project_dir": ".",
                                       "manifest_path": "target/manifest.json",
                                       "enforcement_mode": "shadow"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "github_installation_required")

    def test_completion_is_refused_without_the_preconditions(self):
        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        response = self.http.post("/api/onboarding/complete",
                                  headers=self._auth(), json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "github_installation_required")
        self.assertIs(self._state()["complete"], False)

    def test_the_ci_token_is_never_returned_by_the_state_route(self):
        self._run_full_flow()
        text = self.http.get("/api/onboarding/state", headers=self._auth()).text
        self.assertNotIn("rlm_", text)

    def test_the_ci_token_is_not_returned_a_second_time_over_the_route(self):
        self._run_full_flow()
        again = self.http.post("/api/onboarding/ci-token", headers=self._auth(),
                               json={"repository_id": REPO_ID})
        self.assertEqual(again.status_code, 200)
        self.assertNotIn("token", again.json())
        self.assertIs(again.json()["ci_token_issued"], True)

    def test_routes_require_a_workspace_before_anything_else(self):
        for method, path, payload in (
                ("get", "/api/onboarding/repositories", None),
                ("put", "/api/onboarding/dbt", {"repository_id": REPO_ID}),
                ("post", "/api/onboarding/complete", {})):
            kwargs = {"headers": self._auth()}
            if payload is not None:
                kwargs["json"] = payload
            response = getattr(self.http, method)(path, **kwargs)
            self.assertEqual(response.status_code, 409, f"{method} {path}")
            self.assertEqual(response.json()["code"], "workspace_required")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the flow needs PostgreSQL")
class ClerkOrganizationActivationTests(_FlowHarness):
    """The steps before a tenant can exist at all.

    Clerk owns organizations, so a signed-in user may have no active one. The
    organization id is a CLAIM INSIDE THE TOKEN, which is why activating an
    organization must be followed by minting a fresh token — the old one still
    says there is none, and no amount of retrying changes that.
    """

    def test_a_session_without_an_organization_is_told_where_to_go(self):
        body = self.http.get("/api/onboarding/state",
                             headers=self._auth(org_id=None)).json()
        self.assertEqual(body["current_step"], "organization")
        self.assertEqual(body["code"], "clerk_organization_required")

    def test_creating_a_workspace_without_an_organization_is_refused(self):
        response = self.http.put("/api/tenants", headers=self._auth(org_id=None),
                                 json={"organization_name": "Acme"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "clerk_organization_required")
        with self.pool.acquire() as store:
            count = store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenants").fetchone()["c"]
        self.assertEqual(count, 0, "a tenant was created without an organization")

    def test_the_stale_token_still_fails_after_activation(self):
        """The trap this flow has to avoid.

        Activating an organization in Clerk does not change a token already
        minted. A frontend that reuses it sees the same refusal and it looks
        like a Relium bug. The fix is getToken({skipCache: true}), and this
        test is what documents that requirement.
        """
        stale = self._auth(org_id=None)
        # ... organization activated in Clerk ...
        self.assertEqual(
            self.http.put("/api/tenants", headers=stale,
                          json={"organization_name": "Acme"}).status_code, 409)
        # A REFRESHED token carries the claim, and the same request succeeds.
        refreshed = self._auth(org_id="org_2acme")
        self.assertEqual(
            self.http.put("/api/tenants", headers=refreshed,
                          json={"organization_name": "Acme"}).status_code, 200)

    def test_switching_organization_switches_tenant(self):
        first = self.http.put("/api/tenants", headers=self._auth(org_id="org_2one"),
                              json={"organization_name": "One"}).json()
        second = self.http.put("/api/tenants",
                               headers=self._auth(org_id="org_2two"),
                               json={"organization_name": "Two"}).json()
        self.assertNotEqual(first["id"], second["id"])
        # And each token sees only its own workspace.
        one = self.http.get("/api/onboarding/state",
                            headers=self._auth(org_id="org_2one")).json()
        self.assertEqual(one["workspace"]["id"], first["id"])


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the flow needs PostgreSQL")
class GitHubIdentityLinkFlowTests(_FlowHarness):
    """Linking a GitHub identity through the real route, not a seeded row."""

    def setUp(self):
        super().setUp()
        # Start from no link at all, so the route has to establish it.
        with self.pool.acquire() as store:
            store.connection.execute("DELETE FROM clerk_github_identities")

    def test_installing_is_refused_until_a_github_identity_is_linked(self):
        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        response = self.http.post("/api/onboarding/github/install",
                                  headers=self._auth())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "github_identity_required")

    def test_the_link_flow_establishes_a_verified_identity(self):
        import urllib.parse

        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        started = self.http.post("/api/onboarding/github/identity",
                                 headers=self._auth())
        self.assertEqual(started.status_code, 200, started.text)
        query = urllib.parse.urlparse(started.json()["authorize_url"]).query
        state = urllib.parse.parse_qs(query)["state"][0]

        callback = self.http.get("/auth/github/link/callback",
                                 params={"code": "valid-code", "state": state})
        self.assertEqual(callback.status_code, 302)
        self.assertIn("github_linked=1", callback.headers["location"])

        body = self.http.get("/api/onboarding/state", headers=self._auth()).json()
        self.assertIs(body["github"]["identity"]["linked"], True)
        self.assertEqual(body["github"]["identity"]["login"], "alice")

        # And installing is now permitted.
        self.assertEqual(
            self.http.post("/api/onboarding/github/install",
                           headers=self._auth()).status_code, 200)

    def test_the_github_credential_never_reaches_the_browser(self):
        import urllib.parse

        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        started = self.http.post("/api/onboarding/github/identity",
                                 headers=self._auth())
        query = urllib.parse.urlparse(started.json()["authorize_url"]).query
        state = urllib.parse.parse_qs(query)["state"][0]
        callback = self.http.get("/auth/github/link/callback",
                                 params={"code": "valid-code", "state": state})

        self.assertNotIn("alice-token", callback.headers["location"])
        text = self.http.get("/api/onboarding/state", headers=self._auth()).text
        self.assertNotIn("alice-token", text)
        self.assertNotIn("secret", text.lower().replace("secret_name", ""))

    def test_a_refused_authorization_links_nothing(self):
        import urllib.parse

        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme"})
        started = self.http.post("/api/onboarding/github/identity",
                                 headers=self._auth())
        query = urllib.parse.urlparse(started.json()["authorize_url"]).query
        state = urllib.parse.parse_qs(query)["state"][0]
        callback = self.http.get("/auth/github/link/callback",
                                 params={"code": "stolen-code", "state": state})
        self.assertIn("github_error=", callback.headers["location"])
        body = self.http.get("/api/onboarding/state", headers=self._auth()).json()
        self.assertIs(body["github"]["identity"]["linked"], False)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the flow needs PostgreSQL")
class ReturningUserTests(_FlowHarness):
    """After completion, nothing is created again."""

    def _counts(self):
        with self.pool.acquire() as store:
            def count(table):
                return store.connection.execute(
                    f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

            return {
                "tenants": count("tenants"),
                "installations": count("tenant_github_installations"),
                "repositories": count("tenant_repositories"),
                "tokens": count("api_service_tokens"),
            }

    def test_a_returning_user_creates_nothing_and_skips_onboarding(self):
        self._run_full_flow()
        before = self._counts()

        # A returning session: read state, and re-attempt every setup step.
        self.assertIs(self._state()["complete"], True)
        self.http.put("/api/tenants", headers=self._auth(),
                      json={"organization_name": "Acme Analytics"})
        self.http.put(f"/api/onboarding/repositories/{REPO_ID}",
                      headers=self._auth())
        self.http.put("/api/onboarding/dbt", headers=self._auth(),
                      json={"repository_id": REPO_ID, "project_dir": "analytics",
                            "manifest_path": "analytics/target/manifest.json",
                            "enforcement_mode": "shadow"})
        self.http.post("/api/onboarding/ci-token", headers=self._auth(),
                       json={"repository_id": REPO_ID})
        self.http.post("/api/onboarding/complete", headers=self._auth(), json={})

        self.assertEqual(self._counts(), before,
                         "a returning user created new records")
        self.assertIs(self._state()["complete"], True)

    def test_state_survives_a_refresh(self):
        """A reload is a new request with the same token. Nothing is cached in
        the browser that the server would not repeat."""
        self._run_full_flow()
        for _ in range(3):
            body = self._state()
            self.assertIs(body["complete"], True)
            self.assertEqual(body["current_step"], "ready")
            self.assertEqual(body["configuration"]["repository_id"], REPO_ID)
            self.assertEqual(body["github"]["status"], "connected")

    def test_a_second_user_in_the_same_organization_sees_the_finished_setup(self):
        """Onboarding belongs to the workspace, not the person who did it."""
        self._run_full_flow()
        colleague = self.http.get(
            "/api/onboarding/state",
            headers=self._auth(sub="user_colleague")).json()
        self.assertIs(colleague["complete"], True)
        self.assertEqual(colleague["configuration"]["repository_id"], REPO_ID)


if __name__ == "__main__":
    unittest.main()
