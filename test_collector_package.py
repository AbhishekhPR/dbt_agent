"""Authenticated distribution contract for the immutable collector bundle."""
from __future__ import annotations

import contextlib
import tempfile
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from agent.api.auth import AuthenticationError, TenantScope
from agent.api.sessions import HumanPrincipal, SESSION_COOKIE


class _Store:
    def __init__(self, plan="starter"):
        self.plan = plan

    def tenant_for_repository_slug(self, organization_id, repository_id):
        if (organization_id, repository_id) == ("acme", "warehouse-dbt"):
            return "tenant-1"
        return None

    def billing_for_tenant(self, tenant_id):
        if tenant_id != "tenant-1" or self.plan is None:
            return None
        return {"plan": self.plan, "subscription_status": "active"}


class _Pool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        return contextlib.nullcontext(self.store)


class _BillingSettings:
    past_due_grace = timedelta(0)


class _Sessions:
    def authenticate(self, store, session_id, require_fresh_permission=False):
        if session_id != "valid-session":
            raise AssertionError("test supplied an unknown session")
        return HumanPrincipal(
            organization_id="acme",
            repository_id="warehouse-dbt",
            environment="production",
            github_login="octocat",
            github_permission="pull",
            may_govern=False,
            session_id_hash="digest",
        )


class _TokenAuthenticator:
    def __init__(self, store):
        pass

    def authenticate(self, token):
        if not token:
            raise AuthenticationError("missing token")
        return TenantScope("acme", "warehouse-dbt", scope="collector")


class CollectorPackageContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / "relium-collector-0.1.0.zip"
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("relium-0.1.0-py3-none-any.whl", b"wheel-bytes")
            archive.writestr("SHA256SUMS", b"digest  relium-0.1.0-py3-none-any.whl\n")

    def _client(self, *, plan="starter", session=True):
        from agent.api.routes import create_api_routes

        client = TestClient(Starlette(routes=create_api_routes(
            store_pool=_Pool(_Store(plan)),
            session_manager=_Sessions(),
            authenticator_factory=_TokenAuthenticator,
            billing_settings=_BillingSettings(),
            collector_package_path=self.bundle,
        )))
        if session:
            client.cookies.set(SESSION_COOKIE, "valid-session")
        return client

    def test_route_factory_accepts_the_image_built_artifact_path(self):
        import inspect

        from agent.api.routes import create_api_routes

        self.assertIn("collector_package_path",
                      inspect.signature(create_api_routes).parameters)

    def test_route_is_declared_and_served(self):
        from agent.api.contract import MANDATORY_ROUTES, served_routes
        from agent.api.routes import create_api_routes

        self.assertIn(("GET", "/api/collector-package"), MANDATORY_ROUTES)
        app = Starlette(routes=create_api_routes(store_pool=_Pool(_Store())))
        paths = {route.path for route in app.routes}
        self.assertIn("/api/collector-package", paths)
        route = next(entry for entry in served_routes(app)
                     if entry["path"] == "/api/collector-package")
        self.assertEqual(route["authentication"], "github-dashboard-session")

    def test_download_capability_is_dashboard_session_only(self):
        from agent.api import authorization

        capability = getattr(authorization, "COLLECTOR_PACKAGE_DOWNLOAD", None)
        self.assertIsNotNone(capability)
        self.assertTrue(capability.human)
        self.assertEqual(capability.token_scopes, frozenset())

    def test_entitled_dashboard_session_gets_the_exact_image_artifact(self):
        response = self._client().get("/api/collector-package")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, self.bundle.read_bytes())
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["content-disposition"],
                         'attachment; filename="relium-collector-0.1.0.zip"')
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertTrue(response.headers["x-request-id"])

    def test_unauthenticated_request_is_refused(self):
        response = self._client(session=False).get("/api/collector-package")
        self.assertEqual(response.status_code, 401)

    def test_free_dashboard_session_is_refused_by_the_entitlement_gate(self):
        response = self._client(plan="free").get("/api/collector-package")
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["capability"], "warehouse_evidence")

    def test_collector_service_token_cannot_download_executable_code(self):
        response = self._client(session=False).get(
            "/api/collector-package",
            headers={"Authorization": "Bearer rlm_collector.secret"},
        )
        self.assertEqual(response.status_code, 403)

    def test_missing_image_artifact_fails_closed(self):
        self.bundle.unlink()
        response = self._client().get("/api/collector-package")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
