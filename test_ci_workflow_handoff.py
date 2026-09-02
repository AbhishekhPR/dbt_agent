"""Authenticated handoff of the canonical customer GitHub Actions workflow.

The workflow is static and credential-free. Tenant-specific repository
variables travel beside it; the one-time CI credential never does.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from agent.api.onboarding_repository_routes import (
    create_onboarding_repository_routes,
)
from agent.api.repository_onboarding import (
    CODE_REPOSITORY_NOT_FOUND,
    RepositoryOnboardingError,
)
from agent.ci_workflow import (
    CI_TOKEN_SECRET_NAME,
    WORKFLOW_PATH,
    workflow_payload,
    workflow_source,
    workflow_version,
)


ACME_REPOSITORY = 900001
GLOBEX_REPOSITORY = 900002


class _Store:
    records = {
        "ten_acme": {
            "github_repository_id": ACME_REPOSITORY,
            "project_dir": "analytics",
            "manifest_path": "analytics/target/manifest.json",
            "ci_token_id": "tok_non_secret_identifier",
        },
        "ten_globex": {
            "github_repository_id": GLOBEX_REPOSITORY,
            "project_dir": ".",
            "manifest_path": "target/manifest.json",
            "ci_token_id": None,
        },
    }

    def configured_tenant_repository(self, tenant_id):
        return self.records.get(tenant_id)


class _Pool:
    @contextmanager
    def acquire(self):
        yield _Store()


class _Service:
    allowed = {
        "ten_acme": {ACME_REPOSITORY},
        "ten_globex": {GLOBEX_REPOSITORY},
    }

    def authorized_repository(self, store, tenant_id, repository_id):
        if repository_id not in self.allowed.get(tenant_id, set()):
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)
        return SimpleNamespace(github_repository_id=repository_id)


class _AuthError(Exception):
    pass


class _Authenticator:
    principals = {
        "Bearer acme-session": SimpleNamespace(
            tenant_id="ten_acme", clerk_user_id="user_acme"),
        "Bearer globex-session": SimpleNamespace(
            tenant_id="ten_globex", clerk_user_id="user_globex"),
    }

    def principal(self, request, store, *, write, require_tenant):
        if write or not require_tenant:
            raise AssertionError("workflow download must be a tenant read")
        try:
            return self.principals[request.headers.get("Authorization")]
        except KeyError as exc:
            raise _AuthError from exc

    def map_error(self, exc, request_id):
        if isinstance(exc, _AuthError):
            return JSONResponse(
                {"status": "unauthorized", "request_id": request_id},
                status_code=401,
            )
        return None


def _client():
    routes = create_onboarding_repository_routes(
        store_pool=_Pool(),
        clerk_authenticator=_Authenticator(),
        service=_Service(),
        api_url="https://api.relium.test",
    )
    return TestClient(Starlette(routes=routes))


class WorkflowAssetTests(unittest.TestCase):
    def test_packaged_workflow_is_the_canonical_checked_in_workflow(self):
        from pathlib import Path

        canonical = (Path(__file__).parent / ".github" / "workflows"
                     / "relium-pr-review.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow_source(), canonical)

    def test_version_is_content_addressed(self):
        source = workflow_source()
        expected = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(workflow_version(), expected)

    def test_payload_names_the_path_and_never_contains_a_credential(self):
        payload = workflow_payload(
            ci_variables={"RELIUM_API_URL": "https://api.relium.test"},
            ci_token_issued=True,
        )
        self.assertEqual(payload["path"], WORKFLOW_PATH)
        self.assertEqual(payload["secret_name"], CI_TOKEN_SECRET_NAME)
        self.assertNotIn("token", payload)
        self.assertNotIn("rlm_", payload["content"])
        self.assertNotIn("tok_non_secret_identifier", str(payload))

    def test_workflow_references_the_variables_and_secret_it_is_given(self):
        source = workflow_source()
        for variable in (
                "RELIUM_API_URL", "RELIUM_DBT_PROJECT_DIR",
                "RELIUM_MANIFEST_PATH"):
            self.assertIn(f"vars.{variable}", source)
        self.assertIn("secrets.RELIUM_CI_TOKEN", source)


class WorkflowEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = _client()

    def test_endpoint_requires_authentication(self):
        response = self.client.get(
            f"/api/onboarding/ci-workflow?repository_id={ACME_REPOSITORY}")
        self.assertEqual(response.status_code, 401)

    def test_endpoint_returns_only_the_authenticated_tenants_configuration(self):
        response = self.client.get(
            f"/api/onboarding/ci-workflow?repository_id={ACME_REPOSITORY}",
            headers={"Authorization": "Bearer acme-session"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], WORKFLOW_PATH)
        self.assertEqual(body["content"], workflow_source())
        self.assertIs(body["ci_token_issued"], True)
        self.assertEqual(
            body["variables"],
            [
                {"name": "RELIUM_API_URL",
                 "value": "https://api.relium.test"},
                {"name": "RELIUM_DBT_PROJECT_DIR", "value": "analytics"},
                {"name": "RELIUM_MANIFEST_PATH",
                 "value": "analytics/target/manifest.json"},
            ],
        )
        self.assertNotIn("tok_non_secret_identifier", response.text)
        self.assertNotIn("rlm_", response.text)

    def test_another_tenants_repository_is_non_disclosing_not_found(self):
        response = self.client.get(
            f"/api/onboarding/ci-workflow?repository_id={GLOBEX_REPOSITORY}",
            headers={"Authorization": "Bearer acme-session"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], CODE_REPOSITORY_NOT_FOUND)
        self.assertNotIn("globex", response.text.lower())
        self.assertNotIn("target/manifest.json", response.text)

    def test_repository_id_must_be_the_configured_repository(self):
        response = self.client.get(
            f"/api/onboarding/ci-workflow?repository_id={ACME_REPOSITORY}",
            headers={"Authorization": "Bearer globex-session"},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
