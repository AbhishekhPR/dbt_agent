"""Route-contract drift guard.

These tests do not need a database: they assert that the declared contract and
the served route table cannot silently diverge. An endpoint named by a constant
but not served must fail here rather than be reported as implemented.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

CONTRACT_PATH = Path(__file__).with_name("docs") / "api-contract.json"


class _Queue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


class _FakePool:
    """Route registration must not require a live database."""

    def acquire(self, timeout=30.0):  # pragma: no cover - never entered here
        raise AssertionError("contract tests must not open a database connection")

    def close(self):
        pass


def _app():
    from agent.github_app.http_app import create_http_app

    return create_http_app(
        webhook_secret="contract-test",
        job_queue=_Queue(),
        max_body_bytes=1024,
        shutdown_timeout_seconds=1.0,
        clock=lambda: 0.0,
        store_pool=_FakePool(),
    )


class RouteContractTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def test_every_dashboard_resource_is_served(self):
        from agent.api.contract import contract_drift

        drift = contract_drift(self.app)
        self.assertEqual(
            drift["unserved_dashboard_resources"], [],
            "DASHBOARD_RESOURCES declares a path with no handler",
        )

    def test_no_mandatory_route_is_missing(self):
        from agent.api.contract import contract_drift

        self.assertEqual(contract_drift(self.app)["missing_mandatory_routes"], [])

    def test_no_undeclared_api_route_is_served(self):
        from agent.api.contract import contract_drift

        self.assertEqual(contract_drift(self.app)["undeclared_api_routes"], [])

    def test_contract_is_drift_free(self):
        from agent.api.contract import contract_drift

        drift = contract_drift(self.app)
        self.assertTrue(drift["drift_free"], drift)

    def test_checked_in_contract_matches_the_served_route_table(self):
        from agent.api.contract import served_routes

        self.assertTrue(
            CONTRACT_PATH.is_file(),
            "docs/api-contract.json is missing; regenerate it from the served routes",
        )
        checked_in = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            checked_in["routes"], served_routes(self.app),
            "docs/api-contract.json has drifted from the served route table",
        )

    def test_every_api_route_is_authenticated(self):
        """No /api route may be reachable without a credential.

        Widened from "service-token" to an allow-list of authenticated modes
        when Clerk sessions were introduced. The guarantee is unchanged and the
        check is no weaker: a mode absent from AUTHENTICATED_MODES — "none"
        above all — still fails, and adding a new mode is a deliberate edit to
        that set rather than something a new route can do on its own.
        """
        from agent.api.contract import AUTHENTICATED_MODES, served_routes

        for entry in served_routes(self.app):
            if entry["path"].startswith("/api/"):
                self.assertIn(
                    entry["authentication"], AUTHENTICATED_MODES,
                    f"{entry['method']} {entry['path']} is not marked authenticated",
                )

    def test_onboarding_routes_are_clerk_authenticated(self):
        """Onboarding is a Clerk principal, and is declared as one."""
        from agent.api.contract import served_routes

        modes = {
            (e["method"], e["path"]): e["authentication"]
            for e in served_routes(self.app)
        }
        self.assertEqual(modes[("GET", "/api/onboarding/state")], "clerk-session")
        self.assertEqual(modes[("PUT", "/api/tenants")], "clerk-session")

    def test_service_token_routes_did_not_become_clerk_routes(self):
        """The pre-existing API surface keeps the credential it always had."""
        from agent.api.contract import served_routes

        clerk_paths = {"/api/onboarding/state", "/api/tenants",
                       "/api/onboarding/github/identity",
                       "/api/onboarding/github/install",
                       "/api/onboarding/repositories",
                       "/api/onboarding/repositories/{repository_id}",
                       "/api/onboarding/dbt",
                       "/api/onboarding/ci-token",
                       "/api/onboarding/complete",
                       "/api/onboarding/dashboard-session"}
        for entry in served_routes(self.app):
            if entry["path"].startswith("/api/") and entry["path"] not in clerk_paths:
                self.assertEqual(
                    entry["authentication"], "service-token",
                    f"{entry['method']} {entry['path']} changed authentication mode",
                )

    def test_webhook_keeps_its_own_signature_authentication(self):
        from agent.api.contract import served_routes

        webhook = [e for e in served_routes(self.app) if e["path"] == "/github/webhook"]
        self.assertEqual(len(webhook), 1)
        self.assertEqual(webhook[0]["authentication"], "github-webhook-signature")

    def test_api_routes_are_absent_when_no_store_pool_is_configured(self):
        from agent.api.contract import served_routes
        from agent.github_app.http_app import create_http_app

        app = create_http_app(
            webhook_secret="contract-test",
            job_queue=_Queue(),
            max_body_bytes=1024,
            shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0,
        )
        paths = {e["path"] for e in served_routes(app)}
        self.assertEqual(paths, {"/healthz", "/readyz", "/github/webhook"})


class DashboardContractCompatibilityTests(unittest.TestCase):
    def test_original_declared_resources_are_unchanged(self):
        from agent.dashboard_contracts import DASHBOARD_RESOURCES

        original = {
            "review_list": "/api/reviews",
            "review_detail": "/api/reviews/{review_id}",
            "deployment_list": "/api/deployments",
            "deployment_detail": "/api/deployments/{deployment_id}",
            "monitoring_status": "/api/monitoring",
            "anomaly_list": "/api/anomalies",
            "incident_detail": "/api/incidents/{incident_id}",
            "model_lineage": "/api/models/{model}/lineage",
            "kpi_impact": "/api/kpis/{kpi}/impact",
            "repository_settings": "/api/repositories/{repository}/settings",
        }
        for name, path in original.items():
            self.assertEqual(
                DASHBOARD_RESOURCES.get(name), path,
                f"pre-existing dashboard resource {name} changed path",
            )

    def test_added_resources_cover_rca_coverage_and_delivery(self):
        from agent.dashboard_contracts import DASHBOARD_RESOURCES

        for name in ("incident_rca", "evidence_coverage", "delivery_status"):
            self.assertIn(name, DASHBOARD_RESOURCES)


if __name__ == "__main__":
    unittest.main()
