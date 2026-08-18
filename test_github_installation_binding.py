"""Tenant to GitHub App installation binding — Phase 2.

The central claim under test:

    A GitHub installation must never become attached to a Relium tenant
    merely because a browser supplied an installation_id.

Every test below either demonstrates that a legitimate flow converges, or that
a forged one does not bind. Where a test asserts a refusal, it also asserts the
absence of a row — "returned an error" is not the same as "wrote nothing".

Real PostgreSQL, because single-use state, cross-tenant refusal and concurrent
consumption are properties of the database. GitHub is scripted: no network, and
every failure mode can be produced deliberately.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

APP_ID = 900001
APP_SLUG = "relium-production-test"

# Two GitHub humans and two installations, so "wrong human" and "wrong
# installation" are distinct, testable situations rather than the same one.
ALICE_GITHUB_ID = 4242
BOB_GITHUB_ID = 8484
ALICE_INSTALLATION = 111111
BOB_INSTALLATION = 222222
OTHER_APP_INSTALLATION = 333333


class _FakeClient:
    """A scripted GitHub App API.

    ``get_installation`` answers only for installations of THIS App, exactly as
    the real endpoint does — which is what makes another App's installation
    indistinguishable from one that does not exist.
    """

    def __init__(self):
        self.installations = {
            ALICE_INSTALLATION: {
                "id": ALICE_INSTALLATION, "app_id": APP_ID,
                "repository_selection": "selected",
                "account": {"id": 5001, "login": "alice-analytics",
                            "type": "Organization"},
            },
            BOB_INSTALLATION: {
                "id": BOB_INSTALLATION, "app_id": APP_ID,
                "repository_selection": "all",
                "account": {"id": 6001, "login": "bob-data", "type": "User"},
            },
        }
        self.app_calls = 0

    def get_app(self, app_jwt):
        self.app_calls += 1
        return {"id": APP_ID, "slug": APP_SLUG, "name": "Relium"}

    def get_installation(self, installation_id, app_jwt):
        from agent.github_app.client import GitHubNotFoundError

        document = self.installations.get(installation_id)
        if document is None:
            # Includes OTHER_APP_INSTALLATION: GitHub answers 404 for an
            # installation of a different App, because this endpoint is scoped
            # to the App the JWT belongs to.
            raise GitHubNotFoundError("installation not found")
        return document


class _FakeGitHubIdentity:
    """GitHub, answered as a user.

    ``user_can_access_installation`` is the check that makes a forged
    installation id useless, so it is modelled honestly: each user sees only
    their own installations.
    """

    def __init__(self):
        self.access = {
            "alice-token": {ALICE_INSTALLATION},
            "bob-token": {BOB_INSTALLATION},
        }
        self.unavailable = False
        self.expired = False

    def user_can_access_installation(self, access_token, installation_id):
        from agent.api.github_identity import (
            GitHubCredentialExpired, GitHubIdentityError,
        )

        if self.unavailable:
            raise GitHubIdentityError("GitHub was unreachable")
        if self.expired:
            raise GitHubCredentialExpired("credential rejected")
        return installation_id in self.access.get(access_token, set())

    # Used only by the linker tests.
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


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; installation binding needs PostgreSQL")
class InstallationBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        from agent.api.github_installation import (
            GitHubAppIdentity, InstallationBinder,
        )
        from agent.api.session_crypto import encrypt, generate_key, load_key

        self.store.connection.execute("DELETE FROM tenant_github_installations")
        self.store.connection.execute("DELETE FROM github_installations")
        self.store.connection.execute("DELETE FROM github_installation_states")
        self.store.connection.execute("DELETE FROM clerk_github_identities")
        self.store.connection.execute("DELETE FROM tenants")

        self.key = load_key(generate_key())
        self.client = _FakeClient()
        self.identity = _FakeGitHubIdentity()
        self.clock = lambda: NOW
        self.app = GitHubAppIdentity(self.client, lambda: "app-jwt",
                                     clock=self.clock)
        self.binder = InstallationBinder(
            app_identity=self.app, client=self.client, jwt_factory=lambda: "app-jwt",
            session_key=self.key, github_identity=self.identity, clock=self.clock)

        # Two tenants, so cross-tenant attacks have somewhere to point.
        self.acme = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")["tenant_id"]
        self.globex = self.store.upsert_tenant_for_clerk_organization(
            "org_2globex", organization_name="Globex")["tenant_id"]

        # Alice belongs to Acme and controls ALICE_INSTALLATION.
        self.store.upsert_clerk_github_identity(
            "user_alice", github_user_id=ALICE_GITHUB_ID, github_login="alice",
            access_token=encrypt(self.key, "alice-token", associated="user_alice"))
        # Bob belongs to Globex and controls BOB_INSTALLATION.
        self.store.upsert_clerk_github_identity(
            "user_bob", github_user_id=BOB_GITHUB_ID, github_login="bob",
            access_token=encrypt(self.key, "bob-token", associated="user_bob"))

    # -- helpers ------------------------------------------------------------

    def _start(self, tenant_id, clerk_user_id):
        """Start a flow and return the raw state value from the URL."""
        import urllib.parse

        started = self.binder.start(self.store, tenant_id=tenant_id,
                                    clerk_user_id=clerk_user_id)
        query = urllib.parse.urlparse(started["install_url"]).query
        return urllib.parse.parse_qs(query)["state"][0]

    def _bindings(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM tenant_github_installations").fetchone()["c"]

    # -- the happy path -----------------------------------------------------

    def test_a_verified_flow_binds_the_installation(self):
        state = self._start(self.acme, "user_alice")
        binding = self.binder.complete(
            self.store, presented_state=state,
            installation_id=ALICE_INSTALLATION, clerk_user_id="user_alice")

        self.assertEqual(binding.tenant_id, self.acme)
        self.assertEqual(binding.installation_id, ALICE_INSTALLATION)
        self.assertTrue(binding.created)
        self.assertEqual(
            self.store.tenant_for_github_installation(ALICE_INSTALLATION),
            self.acme)

    def test_the_install_url_comes_from_the_app_itself(self):
        """The slug is read from GET /app, never configured or supplied."""
        started = self.binder.start(self.store, tenant_id=self.acme,
                                    clerk_user_id="user_alice")
        self.assertIn(f"/apps/{APP_SLUG}/installations/new",
                      started["install_url"])
        self.assertNotIn("relium-e2e", started["install_url"])

    def test_the_state_value_does_not_contain_the_tenant_id(self):
        """A decodable state would be an editable one."""
        state = self._start(self.acme, "user_alice")
        self.assertNotIn(self.acme, state)
        self.assertNotIn(self.acme.replace("ten_", ""), state)

    def test_facts_are_taken_from_github_not_the_request(self):
        state = self._start(self.acme, "user_alice")
        binding = self.binder.complete(
            self.store, presented_state=state,
            installation_id=ALICE_INSTALLATION, clerk_user_id="user_alice")
        self.assertEqual(binding.account_login, "alice-analytics")
        self.assertEqual(binding.account_type, "Organization")
        self.assertEqual(binding.account_id, 5001)

    # -- THE CENTRAL ATTACK -------------------------------------------------

    def test_a_forged_installation_id_does_not_bind(self):
        """The whole point of Phase 2.

        Alice runs a genuine flow with a genuine state, then substitutes Bob's
        installation id into the redirect. Everything except the human check
        passes: the state is valid, the installation is real, and it belongs to
        our App. GitHub, asked as Alice, does not list it — so nothing binds.
        """
        state = self._start(self.acme, "user_alice")
        with self._refused("installation_not_authorized"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=BOB_INSTALLATION,
                                 clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)
        self.assertIsNone(
            self.store.tenant_for_github_installation(BOB_INSTALLATION))

    def test_an_invented_installation_id_does_not_bind(self):
        state = self._start(self.acme, "user_alice")
        with self._refused("installation_unknown"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=99999999,
                                 clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)

    def test_an_installation_of_another_github_app_does_not_bind(self):
        """GET /app/installations is scoped to our App, so another App's
        installation is indistinguishable from one that does not exist."""
        state = self._start(self.acme, "user_alice")
        with self._refused("installation_unknown"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=OTHER_APP_INSTALLATION,
                                 clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)

    def test_a_missing_github_identity_refuses_rather_than_binding(self):
        """The check cannot be skipped just because it cannot be performed."""
        self.store.upsert_tenant_for_clerk_organization(
            "org_2nolink", organization_name="No Link")
        tenant = self.store.tenant_by_clerk_organization("org_2nolink")["tenant_id"]
        state = self._start(tenant, "user_unlinked")
        with self._refused("github_identity_required"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_unlinked")
        self.assertEqual(self._bindings(), 0)

    def test_an_expired_github_credential_refuses_rather_than_binding(self):
        from agent.api.session_crypto import encrypt

        self.store.upsert_clerk_github_identity(
            "user_alice", github_user_id=ALICE_GITHUB_ID, github_login="alice",
            access_token=encrypt(self.key, "alice-token", associated="user_alice"),
            access_expires_at=NOW - timedelta(minutes=1))
        state = self._start(self.acme, "user_alice")
        with self._refused("github_identity_unusable"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)

    def test_github_being_unreachable_refuses_rather_than_binding(self):
        self.identity.unavailable = True
        state = self._start(self.acme, "user_alice")
        with self._refused("github_unavailable"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)

    # -- state attacks ------------------------------------------------------

    def test_a_state_is_single_use(self):
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")
        with self._refused("installation_state_invalid"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_alice")

    def test_a_replayed_state_cannot_rebind_to_another_tenant(self):
        """The replay that would matter: reuse Acme's spent state to attach the
        installation to Globex."""
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")
        with self._refused("installation_state_invalid"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_bob")
        self.assertEqual(
            self.store.tenant_for_github_installation(ALICE_INSTALLATION),
            self.acme)

    def test_an_expired_state_is_refused(self):
        from agent.api.github_installation import InstallationBinder

        state = self._start(self.acme, "user_alice")
        later = InstallationBinder(
            app_identity=self.app, client=self.client, jwt_factory=lambda: "j",
            session_key=self.key, github_identity=self.identity,
            clock=lambda: NOW + timedelta(hours=1))
        with self._refused("installation_state_invalid"):
            later.complete(self.store, presented_state=state,
                           installation_id=ALICE_INSTALLATION,
                           clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)

    def test_a_tampered_or_unknown_state_is_refused(self):
        state = self._start(self.acme, "user_alice")
        for bad in (state[:-1] + ("A" if state[-1] != "A" else "B"),
                    "not-a-real-state", "", None, state.upper(), state + "x"):
            with self._refused("installation_state_invalid"):
                self.binder.complete(self.store, presented_state=bad,
                                     installation_id=ALICE_INSTALLATION,
                                     clerk_user_id="user_alice")
        self.assertEqual(self._bindings(), 0)

    def test_a_state_belonging_to_another_clerk_user_is_refused(self):
        """A state lifted from Alice's URL must not be completable by Bob,
        who is signed in on the same machine."""
        state = self._start(self.acme, "user_alice")
        with self._refused("installation_state_invalid"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_bob")
        self.assertEqual(self._bindings(), 0)

    def test_a_state_minted_for_the_identity_link_cannot_install(self):
        """Purposes are not interchangeable."""
        from agent.api.github_installation import hash_state, new_state

        state = new_state()
        self.store.create_github_installation_state(
            state_hash=hash_state(state), tenant_id=self.acme,
            clerk_user_id="user_alice", created_at=NOW, expires_at=NOW + timedelta(minutes=10),
            purpose="github_identity_link")
        with self._refused("installation_state_invalid"):
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_alice")

    def test_only_the_hash_of_the_state_is_stored(self):
        from agent.api.github_installation import hash_state

        state = self._start(self.acme, "user_alice")
        rows = self.store.connection.execute(
            "SELECT state_hash FROM github_installation_states").fetchall()
        stored = {row["state_hash"] for row in rows}
        self.assertIn(hash_state(state), stored)
        self.assertNotIn(state, stored)

    def test_concurrent_consumption_binds_exactly_once(self):
        """A double-clicked redirect, or a deliberate replay race.

        Only one caller may claim the state. The others must be refused, not
        silently succeed against an already-consumed row.
        """
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        state = self._start(self.acme, "user_alice")
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def attempt():
            store = None
            try:
                store = PostgresLifecycleStore(DSN)
                barrier.wait(timeout=30)
                self.binder.complete(store, presented_state=state,
                                     installation_id=ALICE_INSTALLATION,
                                     clerk_user_id="user_alice")
                with lock:
                    outcomes.append("bound")
            except Exception as exc:
                with lock:
                    outcomes.append(type(exc).__name__)
            finally:
                if store is not None:
                    store.close()

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(outcomes.count("bound"), 1,
                         f"state consumed more than once: {outcomes}")
        self.assertEqual(self._bindings(), 1)

    # -- tenancy ------------------------------------------------------------

    def test_an_installation_cannot_be_claimed_by_a_second_tenant(self):
        """Re-pointing would hand one customer's repositories to another."""
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")

        # Globex arranges a legitimate flow of its own, then names Acme's
        # installation. Bob cannot see it, so the human check refuses first —
        # and even if he could, the binding is already claimed.
        hostile = self._start(self.globex, "user_bob")
        with self._refused("installation_not_authorized"):
            self.binder.complete(self.store, presented_state=hostile,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_bob")
        self.assertEqual(
            self.store.tenant_for_github_installation(ALICE_INSTALLATION),
            self.acme)

    def test_the_claimed_binding_is_refused_even_when_the_human_check_passes(self):
        """Belt and braces: grant Bob access to Alice's installation and
        confirm the database still refuses to re-point it."""
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")

        self.identity.access["bob-token"].add(ALICE_INSTALLATION)
        hostile = self._start(self.globex, "user_bob")
        with self._refused("installation_already_connected"):
            self.binder.complete(self.store, presented_state=hostile,
                                 installation_id=ALICE_INSTALLATION,
                                 clerk_user_id="user_bob")
        self.assertEqual(
            self.store.tenant_for_github_installation(ALICE_INSTALLATION),
            self.acme)

    def test_a_tenant_may_hold_several_installations(self):
        """A customer with a personal account and an organization needs two."""
        self.identity.access["alice-token"].add(BOB_INSTALLATION)
        for installation in (ALICE_INSTALLATION, BOB_INSTALLATION):
            state = self._start(self.acme, "user_alice")
            self.binder.complete(self.store, presented_state=state,
                                 installation_id=installation,
                                 clerk_user_id="user_alice")
        rows = self.store.tenant_github_installations(self.acme)
        self.assertEqual(len(rows), 2)

    def test_rebinding_the_same_installation_to_the_same_tenant_is_idempotent(self):
        """A customer who reloads the Setup redirect must not see an error."""
        first = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=first,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")
        second = self._start(self.acme, "user_alice")
        binding = self.binder.complete(self.store, presented_state=second,
                                       installation_id=ALICE_INSTALLATION,
                                       clerk_user_id="user_alice")
        self.assertFalse(binding.created)
        self.assertEqual(self._bindings(), 1)

    def test_one_tenant_cannot_see_another_tenants_installations(self):
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")
        self.assertEqual(self.store.tenant_github_installations(self.globex), [])

    # -- redirect / webhook ordering ---------------------------------------

    def test_webhook_first_then_redirect_converges(self):
        """The delivery usually beats the browser."""
        self.store.record_github_installation(
            ALICE_INSTALLATION, github_app_id=APP_ID, github_account_id=5001,
            github_account_login="alice-analytics",
            github_account_type="Organization", repository_selection="selected")

        state = self._start(self.acme, "user_alice")
        binding = self.binder.complete(self.store, presented_state=state,
                                       installation_id=ALICE_INSTALLATION,
                                       clerk_user_id="user_alice")
        self.assertEqual(binding.tenant_id, self.acme)
        self.assertEqual(self._bindings(), 1)

    def test_redirect_first_then_webhook_converges(self):
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")

        # A later delivery refreshes the facts and must not disturb the binding.
        self.store.record_github_installation(
            ALICE_INSTALLATION, github_app_id=APP_ID, github_account_id=5001,
            github_account_login="alice-analytics-renamed",
            github_account_type="Organization", repository_selection="all")
        self.assertEqual(
            self.store.tenant_for_github_installation(ALICE_INSTALLATION),
            self.acme)
        self.assertEqual(self._bindings(), 1)

    def test_a_duplicate_webhook_delivery_does_not_duplicate_the_row(self):
        for _ in range(4):
            self.store.record_github_installation(
                ALICE_INSTALLATION, github_app_id=APP_ID, github_account_id=5001,
                github_account_login="alice-analytics",
                github_account_type="Organization")
        count = self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM github_installations").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_a_webhook_alone_never_creates_a_binding(self):
        """A signature-verified delivery proves GitHub sent it. It does not
        say which Relium customer installed the App."""
        self.store.record_github_installation(
            ALICE_INSTALLATION, github_app_id=APP_ID, github_account_id=5001,
            github_account_login="acme", github_account_type="Organization")
        self.assertEqual(self._bindings(), 0)
        self.assertIsNone(
            self.store.tenant_for_github_installation(ALICE_INSTALLATION))

    # -- lifecycle ----------------------------------------------------------

    def test_suspension_is_reported_without_losing_the_binding(self):
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")
        self.store.set_github_installation_status(
            ALICE_INSTALLATION, "suspended", suspended_at=NOW)

        from agent.api.github_installation import installations_payload

        payload = installations_payload(self.store, self.acme)
        self.assertEqual(payload["status"], "suspended")
        self.assertEqual(payload["installations"][0]["status"], "suspended")

    def test_deletion_removes_it_from_the_connected_set(self):
        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")
        self.store.set_github_installation_status(
            ALICE_INSTALLATION, "deleted", deleted_at=NOW)

        from agent.api.github_installation import installations_payload

        payload = installations_payload(self.store, self.acme)
        self.assertEqual(payload["status"], "not_connected")
        # Soft delete: the history survives even though it is not offered.
        self.assertIsNotNone(self.store.github_installation(ALICE_INSTALLATION))

    def test_a_status_update_for_an_unknown_installation_creates_nothing(self):
        self.store.set_github_installation_status(987654321, "suspended")
        self.assertIsNone(self.store.github_installation(987654321))

    # -- payload ------------------------------------------------------------

    def test_the_payload_carries_no_credential(self):
        import json as json_module

        state = self._start(self.acme, "user_alice")
        self.binder.complete(self.store, presented_state=state,
                             installation_id=ALICE_INSTALLATION,
                             clerk_user_id="user_alice")

        from agent.api.github_installation import installations_payload

        text = json_module.dumps(installations_payload(self.store, self.acme))
        for forbidden in ("alice-token", "app-jwt", "access_token", "token"):
            self.assertNotIn(forbidden, text)

    # -- helper -------------------------------------------------------------

    def _refused(self, code):
        from agent.api.github_installation import InstallationBindingError

        test = self

        class _Context:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                test.assertIsNotNone(exc, f"expected refusal with code {code}")
                test.assertIsInstance(exc, InstallationBindingError)
                test.assertEqual(exc.code, code)
                return True

        return _Context()


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class GitHubIdentityLinkTests(unittest.TestCase):
    """Proving a Clerk user controls a GitHub account."""

    @classmethod
    def setUpClass(cls):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        from agent.api.github_installation import GitHubIdentityLinker
        from agent.api.session_crypto import generate_key, load_key

        self.store.connection.execute("DELETE FROM clerk_github_identities")
        self.store.connection.execute("DELETE FROM github_installation_states")
        self.store.connection.execute("DELETE FROM tenants")
        self.key = load_key(generate_key())
        self.identity = _FakeGitHubIdentity()
        self.linker = GitHubIdentityLinker(
            client_id="client", client_secret="secret",
            redirect_uri="https://api.relium.test/auth/github/link/callback",
            session_key=self.key, github_identity=self.identity,
            clock=lambda: NOW)
        self.tenant = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")["tenant_id"]

    def _state(self, clerk_user_id="user_alice", purpose="github_identity_link"):
        from agent.api.github_installation import hash_state, new_state

        state = new_state()
        self.store.create_github_installation_state(
            state_hash=hash_state(state), tenant_id=self.tenant,
            clerk_user_id=clerk_user_id, created_at=NOW, expires_at=NOW + timedelta(minutes=10),
            purpose=purpose)
        return state

    def test_a_completed_oauth_exchange_links_the_identity(self):
        self.linker.complete(self.store, code="valid-code",
                             presented_state=self._state())
        link = self.store.clerk_github_identity("user_alice")
        self.assertEqual(link["github_user_id"], ALICE_GITHUB_ID)
        self.assertEqual(link["github_login"], "alice")

    def test_the_credential_is_encrypted_at_rest(self):
        self.linker.complete(self.store, code="valid-code",
                             presented_state=self._state())
        raw = self.store.connection.execute(
            "SELECT access_token FROM clerk_github_identities "
            "WHERE clerk_user_id = 'user_alice'").fetchone()["access_token"]
        self.assertNotIn(b"alice-token", bytes(raw))

    def test_the_credential_is_bound_to_the_clerk_user(self):
        """A row lifted into another user's record must fail to decrypt."""
        from agent.api.session_crypto import CredentialEncryptionError, decrypt

        self.linker.complete(self.store, code="valid-code",
                             presented_state=self._state())
        stored = self.store.clerk_github_identity("user_alice")["access_token"]
        with self.assertRaises(CredentialEncryptionError):
            decrypt(self.key, stored, associated="user_someone_else")

    def test_the_link_belongs_to_the_clerk_user_who_minted_the_state(self):
        """Not to whoever completes the callback."""
        self.linker.complete(self.store, code="valid-code",
                             presented_state=self._state("user_carol"))
        self.assertIsNotNone(self.store.clerk_github_identity("user_carol"))
        self.assertIsNone(self.store.clerk_github_identity("user_alice"))

    def test_an_install_state_cannot_be_spent_on_the_link_flow(self):
        from agent.api.github_installation import InstallationBindingError

        state = self._state(purpose="github_app_install")
        with self.assertRaises(InstallationBindingError) as caught:
            self.linker.complete(self.store, code="valid-code",
                                 presented_state=state)
        self.assertEqual(caught.exception.code, "installation_state_invalid")

    def test_a_link_state_is_single_use(self):
        from agent.api.github_installation import InstallationBindingError

        state = self._state()
        self.linker.complete(self.store, code="valid-code", presented_state=state)
        with self.assertRaises(InstallationBindingError):
            self.linker.complete(self.store, code="valid-code",
                                 presented_state=state)

    def test_a_refused_code_links_nothing(self):
        from agent.api.github_installation import InstallationBindingError

        with self.assertRaises(InstallationBindingError):
            self.linker.complete(self.store, code="stolen-code",
                                 presented_state=self._state())
        self.assertIsNone(self.store.clerk_github_identity("user_alice"))


if __name__ == "__main__":
    unittest.main()
