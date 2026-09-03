"""The dashboard's CSRF path, over real HTTP, without a database.

###################################################################
# THE DASHBOARD AND THE API ARE DIFFERENT HOSTS IN PRODUCTION.    #
###################################################################

The dashboard is ``app.relium.dev``; the API is ``api.relium.dev``. The API
sets ``relium_csrf`` with no Domain, so the cookie is host-only, and
``document.cookie`` is scoped by HOST rather than by site — the dashboard could
not read it at all. The session cookie was never affected, because the browser
only has to SEND that one, and ``app`` and ``api`` are same-site under
``relium.dev``, so it went out on every request.

That asymmetry is the whole defect. Authenticated reads worked; every
cookie-authenticated mutation arrived with an empty ``X-Relium-CSRF`` and was
refused with "missing or invalid CSRF token". Re-run was simply the first such
mutation a production run exercised — nothing about the re-run endpoint is
special, and nothing here is special-cased for it.

The route-level suite in test_public_api.py covers this against real
PostgreSQL, and is skipped on a machine without one. These tests exist so the
HTTP path is exercised anyway: the store is a small in-memory double, and the
application, the session manager, the /auth routes, the CORS configuration and
the CSRF check are all the served ones.

Every test here obtains the token the way a browser on another host must —
from ``GET /auth/session``, carrying nothing but the HttpOnly session cookie.
None of them reads a cookie jar, because in production there is nothing to
read.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from agent.api.auth_routes import create_auth_routes
from agent.api.session_crypto import generate_key, load_key
from agent.api.sessions import SessionManager
from agent.github_app.http_app import create_http_app

from test_dashboard_auth import ADMIN, READ, FakeIdentity, FakeStore

T0 = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

DASHBOARD = "https://app.relium.test"
API = "https://api.relium.test"

ORG = "acme"
REPO = "analytics"
ENV = "production"
REVIEW = "gh-review-1"


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


class _ReviewStore(FakeStore):
    """The session double from test_dashboard_auth, plus one review.

    Only the handful of lifecycle calls ``rerun_review`` makes are implemented.
    That is deliberate: the point of this suite is the authorization path in
    front of the handler, and a fuller store would only add ways for it to fail
    for reasons that are not the subject.
    """

    def __init__(self):
        super().__init__()
        self.collection_requests = []
        self.transitions = []
        self.audit = []
        self.review = {
            "organization_id": ORG, "repository_id": REPO, "environment": ENV,
            "review_id": REVIEW, "pull_number": 60,
            "head_sha": "a" * 40, "base_sha": "b" * 40,
            "base_manifest_hash": "c" * 40, "head_manifest_hash": "d" * 40,
            "attempt": 1, "policy_version": "1", "policy_hash": "e" * 40,
            "metadata_required": True,
            "payload": {"plan": {
                "required_evidence_level": "profile",
                "targets": [{"dependency_kind": "external",
                             "criticality": "standard",
                             "relation": "warehouse.analytics.fct_orders"}],
            }},
        }

    def get_review(self, organization_id, repository_id, review_id):
        if (organization_id, repository_id, review_id) != (ORG, REPO, REVIEW):
            return None
        return dict(self.review)

    def collection_requests_for_review(self, organization_id, repository_id,
                                       review_id):
        return [dict(r) for r in self.collection_requests]

    def create_collection_request(self, organization_id, repository_id,
                                  environment, *, request_id, review_id,
                                  reason, expires_at, targets, **rest):
        self.collection_requests.append({
            "request_id": request_id, "review_id": review_id,
            "state": "PENDING", "reason": reason, "expires_at": expires_at,
            "targets": targets, **rest})
        return request_id

    def transition_review(self, organization_id, repository_id, review_id,
                          state, reason=None):
        self.transitions.append((review_id, state, reason))
        return True

    def append_audit(self, organization_id, repository_id, **fields):
        self.audit.append(fields)
        return True


class _Pool:
    def __init__(self, store):
        self._store = store

    @contextmanager
    def acquire(self):
        yield self._store

    def close(self):
        pass


class DashboardCsrfRouteTests(unittest.TestCase):
    """One application, driven the way a browser on another host drives it."""

    def setUp(self):
        self.store = _ReviewStore()
        self.identity = FakeIdentity()
        self.identity.permissions = ADMIN
        self.now = T0
        self.sessions = SessionManager(
            client_id="cid", client_secret="csecret",
            encryption_key=load_key(generate_key()),
            identity=self.identity, clock=lambda: self.now)
        self.pool = _Pool(self.store)
        self.app = create_http_app(
            webhook_secret="secret", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=self.pool,
            session_manager=self.sessions,
            cors_allowed_origins=(DASHBOARD,),
            auth_routes=create_auth_routes(
                store_pool=self.pool, session_manager=self.sessions,
                dashboard_url=DASHBOARD,
                callback_url=f"{API}/auth/github/callback",
                organization_id=ORG, repository_id=REPO, environment=ENV,
                secure_cookies=False),
            secure_cookies=False)
        self.client = TestClient(self.app)

    # -- helpers -----------------------------------------------------------

    def _sign_in(self, permissions=ADMIN):
        self.identity.permissions = permissions
        url, nonce = self.sessions.begin_authorization(
            self.store, redirect_to="/changes", redirect_uri=f"{API}/cb")
        state = url.split("state=")[1]
        return self.sessions.complete_authorization(
            self.store, code="code", state=state, nonce=nonce,
            redirect_uri=f"{API}/cb", organization_id=ORG,
            repository_id=REPO, environment=ENV)

    def _cookies(self, session):
        """ONLY the session cookie. The CSRF cookie is on a host we cannot read."""
        return {"relium_session": session["session_id"]}

    def _browser_csrf(self, session):
        response = self.client.get("/auth/session", cookies=self._cookies(session),
                                   headers={"Origin": DASHBOARD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def _rerun(self, session, csrf, *, origin=DASHBOARD, review_id=REVIEW):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if csrf is not None:
            headers["X-Relium-CSRF"] = csrf
        return self.client.post(f"/api/reviews/{review_id}/rerun", json={},
                                headers=headers, cookies=self._cookies(session))

    # -- the token is reachable from another host --------------------------

    def test_the_session_endpoint_reports_this_session_s_token(self):
        session = self._sign_in()
        self.assertEqual(self._browser_csrf(session), session["csrf_token"])

    def test_the_cookie_is_still_set_and_still_host_only(self):
        """The fix adds a way to read the token; it does not widen the cookie.

        A ``Domain=.relium.dev`` would have handed the token to every sibling
        subdomain — the exact attacker the CSRF check exists to stop.
        """
        session = self._sign_in()
        response = self.client.get("/auth/session", cookies=self._cookies(session))
        self.assertEqual(response.status_code, 200)
        for header in response.headers.get_list("set-cookie"):
            self.assertNotIn("Domain=", header)

    def test_an_unauthenticated_browser_gets_no_token(self):
        response = self.client.get("/auth/session")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("csrf", response.text.lower())

    def test_the_session_endpoint_never_returns_a_github_credential(self):
        session = self._sign_in()
        body = self.client.get("/auth/session", cookies=self._cookies(session)).json()
        self.assertEqual(body.pop("csrf_token"), session["csrf_token"])
        self.assertNotRegex(str(body), r"(?i)access_token|refresh|secret|gho_")

    def test_a_revoked_session_reports_nothing(self):
        session = self._sign_in()
        self.sessions.revoke(self.store, session["session_id"])
        self.assertEqual(
            self.client.get("/auth/session",
                            cookies=self._cookies(session)).status_code, 401)

    # -- and the re-run works ----------------------------------------------

    def test_a_dashboard_on_another_host_can_re_run(self):
        """The assertion production failed.

        The token is fetched over HTTP with only the session cookie attached,
        exactly as the dashboard must, and the mutation is accepted.
        """
        session = self._sign_in()

        response = self._rerun(session, self._browser_csrf(session))

        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["rerun_id"])

    def test_the_accepted_re_run_actually_requested_the_collection(self):
        """202 has to mean work was requested, not merely that a gate opened."""
        session = self._sign_in()
        body = self._rerun(session, self._browser_csrf(session)).json()

        self.assertEqual([r["request_id"] for r in self.store.collection_requests],
                         [body["rerun_id"]])
        self.assertEqual(self.store.transitions,
                         [(REVIEW, "METADATA_REQUESTED",
                           f"re-run requested: {body['rerun_id']}")])
        self.assertEqual([e["event_type"] for e in self.store.audit],
                         ["review.rerun_requested"])

    def test_reads_never_needed_the_token_and_still_do_not(self):
        """The half that always worked must keep working, unchanged.

        A read carries no CSRF header at all and must not acquire one. 404
        rather than 200 is the assertion available here: this store double
        answers the session calls, not the full review projection, so reaching
        the handler and being told the review is unknown is exactly the proof
        wanted -- authentication passed and nothing demanded a token.
        """
        session = self._sign_in()
        response = self.client.get("/api/reviews/no-such-review",
                                   cookies=self._cookies(session))
        self.assertEqual(response.status_code, 404, response.text)

    # -- and the protection is intact --------------------------------------

    def test_a_missing_token_is_refused(self):
        session = self._sign_in()
        response = self._rerun(session, None)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_token_invalid")
        self.assertEqual(self.store.collection_requests, [])

    def test_an_empty_token_is_refused(self):
        """The empty string is precisely what the broken dashboard sent."""
        session = self._sign_in()
        response = self._rerun(session, "")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_token_invalid")
        self.assertEqual(self.store.collection_requests, [])

    def test_a_wrong_token_is_refused(self):
        session = self._sign_in()
        for wrong in ("wrong", session["csrf_token"] + "x",
                      session["csrf_token"][:-1]):
            response = self._rerun(session, wrong)
            self.assertEqual(response.status_code, 403, wrong)
            self.assertEqual(response.json()["code"], "csrf_token_invalid")
        self.assertEqual(self.store.collection_requests, [])

    def test_another_sessions_token_cannot_re_run_this_one(self):
        """Reading a token does not unbind it from the session it belongs to."""
        session = self._sign_in()
        self.store.states.clear()
        other = self._sign_in()
        self.assertNotEqual(session["csrf_token"], other["csrf_token"])

        response = self._rerun(session, self._browser_csrf(other))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_token_invalid")
        self.assertEqual(self.store.collection_requests, [])

    def test_a_valid_token_from_another_origin_is_refused(self):
        """A sibling subdomain is same-site, so SameSite alone is not enough."""
        session = self._sign_in()
        csrf = self._browser_csrf(session)

        forged = self._rerun(session, csrf, origin="https://evil.relium.test")
        self.assertEqual(forged.status_code, 403)
        self.assertEqual(forged.json()["code"], "origin_not_allowed")

        missing = self._rerun(session, csrf, origin=None)
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(missing.json()["code"], "origin_required")

        self.assertEqual(self.store.collection_requests, [])

    def test_a_revoked_sessions_token_cannot_re_run(self):
        session = self._sign_in()
        csrf = self._browser_csrf(session)
        self.sessions.revoke(self.store, session["session_id"])

        self.assertEqual(self._rerun(session, csrf).status_code, 403)
        self.assertEqual(self.store.collection_requests, [])

    def test_an_expired_sessions_token_cannot_re_run(self):
        session = self._sign_in()
        csrf = self._browser_csrf(session)
        self.now = T0 + timedelta(hours=13)

        self.assertEqual(self._rerun(session, csrf).status_code, 401)
        self.assertEqual(self.store.collection_requests, [])

    def test_a_token_with_no_session_cookie_is_not_a_credential(self):
        """This is why serving the token is safe: alone, it does nothing."""
        session = self._sign_in()
        csrf = self._browser_csrf(session)

        response = self.client.post(f"/api/reviews/{REVIEW}/rerun", json={},
                                    headers={"Origin": DASHBOARD,
                                             "X-Relium-CSRF": csrf})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.collection_requests, [])

    def test_a_read_only_collaborator_is_still_refused(self):
        """CSRF is not authorization. Passing it confers no authority."""
        session = self._sign_in(READ)
        response = self._rerun(session, self._browser_csrf(session))
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(response.json().get("code"), "csrf_token_invalid")
        self.assertEqual(self.store.collection_requests, [])


if __name__ == "__main__":
    unittest.main()
