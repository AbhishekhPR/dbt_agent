"""Dashboard session and capability tests.

These run without a database: the store is a small in-memory double and GitHub
is a scripted stand-in, so the session lifecycle and the capability policy can
be exercised exhaustively — including the paths a real GitHub cannot be asked
to produce on demand, like a refresh token that GitHub has stopped accepting.

The route-level and cross-tenant tests live in test_public_api.py, against a
real PostgreSQL and the real served application.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.api.authorization import (
    COLLECTOR_INGEST, DASHBOARD_READ, GOVERNANCE_WRITE, CapabilityError,
    authorize, highest_permission, may_govern, may_read,
)
from agent.api.session_crypto import (
    CredentialEncryptionError, decrypt, encrypt, generate_key, load_key,
)
from agent.api.sessions import SessionError, SessionManager, digest

T0 = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

# Exactly the shape GitHub returns from GET /repos/{owner}/{repo}.
ADMIN = {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True}
WRITE = {"admin": False, "maintain": False, "push": True, "triage": True, "pull": True}
TRIAGE = {"admin": False, "maintain": False, "push": False, "triage": True, "pull": True}
READ = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}
NONE = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": False}


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.states = {}
        self.tenants = set()

    def ensure_tenant(self, org, repo, env):
        self.tenants.add((org, repo, env))

    def create_oauth_state(self, state_hash, *, nonce_hash, redirect_to, expires_at):
        self.states[state_hash] = {"state_hash": state_hash, "nonce_hash": nonce_hash,
                                   "redirect_to": redirect_to, "expires_at": expires_at,
                                   "consumed_at": None}

    def consume_oauth_state(self, state_hash, *, now):
        row = self.states.get(state_hash)
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
            return None
        row["consumed_at"] = now
        return dict(row)

    def create_dashboard_session(self, session_id_hash, **fields):
        self.sessions[session_id_hash] = {
            "session_id_hash": session_id_hash, "revoked_at": None, **fields}
        return session_id_hash

    def get_dashboard_session(self, session_id_hash):
        row = self.sessions.get(session_id_hash)
        return dict(row) if row else None

    def touch_dashboard_session(self, session_id_hash):
        pass

    def update_dashboard_session_permission(self, session_id_hash, **fields):
        self.sessions[session_id_hash].update(fields)

    def update_dashboard_session_credential(self, session_id_hash, **fields):
        self.sessions[session_id_hash].update(fields)

    def revoke_dashboard_session(self, session_id_hash, reason="logout"):
        row = self.sessions.get(session_id_hash)
        if row is None or row["revoked_at"] is not None:
            return False
        row.update({"revoked_at": T0, "revocation_reason": reason,
                    "github_access_token": None, "github_refresh_token": None})
        return True


class FakeIdentity:
    """A scripted GitHub. Every call is recorded so absence can be asserted."""

    GitHubIdentityError = __import__(
        "agent.api.github_identity", fromlist=["x"]).GitHubIdentityError
    GitHubCredentialExpired = __import__(
        "agent.api.github_identity", fromlist=["x"]).GitHubCredentialExpired

    def __init__(self, permissions=ADMIN, login="octocat"):
        self.permissions = permissions
        self.login = login
        self.calls = []
        self.exchange_result = None
        self.refresh_result = None
        self.refresh_fails = False
        self.permission_error = None

    def authorize_url(self, client_id, redirect_uri, state):
        return f"https://github.test/authorize?client_id={client_id}&state={state}"

    def exchange_code(self, *, client_id, client_secret, code, redirect_uri, now=None):
        self.calls.append(("exchange", code))
        from agent.api.github_identity import UserCredential
        return self.exchange_result or UserCredential(
            access_token="gh-access-1", expires_at=(now or T0) + timedelta(hours=8),
            refresh_token="gh-refresh-1",
            refresh_expires_at=(now or T0) + timedelta(days=180))

    def refresh_credential(self, *, client_id, client_secret, refresh_token, now=None):
        self.calls.append(("refresh", refresh_token))
        if self.refresh_fails:
            raise self.GitHubIdentityError("refresh rejected")
        from agent.api.github_identity import UserCredential
        return self.refresh_result or UserCredential(
            access_token="gh-access-2", expires_at=(now or T0) + timedelta(hours=8),
            refresh_token="gh-refresh-2",
            refresh_expires_at=(now or T0) + timedelta(days=180))

    def fetch_viewer(self, access_token, **kwargs):
        self.calls.append(("viewer", access_token))
        return {"login": self.login, "user_id": 4242, "name": "Octo Cat"}

    def fetch_repository_permissions(self, access_token, owner, repository, **kwargs):
        self.calls.append(("permissions", access_token, owner, repository))
        if self.permission_error:
            raise self.permission_error
        return self.permissions


class TokenPrincipal:
    is_human = False

    def __init__(self, scope):
        self.scope = scope


class PermissionPolicyTests(unittest.TestCase):
    """The vocabulary is GitHub's. These assert against the real field names."""

    def test_governance_requires_write_authority(self):
        self.assertTrue(may_govern(ADMIN))
        self.assertTrue(may_govern(WRITE))
        self.assertTrue(may_govern({"admin": False, "maintain": True, "push": False,
                                    "triage": True, "pull": True}))

    def test_triage_and_read_are_not_governance(self):
        # triage manages issues and pull requests but cannot push, and Relium's
        # governance actions decide whether code merges.
        self.assertFalse(may_govern(TRIAGE))
        self.assertFalse(may_govern(READ))
        self.assertFalse(may_govern(NONE))

    def test_read_needs_some_access(self):
        for permissions in (ADMIN, WRITE, TRIAGE, READ):
            self.assertTrue(may_read(permissions))
        self.assertFalse(may_read(NONE))
        self.assertFalse(may_read(None))
        self.assertFalse(may_read("push"))

    def test_highest_permission_names_the_strongest(self):
        self.assertEqual(highest_permission(ADMIN), "admin")
        self.assertEqual(highest_permission(WRITE), "push")
        self.assertEqual(highest_permission(TRIAGE), "triage")
        self.assertEqual(highest_permission(READ), "pull")
        self.assertIsNone(highest_permission(NONE))


