"""Authenticated handoff of the canonical customer GitHub Actions workflow.

The workflow is static and credential-free. Tenant-specific repository
variables travel beside it; the one-time CI credential never does.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
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
    ci_variables_for,
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



class ManifestPathResolutionTests(unittest.TestCase):
    """The variables we serve, run through the workflow's own resolution code.

    ###################################################################
    # THE BUG THIS EXISTS FOR                                         #
    ###################################################################

    ``ci_variables_for`` used to strip the project directory out of
    RELIUM_MANIFEST_PATH, on the stated reasoning that the workflow had already
    changed into the project. It has not: the resolution step runs with
    ``working-directory: source`` and evaluates ``root / configured_manifest``
    where ``root`` is the checkout root.

    So a customer whose dbt project lived in ``analytics/`` was handed
    ``RELIUM_MANIFEST_PATH=target/manifest.json``. The workflow looked for
    ``<root>/target/manifest.json``, dbt had written
    ``<root>/analytics/target/manifest.json``, and the ``test -f`` guard failed
    the job. On their first pull request, after setup told them Relium was
    ready.

    ###################################################################
    # WHY IT RUNS THE REAL SCRIPT RATHER THAN RESTATING THE RULE.     #
    ###################################################################

    An assertion that RELIUM_MANIFEST_PATH equals a hard-coded string is a
    second copy of the rule, and a second copy can be wrong in exactly the way
    the first one was. There is already such an assertion in
    test_repository_onboarding.py -- and it is gated on PostgreSQL, so it does
    not run in CI or on a laptop without a database. The most consequential
    line in this change had no executing test.

    This extracts the Python from the workflow file, runs it against a real
    directory tree laid out the way dbt would leave one, and asserts it finds
    the manifest. If either side moves -- the template's resolution or the
    variables we serve -- this fails, and it needs neither PostgreSQL nor
    GitHub to say so.
    """

    #: The step whose output every later step consumes.
    STEP = "Resolve dbt project and manifest paths"

    @classmethod
    def setUpClass(cls):
        source = workflow_source()
        # The heredoc body between `python - <<'PY'` and its terminator, taken
        # from the resolution step rather than from the first heredoc in the
        # file -- the submit job contains another one.
        step = source.split(cls.STEP, 1)[1]
        body = step.split("python - <<'PY'", 1)[1].split("\n          PY", 1)[0]
        # The block is indented to sit inside the YAML `run:` scalar.
        cls.script = "\n".join(
            line[10:] if line.startswith(" " * 10) else line
            for line in body.split("\n")
        )

    def _resolve(self, root, variables):
        """Run the workflow's resolution step and return its GITHUB_OUTPUT."""
        with tempfile.NamedTemporaryFile("w+", delete=False) as handle:
            output_path = handle.name
        env = {
            **os.environ,
            "GITHUB_OUTPUT": output_path,
            "RELIUM_DBT_PROJECT_DIR": variables["RELIUM_DBT_PROJECT_DIR"],
            "RELIUM_MANIFEST_PATH": variables["RELIUM_MANIFEST_PATH"],
        }
        completed = subprocess.run(
            [sys.executable, "-c", self.script],
            cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(
            completed.returncode, 0,
            f"the workflow's own resolution step refused these variables:"
            f"\n{completed.stdout}{completed.stderr}")
        written = Path(output_path).read_text(encoding="utf-8")
        os.unlink(output_path)
        return dict(
            line.split("=", 1) for line in written.splitlines() if "=" in line)

    @staticmethod
    def _lay_out(root, project_dir):
        """A checkout as dbt would leave it after `dbt compile`."""
        project = Path(root) if project_dir == "." else Path(root) / project_dir
        project.mkdir(parents=True, exist_ok=True)
        (project / "dbt_project.yml").write_text("name: demo\n", encoding="utf-8")
        target = project / "target"
        target.mkdir(exist_ok=True)
        manifest = target / "manifest.json"
        manifest.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
        return manifest

    def test_a_nested_project_resolves_to_the_manifest_dbt_wrote(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self._lay_out(root, "analytics")
            variables = ci_variables_for(
                project_dir="analytics",
                manifest_path="analytics/target/manifest.json",
                api_url="https://api.relium.example")

            resolved = self._resolve(root, variables)

            self.assertEqual(Path(resolved["manifest_path"]).resolve(),
                             manifest.resolve())
            self.assertEqual(resolved["project_dir"], "analytics")

    def test_a_root_project_resolves_to_the_manifest_dbt_wrote(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self._lay_out(root, ".")
            variables = ci_variables_for(
                project_dir=".", manifest_path="target/manifest.json",
                api_url="https://api.relium.example")

            resolved = self._resolve(root, variables)

            self.assertEqual(Path(resolved["manifest_path"]).resolve(),
                             manifest.resolve())
            self.assertEqual(resolved["project_dir"], ".")

    def test_the_previous_project_relative_value_would_have_failed(self):
        """The regression, demonstrated rather than described.

        Pinning the bug's behaviour is what stops somebody "simplifying" the
        fix back into it: the stripped value resolves to a path that is not
        where dbt writes, and the workflow's own `test -f` guard is what turned
        that into a red job.
        """
        with tempfile.TemporaryDirectory() as root:
            manifest = self._lay_out(root, "analytics")
            stripped = {
                "RELIUM_DBT_PROJECT_DIR": "analytics",
                # What ci_variables_for used to return.
                "RELIUM_MANIFEST_PATH": "target/manifest.json",
            }

            resolved = self._resolve(root, stripped)

            self.assertNotEqual(Path(resolved["manifest_path"]).resolve(),
                                manifest.resolve())
            self.assertFalse(Path(resolved["manifest_path"]).exists())

    def test_relium_yml_and_the_variable_share_one_base(self):
        """Both reach the same `root / configured_manifest` expression.

        relium.yml's manifest_path is repository-relative and is written that
        way by render_relium_yml. The workflow reads it as the fallback for the
        same variable, through the same line. Two bases for one expression is
        not a thing that can be true, which is the whole argument for the fix.
        """
        from agent.api.repository_onboarding import render_relium_yml

        rendered = render_relium_yml(
            manifest_path="analytics/target/manifest.json",
            enforcement_mode="shadow")
        self.assertIn("analytics/target/manifest.json", rendered)

        with tempfile.TemporaryDirectory() as root:
            manifest = self._lay_out(root, "analytics")
            (Path(root) / "relium.yml").write_text(rendered, encoding="utf-8")

            # No RELIUM_MANIFEST_PATH: the workflow falls back to relium.yml.
            resolved = self._resolve(root, {
                "RELIUM_DBT_PROJECT_DIR": "analytics",
                "RELIUM_MANIFEST_PATH": "",
            })

            self.assertEqual(Path(resolved["manifest_path"]).resolve(),
                             manifest.resolve())


if __name__ == "__main__":
    unittest.main()
