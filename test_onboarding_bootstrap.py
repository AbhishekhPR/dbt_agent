"""The production bootstrap actually wires onboarding up.

Every route, service and verification in Phases 1-3 is unreachable unless
``build_application`` constructs them from settings. It did not, at first: the
routes were served and answered 503 in production while every test passed,
because the tests built the application themselves.

These tests close that gap. They assert what the REAL bootstrap produces from
an environment, not what a test harness can assemble by hand.

NO REAL CREDENTIAL APPEARS IN THIS FILE. The RSA key is generated per run and
the values are .test hostnames.
"""
from __future__ import annotations

import base64
import unittest


def _private_key_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode("ascii")


def _environment(tmp, **overrides):
    values = {
        "RELIUM_GITHUB_APP_ID": "12345",
        "RELIUM_GITHUB_WEBHOOK_SECRET": "bootstrap-test-webhook-secret",
        "RELIUM_GITHUB_PRIVATE_KEY": _private_key_pem(),
        "RELIUM_STORAGE_ROOT": tmp,
        # Deliberately no database: the bootstrap must still start, and this
        # keeps the test independent of PostgreSQL.
    }
    values.update(overrides)
    return values


def _clerk_environment(tmp, **overrides):
    values = _environment(tmp, **{
        "RELIUM_CLERK_ISSUER": "https://bootstrap.clerk.accounts.test",
        "RELIUM_GITHUB_CLIENT_ID": "Iv1.bootstraptest",
        "RELIUM_GITHUB_CLIENT_SECRET": "bootstrap-client-secret",
        "RELIUM_SESSION_ENCRYPTION_KEY": base64.b64encode(b"0" * 32).decode(),
        "RELIUM_PUBLIC_URL": "https://api.relium.test",
        "RELIUM_DASHBOARD_URL": "https://app.relium.test",
        "RELIUM_DATABASE_URL": "postgresql://unused:unused@127.0.0.1:1/unused",
    })
    values.update(overrides)
    return values


class _FakePool:
    """Route registration must not need a live database."""

    def acquire(self, timeout=30.0):  # pragma: no cover - never entered
        raise AssertionError("bootstrap tests must not open a connection")

    def close(self):
        pass


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _build(self, environ, *, with_store=True):
        """Build the application the way main() does."""
        from unittest import mock

        from agent.github_app.server import build_application
        from agent.github_app.settings import load_settings

        settings = load_settings(environ)
        pool = _FakePool() if with_store else None
        with mock.patch("agent.github_app.server.build_store_pool",
                        create=True, return_value=pool):
            # build_application resolves the pool itself from the database
            # URL; patching is unnecessary when there is none, and the pool is
            # only used for route registration here.
            app = build_application(settings, environ=environ,
                                    client_factory=lambda: _FakeClient())
        return app

    # -- the gap this file exists to close ---------------------------------

    def test_the_bootstrap_enables_clerk_when_it_is_configured(self):
        """The check that would have caught the wiring gap.

        Previously the onboarding routes were served in production and
        authenticated nobody, because build_application never constructed a
        verifier.
        """
        app = self._build(_clerk_environment(self.tmp))
        self.assertIsNotNone(getattr(app.state, "store_pool", None))
        verifier = _find_clerk_verifier(app)
        self.assertIsNotNone(
            verifier,
            "build_application did not construct a Clerk verifier; every "
            "onboarding route would answer 503 in production")
        self.assertEqual(verifier.issuer, "https://bootstrap.clerk.accounts.test")

    def test_the_bootstrap_serves_every_onboarding_route(self):
        from agent.api.contract import served_routes

        app = self._build(_clerk_environment(self.tmp))
        served = {(e["method"], e["path"]) for e in served_routes(app)}
        for route in (("GET", "/api/onboarding/state"),
                      ("PUT", "/api/tenants"),
                      ("POST", "/api/onboarding/github/identity"),
                      ("POST", "/api/onboarding/github/install"),
                      ("GET", "/api/onboarding/repositories"),
                      ("PUT", "/api/onboarding/repositories/{repository_id}"),
                      ("PUT", "/api/onboarding/dbt"),
                      ("POST", "/api/onboarding/ci-token"),
                      ("POST", "/api/onboarding/complete"),
                      ("GET", "/github/setup"),
                      ("GET", "/auth/github/link/callback")):
            self.assertIn(route, served, f"{route} is not served in production")

    def test_a_deployment_without_clerk_still_starts(self):
        """A GitHub-App-only deployment is valid and must not fail to boot."""
        environ = _clerk_environment(self.tmp)
        environ.pop("RELIUM_CLERK_ISSUER")
        app = self._build(environ)
        self.assertIsNone(_find_clerk_verifier(app))
        # And the routes are still registered, answering 503 rather than
        # disappearing.
        from agent.api.contract import served_routes

        served = {e["path"] for e in served_routes(app)}
        self.assertIn("/api/onboarding/state", served)

    def test_an_insecure_clerk_issuer_stops_the_boot(self):
        """Misconfiguration fails on us, not on a customer."""
        from agent.github_app.settings import SettingsError

        environ = _clerk_environment(
            self.tmp, RELIUM_CLERK_ISSUER="http://insecure.test")
        with self.assertRaises(SettingsError):
            self._build(environ)

    def test_no_onboarding_service_is_built_without_a_database(self):
        """There is nothing to onboard into without a store."""
        environ = _clerk_environment(self.tmp)
        environ.pop("RELIUM_DATABASE_URL")
        app = self._build(environ, with_store=False)
        self.assertIsNone(_find_clerk_verifier(app))

    def test_the_private_key_is_not_exposed_on_the_application(self):
        """A key reachable from app.state is a key in every traceback."""
        app = self._build(_clerk_environment(self.tmp))
        state = {k: v for k, v in vars(app.state).items()}
        rendered = repr(state)
        self.assertNotIn("PRIVATE KEY", rendered)
        self.assertNotIn("bootstrap-client-secret", rendered)


class _FakeClient:
    def with_token(self, token):
        return self

    def get_app(self, app_jwt):
        return {"id": 12345, "slug": "relium-bootstrap-test"}


def _find_clerk_verifier(app):
    """Recover the verifier the bootstrap installed, if any.

    The routes close over it, so this reads it back out of the closure rather
    than requiring the application to expose it — exposing it would put a
    security-relevant object on shared mutable state for no benefit.
    """
    for route in app.routes:
        if getattr(route, "path", None) != "/api/onboarding/state":
            continue
        endpoint = route.endpoint
        seen = set()
        stack = [endpoint]
        while stack:
            candidate = stack.pop()
            if id(candidate) in seen:
                continue
            seen.add(id(candidate))
            closure = getattr(candidate, "__closure__", None) or ()
            for cell in closure:
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if value.__class__.__name__ == "ClerkVerifier":
                    return value
                if value.__class__.__name__ == "ClerkAuthenticator":
                    inner = getattr(value, "_verifier", None)
                    if inner is not None:
                        return inner
                if callable(value):
                    stack.append(value)
    return None


if __name__ == "__main__":
    unittest.main()