class CapabilityTests(unittest.TestCase):
    def _human(self, may_govern_flag):
        from agent.api.sessions import HumanPrincipal
        return HumanPrincipal(
            organization_id="acme", repository_id="analytics", environment="production",
            github_login="octocat", github_permission="push" if may_govern_flag else "pull",
            may_govern=may_govern_flag, session_id_hash="h")

    def test_collector_token_cannot_perform_governance(self):
        with self.assertRaises(CapabilityError):
            authorize(TokenPrincipal("collector"), GOVERNANCE_WRITE)

    def test_no_token_scope_can_ever_perform_governance(self):
        for scope in ("collector", "operator_read", "anything"):
            with self.assertRaises(CapabilityError):
                authorize(TokenPrincipal(scope), GOVERNANCE_WRITE)

    def test_human_session_cannot_ingest_collector_metadata(self):
        with self.assertRaises(CapabilityError):
            authorize(self._human(True), COLLECTOR_INGEST)

    def test_collector_token_cannot_browse_the_dashboard(self):
        with self.assertRaises(CapabilityError):
            authorize(TokenPrincipal("collector"), DASHBOARD_READ)

    def test_read_only_human_may_read_but_not_govern(self):
        authorize(self._human(False), DASHBOARD_READ)
        with self.assertRaises(CapabilityError):
            authorize(self._human(False), GOVERNANCE_WRITE)

    def test_writing_human_may_do_both(self):
        authorize(self._human(True), DASHBOARD_READ)
        authorize(self._human(True), GOVERNANCE_WRITE)

    def test_collector_token_may_ingest(self):
        authorize(TokenPrincipal("collector"), COLLECTOR_INGEST)

    def test_absent_principal_is_refused(self):
        with self.assertRaises(CapabilityError):
            authorize(None, DASHBOARD_READ)


class CredentialEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.key = load_key(generate_key())

    def test_round_trip(self):
        sealed = encrypt(self.key, "gh-token", associated="session-a")
        self.assertNotIn(b"gh-token", sealed)
        self.assertEqual(decrypt(self.key, sealed, associated="session-a"), "gh-token")

    def test_ciphertext_cannot_be_moved_between_sessions(self):
        sealed = encrypt(self.key, "gh-token", associated="session-a")
        with self.assertRaises(CredentialEncryptionError):
            decrypt(self.key, sealed, associated="session-b")

    def test_absent_credential_stays_absent(self):
        self.assertIsNone(encrypt(self.key, None, associated="s"))
        self.assertIsNone(decrypt(self.key, None, associated="s"))

    def test_a_different_key_cannot_open_it(self):
        sealed = encrypt(self.key, "gh-token", associated="s")
        with self.assertRaises(CredentialEncryptionError):
            decrypt(load_key(generate_key()), sealed, associated="s")

    def test_key_must_be_32_bytes_of_base64(self):
        for bad in (None, "", "not-base64!", "c2hvcnQ="):
            with self.assertRaises(CredentialEncryptionError):
                load_key(bad)

    def test_nonce_is_not_reused(self):
        first = encrypt(self.key, "same", associated="s")
        second = encrypt(self.key, "same", associated="s")
        self.assertNotEqual(first, second)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.identity = FakeIdentity()
        self.now = T0
        self.manager = SessionManager(
            client_id="cid", client_secret="csecret",
            encryption_key=load_key(generate_key()),
            identity=self.identity, clock=lambda: self.now)

    def _sign_in(self, permissions=ADMIN):
        self.identity.permissions = permissions
        url, nonce = self.manager.begin_authorization(
            self.store, redirect_to="/changes", redirect_uri="https://api.test/cb")
        state = url.split("state=")[1]
        return self.manager.complete_authorization(
            self.store, code="code-1", state=state, nonce=nonce,
            redirect_uri="https://api.test/cb", organization_id="acme",
            repository_id="analytics", environment="production")

    # -- creation ----------------------------------------------------------

    def test_sign_in_records_github_identity_and_permission(self):
        result = self._sign_in(WRITE)
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertEqual(record["github_login"], "octocat")
        self.assertEqual(record["github_permission"], "push")
        self.assertTrue(record["may_govern"])

    def test_the_session_id_is_never_stored(self):
        result = self._sign_in()
        self.assertNotIn(result["session_id"], self.store.sessions)
        self.assertIn(digest(result["session_id"]), self.store.sessions)

    def test_github_credentials_are_stored_encrypted(self):
        result = self._sign_in()
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertNotIn(b"gh-access-1", bytes(record["github_access_token"]))
        self.assertNotIn(b"gh-refresh-1", bytes(record["github_refresh_token"]))

    def test_a_user_without_repository_access_gets_no_session(self):
        with self.assertRaises(SessionError):
            self._sign_in(NONE)
        self.assertEqual(self.store.sessions, {})

    # -- oauth state -------------------------------------------------------

    def test_state_cannot_be_replayed(self):
        url, nonce = self.manager.begin_authorization(
            self.store, redirect_to=None, redirect_uri="https://api.test/cb")
        state = url.split("state=")[1]
        common = dict(code="c", state=state, nonce=nonce,
                      redirect_uri="https://api.test/cb", organization_id="acme",
                      repository_id="analytics", environment="production")
        self.manager.complete_authorization(self.store, **common)
        with self.assertRaises(SessionError):
            self.manager.complete_authorization(self.store, **common)

    def test_unknown_state_is_refused(self):
        with self.assertRaises(SessionError):
            self.manager.complete_authorization(
                self.store, code="c", state="never-issued", nonce="n",
                redirect_uri="https://api.test/cb", organization_id="acme",
                repository_id="analytics", environment="production")

    def test_state_from_another_browser_is_refused(self):
        url, _nonce = self.manager.begin_authorization(
            self.store, redirect_to=None, redirect_uri="https://api.test/cb")
        state = url.split("state=")[1]
        with self.assertRaises(SessionError):
            self.manager.complete_authorization(
                self.store, code="c", state=state, nonce="a-different-browser",
                redirect_uri="https://api.test/cb", organization_id="acme",
                repository_id="analytics", environment="production")

    def test_expired_state_is_refused(self):
        url, nonce = self.manager.begin_authorization(
            self.store, redirect_to=None, redirect_uri="https://api.test/cb")
        state = url.split("state=")[1]
        self.now = T0 + timedelta(minutes=11)
        with self.assertRaises(SessionError):
            self.manager.complete_authorization(
                self.store, code="c", state=state, nonce=nonce,
                redirect_uri="https://api.test/cb", organization_id="acme",
                repository_id="analytics", environment="production")

    # -- authentication ----------------------------------------------------

    def test_actor_is_derived_from_the_session(self):
        result = self._sign_in()
        principal = self.manager.authenticate(self.store, result["session_id"])
        self.assertEqual(principal.actor, "github:octocat")

    def test_unknown_and_absent_sessions_are_refused(self):
        for value in (None, "", "not-a-session"):
            with self.assertRaises(SessionError):
                self.manager.authenticate(self.store, value)

    def test_expired_session_is_refused(self):
        result = self._sign_in()
        self.now = T0 + timedelta(hours=13)
        with self.assertRaises(SessionError):
            self.manager.authenticate(self.store, result["session_id"])

    def test_logout_revokes_and_destroys_stored_credentials(self):
        result = self._sign_in()
        self.assertTrue(self.manager.revoke(self.store, result["session_id"]))
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertIsNone(record["github_access_token"])
        self.assertIsNone(record["github_refresh_token"])
        with self.assertRaises(SessionError):
            self.manager.authenticate(self.store, result["session_id"])

    # -- re-verification ---------------------------------------------------

    def test_a_write_reverifies_permission_with_github(self):
        result = self._sign_in(WRITE)
        before = len([c for c in self.identity.calls if c[0] == "permissions"])
        self.manager.authenticate(self.store, result["session_id"],
                                  require_fresh_permission=True)
        after = len([c for c in self.identity.calls if c[0] == "permissions"])
        self.assertEqual(after, before + 1)

    def test_losing_repository_access_ends_the_session(self):
        result = self._sign_in(WRITE)
        self.identity.permissions = NONE
        with self.assertRaises(SessionError):
            self.manager.authenticate(self.store, result["session_id"],
                                      require_fresh_permission=True)
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertEqual(record["revocation_reason"], "repository_access_lost")

    def test_demotion_to_read_only_removes_governance(self):
        result = self._sign_in(WRITE)
        self.identity.permissions = READ
        principal = self.manager.authenticate(
            self.store, result["session_id"], require_fresh_permission=True)
        self.assertFalse(principal.may_govern)
        with self.assertRaises(CapabilityError):
            authorize(principal, GOVERNANCE_WRITE)

    def test_reads_reuse_a_fresh_permission_without_calling_github(self):
        result = self._sign_in()
        before = len([c for c in self.identity.calls if c[0] == "permissions"])
        self.manager.authenticate(self.store, result["session_id"])
        after = len([c for c in self.identity.calls if c[0] == "permissions"])
        self.assertEqual(after, before)

    def test_a_stale_permission_is_rechecked_even_for_a_read(self):
        result = self._sign_in()
        self.now = T0 + timedelta(minutes=6)
        before = len([c for c in self.identity.calls if c[0] == "permissions"])
        self.manager.authenticate(self.store, result["session_id"])
        after = len([c for c in self.identity.calls if c[0] == "permissions"])
        self.assertEqual(after, before + 1)

    # -- credential rotation ----------------------------------------------

    def test_an_expired_access_token_is_refreshed_and_rotated(self):
        result = self._sign_in()
        self.now = T0 + timedelta(hours=9)          # access expired, refresh valid
        self.manager.authenticate(self.store, result["session_id"],
                                  require_fresh_permission=True)
        self.assertIn(("refresh", "gh-refresh-1"), self.identity.calls)
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertEqual(
            decrypt(self.manager._key, record["github_refresh_token"],
                    associated=digest(result["session_id"])),
            "gh-refresh-2")

    def test_a_failed_refresh_ends_the_session_rather_than_reusing_authorization(self):
        result = self._sign_in()
        self.identity.refresh_fails = True
        self.now = T0 + timedelta(hours=9)
        with self.assertRaises(SessionError):
            self.manager.authenticate(self.store, result["session_id"],
                                      require_fresh_permission=True)
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertIsNotNone(record["revoked_at"])

    def test_an_expired_refresh_token_ends_the_session(self):
        result = self._sign_in()
        self.now = T0 + timedelta(days=200)
        with self.assertRaises(SessionError):
            self.manager.authenticate(self.store, result["session_id"],
                                      require_fresh_permission=True)

    def test_github_rejecting_the_credential_ends_the_session(self):
        result = self._sign_in()
        self.identity.permission_error = self.identity.GitHubCredentialExpired("401")
        with self.assertRaises(SessionError):
            self.manager.authenticate(self.store, result["session_id"],
                                      require_fresh_permission=True)
        record = self.store.get_dashboard_session(digest(result["session_id"]))
        self.assertEqual(record["revocation_reason"], "github_credential_unusable")

    # -- csrf --------------------------------------------------------------

    def test_csrf_token_must_match_the_session(self):
        first = self._sign_in()
        self.assertTrue(self.manager.verify_csrf(
            self.store, first["session_id"], first["csrf_token"]))
        for wrong in (None, "", "wrong", first["csrf_token"] + "x"):
            self.assertFalse(self.manager.verify_csrf(
                self.store, first["session_id"], wrong))

    def test_a_csrf_token_from_another_session_is_refused(self):
        first = self._sign_in()
        self.store.states.clear()
        second = self._sign_in()
        self.assertFalse(self.manager.verify_csrf(
            self.store, first["session_id"], second["csrf_token"]))

    def test_a_revoked_session_fails_csrf(self):
        result = self._sign_in()
        self.manager.revoke(self.store, result["session_id"])
        self.assertFalse(self.manager.verify_csrf(
            self.store, result["session_id"], result["csrf_token"]))

    # -- handing the token to a dashboard on another host ------------------
    #
    # The CSRF token is also written as a script-readable cookie, and that is
    # enough only when the dashboard and the API share a host. Production does
    # not: app.relium.dev cannot read a cookie api.relium.dev set, because
    # document.cookie is scoped by host rather than by site. So the token has
    # to be readable from the session itself. Everything below is about that
    # read being exactly as narrow as the session it belongs to.

    def test_the_session_reports_the_token_bound_to_it(self):
        result = self._sign_in()
        self.assertEqual(
            self.manager.csrf_token(self.store, result["session_id"]),
            result["csrf_token"])
        # And it is the value verify_csrf will actually accept -- one source,
        # not two that could drift.
        self.assertTrue(self.manager.verify_csrf(
            self.store, result["session_id"],
            self.manager.csrf_token(self.store, result["session_id"])))

    def test_each_session_reports_only_its_own_token(self):
        first = self._sign_in()
        self.store.states.clear()
        second = self._sign_in()
        self.assertNotEqual(first["csrf_token"], second["csrf_token"])
        self.assertEqual(self.manager.csrf_token(self.store, second["session_id"]),
                         second["csrf_token"])
        self.assertFalse(self.manager.verify_csrf(
            self.store, first["session_id"],
            self.manager.csrf_token(self.store, second["session_id"])))

    def test_there_is_no_token_without_a_live_session(self):
        """Absent, unknown, revoked and expired all yield nothing.

        A caller who is not signed in must not be able to collect a token and
        then go looking for a session cookie to pair it with.
        """
        result = self._sign_in()
        self.assertIsNone(self.manager.csrf_token(self.store, None))
        self.assertIsNone(self.manager.csrf_token(self.store, ""))
        self.assertIsNone(self.manager.csrf_token(self.store, "not-a-session"))

        self.now = T0 + timedelta(hours=13)
        self.assertIsNone(self.manager.csrf_token(self.store, result["session_id"]))

        self.now = T0
        self.manager.revoke(self.store, result["session_id"])
        self.assertIsNone(self.manager.csrf_token(self.store, result["session_id"]))

    def test_reading_the_token_does_not_grant_anything_on_its_own(self):
        """The token is not a credential, and this is why returning it is safe.

        Presenting it against a session that is over -- or with no session at
        all -- still fails, because verify_csrf looks the session up first.
        """
        result = self._sign_in()
        token = self.manager.csrf_token(self.store, result["session_id"])
        self.manager.revoke(self.store, result["session_id"])
        self.assertFalse(self.manager.verify_csrf(
            self.store, result["session_id"], token))
        self.assertFalse(self.manager.verify_csrf(self.store, None, token))


if __name__ == "__main__":
    unittest.main()
