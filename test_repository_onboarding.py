"""Repository selection, dbt configuration, CI credentials and completion.

Phase 3. The central claim under test:

    A repository is never authorized by anything the browser supplied. Every
    operation resolves tenant -> installation -> repository server-side, and an
    id outside that chain is a non-disclosing 404.

Real PostgreSQL; GitHub scripted so unauthorized repositories, missing dbt
projects and outages can each be produced deliberately.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
API_URL = "https://api.relium.test"

ACME_INSTALLATION = 111111
GLOBEX_INSTALLATION = 222222

ACME_REPO = 900001          # acme-analytics/analytics, has dbt
ACME_REPO_NO_DBT = 900002   # acme-analytics/warehouse, no dbt
GLOBEX_REPO = 900003        # globex-data/models, another tenant entirely
UNRELATED_REPO = 900999     # in no installation at all


class _FakeClient:
    """A scripted GitHub. Each installation sees only its own repositories."""

    def __init__(self):
        self.repositories = {
            ACME_INSTALLATION: [
                {"id": ACME_REPO, "name": "analytics", "private": True,
                 "default_branch": "main",
                 "owner": {"login": "acme-analytics"}},
                {"id": ACME_REPO_NO_DBT, "name": "warehouse", "private": True,
                 "default_branch": "main",
                 "owner": {"login": "acme-analytics"}},
            ],
            GLOBEX_INSTALLATION: [
                {"id": GLOBEX_REPO, "name": "models", "private": True,
                 "default_branch": "main", "owner": {"login": "globex-data"}},
            ],
        }
        # Which repositories contain a dbt project, and where.
        self.dbt = {("acme-analytics", "analytics"): "analytics",
                    ("globex-data", "models"): "."}
        self.token = None
        self.unavailable = False
        self.secrets_written = []

    def with_token(self, token):
        clone = _FakeClient.__new__(_FakeClient)
        clone.__dict__.update(self.__dict__)
        clone.token = token
        return clone

    def get_app(self, app_jwt):
        return {"id": 1, "slug": "relium-production-test"}

    def get_installation(self, installation_id, app_jwt):
        from agent.github_app.client import GitHubNotFoundError

        if installation_id not in self.repositories:
            raise GitHubNotFoundError("unknown installation")
        return {"id": installation_id, "app_id": 1,
                "repository_selection": "selected",
                "account": {"id": 1, "login": "x", "type": "Organization"}}

    def list_installation_repositories(self, installation_token, *, page=1,
                                       per_page=100):
        from agent.github_app.client import GitHubAPIError

        if self.unavailable:
            raise GitHubAPIError("GitHub is unavailable")
        installation_id = int(str(installation_token).rsplit("-", 1)[-1])
        entries = self.repositories.get(installation_id, [])
        if page > 1:
            entries = []
        return {"total_count": len(self.repositories.get(installation_id, [])),
                "repositories": entries}

    def get_file(self, owner, repository, path, ref):
        from agent.github_app.client import GitHubNotFoundError

        directory = self.dbt.get((owner, repository))
        if directory is None:
            raise GitHubNotFoundError("no dbt project")
        expected = ("dbt_project.yml" if directory == "."
                    else f"{directory}/dbt_project.yml")
        if path != expected:
            raise GitHubNotFoundError("not here")
        return b"name: analytics\nversion: '1.0'\n"


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; repository onboarding needs PostgreSQL")
class RepositoryOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        from agent.api.repository_onboarding import RepositoryOnboardingService

        for table in ("tenant_repositories", "tenant_github_installations",
                      "github_installations", "api_service_tokens",
                      "tenant_onboarding_state", "tenants"):
            self.store.connection.execute(f"DELETE FROM {table}")

        self.client = _FakeClient()
        self.service = RepositoryOnboardingService(
            client=self.client, jwt_factory=lambda: "app-jwt",
            installation_token_factory=lambda i: f"installation-token-{i}",
            clock=lambda: NOW)

        self.acme = self._tenant("org_2acme", "Acme", ACME_INSTALLATION)
        self.globex = self._tenant("org_2globex", "Globex", GLOBEX_INSTALLATION)

    def _tenant(self, clerk_org, name, installation_id):
        tenant_id = self.store.upsert_tenant_for_clerk_organization(
            clerk_org, organization_name=name)["tenant_id"]
        self.store.record_github_installation(
            installation_id, github_account_id=installation_id,
            github_account_login=name.lower(),
            github_account_type="Organization")
        self.store.bind_github_installation_to_tenant(
            installation_id, tenant_id=tenant_id,
            bound_by_clerk_user_id=f"user_{clerk_org}",
            verified_github_user_id=installation_id)
        return tenant_id

    def _refused(self, code):
        from agent.api.repository_onboarding import RepositoryOnboardingError

        test = self

        class _Context:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                test.assertIsNotNone(exc, f"expected refusal {code}")
                test.assertIsInstance(exc, RepositoryOnboardingError)
                test.assertEqual(exc.code, code)
                return True

        return _Context()

    def _configure(self, tenant, repo_id, **overrides):
        self.service.select_repository(self.store, tenant, repo_id)
        options = {"project_dir": "analytics",
                   "manifest_path": "analytics/target/manifest.json",
                   "enforcement_mode": "shadow"}
        options.update(overrides)
        return self.service.configure_repository(
            self.store, tenant, repo_id, **options)

    # -- listing and authorization -----------------------------------------

    def test_listing_returns_only_this_tenants_repositories(self):
        acme = {r.github_repository_id
                for r in self.service.list_repositories(self.store, self.acme)}
        globex = {r.github_repository_id
                  for r in self.service.list_repositories(self.store, self.globex)}
        self.assertEqual(acme, {ACME_REPO, ACME_REPO_NO_DBT})
        self.assertEqual(globex, {GLOBEX_REPO})

    def test_listing_without_an_installation_is_refused(self):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            "org_2bare", organization_name="Bare")["tenant_id"]
        with self._refused("github_installation_required"):
            self.service.list_repositories(self.store, tenant)

    def test_a_suspended_installation_grants_nothing(self):
        self.store.set_github_installation_status(
            ACME_INSTALLATION, "suspended", suspended_at=NOW)
        self.assertEqual(
            self.service.list_repositories(self.store, self.acme), [])

    def test_github_unavailable_is_not_an_empty_list(self):
        """An outage must not read as 'you have no repositories'."""
        self.client.unavailable = True
        with self._refused("github_unavailable"):
            self.service.list_repositories(self.store, self.acme)

    # -- THE CENTRAL ATTACKS ------------------------------------------------

    def test_another_tenants_repository_is_not_found(self):
        """Acme names Globex's repository id. Non-disclosing 404."""
        with self._refused("repository_not_found"):
            self.service.authorized_repository(self.store, self.acme, GLOBEX_REPO)

    def test_a_repository_outside_any_installation_is_not_found(self):
        with self._refused("repository_not_found"):
            self.service.authorized_repository(self.store, self.acme,
                                               UNRELATED_REPO)

    def test_a_spoofed_repository_id_cannot_be_selected(self):
        with self._refused("repository_not_found"):
            self.service.select_repository(self.store, self.acme, GLOBEX_REPO)
        self.assertEqual(self.store.tenant_repositories(self.acme), [])

    def test_a_spoofed_repository_id_cannot_be_configured(self):
        with self._refused("repository_not_found"):
            self.service.configure_repository(
                self.store, self.acme, GLOBEX_REPO,
                project_dir=".", manifest_path="target/manifest.json",
                enforcement_mode="shadow")

    def test_a_spoofed_repository_id_cannot_be_issued_a_ci_token(self):
        with self._refused("repository_not_found"):
            self.service.issue_ci_credential(self.store, self.acme, GLOBEX_REPO)
        count = self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM api_service_tokens").fetchone()["c"]
        self.assertEqual(count, 0, "a token was minted for an unauthorized repo")

    def test_malformed_repository_ids_are_not_found(self):
        for bad in (0, -1, True, False, None, "900001", 1.5, [ACME_REPO]):
            with self._refused("repository_not_found"):
                self.service.authorized_repository(self.store, self.acme, bad)

    def test_a_repository_claimed_by_another_tenant_stays_claimed(self):
        """Even a genuinely authorized id cannot be stolen.

        Both installations are made to expose the same repository, so the
        listing check passes for both tenants — and the database still refuses
        to move it.
        """
        self.client.repositories[GLOBEX_INSTALLATION].append(
            self.client.repositories[ACME_INSTALLATION][0])
        self.service.select_repository(self.store, self.acme, ACME_REPO)
        with self._refused("repository_not_found"):
            self.service.select_repository(self.store, self.globex, ACME_REPO)
        record = self.store.tenant_repository(self.acme, ACME_REPO)
        self.assertIsNotNone(record)
        self.assertIsNone(self.store.tenant_repository(self.globex, ACME_REPO))

    def test_authorization_never_uses_the_repository_name(self):
        """Renaming must not transfer anything. The id is the identity."""
        self.service.select_repository(self.store, self.acme, ACME_REPO)
        self.client.repositories[ACME_INSTALLATION][0]["name"] = "renamed"
        self.client.dbt[("acme-analytics", "renamed")] = "analytics"
        resolved = self.service.authorized_repository(
            self.store, self.acme, ACME_REPO)
        self.assertEqual(resolved.name, "renamed")
        self.assertEqual(resolved.github_repository_id, ACME_REPO)

    # -- dbt detection ------------------------------------------------------

    def test_a_dbt_project_is_detected_and_its_directory_reported(self):
        record = self.service.select_repository(self.store, self.acme, ACME_REPO)
        self.assertIs(record["dbt_detected"], True)
        self.assertEqual(record["dbt_project_dir"], "analytics")

    def test_a_repository_without_dbt_is_reported_as_such(self):
        record = self.service.select_repository(
            self.store, self.acme, ACME_REPO_NO_DBT)
        self.assertIs(record["dbt_detected"], False)
        self.assertIsNone(record["dbt_project_dir"])

    def test_a_repository_without_dbt_can_still_be_configured(self):
        """Detection is a suggestion, not a gate: the customer may know where
        the project is."""
        self._configure(self.acme, ACME_REPO_NO_DBT,
                        project_dir="etl", manifest_path="etl/target/manifest.json")
        record = self.store.tenant_repository(self.acme, ACME_REPO_NO_DBT)
        self.assertEqual(record["manifest_path"], "etl/target/manifest.json")

    def test_github_unavailable_during_detection_does_not_claim_no_dbt(self):
        from agent.github_app.client import GitHubAPIError

        def boom(*args, **kwargs):
            raise GitHubAPIError("unavailable")

        self.client.get_file = boom
        with self._refused("github_unavailable"):
            self.service.select_repository(self.store, self.acme, ACME_REPO)

    # -- path validation ----------------------------------------------------

    def test_invalid_manifest_paths_are_refused(self):
        self.service.select_repository(self.store, self.acme, ACME_REPO)
        # Every one of these escapes the repository, or could not be read
        # back by the GitHub App. A blank path is absent from the list on
        # purpose: the relium.yml loader defaults it, so onboarding does too,
        # and that is asserted by its own test below.
        for bad in ("/etc/passwd", "C:/windows/manifest.json",
                    "../outside/manifest.json", "a/../../escape.json",
                    "target\\manifest.json", "a\x00b", "//escape"):
            with self._refused("invalid_path"):
                self.service.configure_repository(
                    self.store, self.acme, ACME_REPO, project_dir=".",
                    manifest_path=bad, enforcement_mode="shadow")

    def test_invalid_project_dirs_are_refused(self):
        self.service.select_repository(self.store, self.acme, ACME_REPO)
        for bad in ("/abs", "C:/win", "../up", "a\\b", "a/../b"):
            with self._refused("invalid_path"):
                self.service.configure_repository(
                    self.store, self.acme, ACME_REPO, project_dir=bad,
                    manifest_path="target/manifest.json",
                    enforcement_mode="shadow")

    def test_safe_but_untidy_paths_are_normalised_rather_than_refused(self):
        """The shared validator normalises a trailing slash and a `.` segment.

        Both resolve to a path that stays inside the repository, so refusing
        them would reject a configuration that works. What gets STORED is the
        normalised form, which is what the GitHub App will later read.
        """
        for supplied, expected in (("target/manifest.json/", "target/manifest.json"),
                                   ("a/./manifest.json", "a/manifest.json")):
            record = self._configure(self.acme, ACME_REPO, project_dir=".",
                                     manifest_path=supplied)
            self.assertEqual(record["manifest_path"], expected)

    def test_a_root_project_dir_is_accepted(self):
        record = self._configure(self.acme, ACME_REPO, project_dir=".",
                                 manifest_path="target/manifest.json")
        self.assertEqual(record["project_dir"], ".")

    def test_an_unknown_enforcement_mode_is_refused(self):
        self.service.select_repository(self.store, self.acme, ACME_REPO)
        for bad in ("block", "warn", "ENFORCE", "", "yes"):
            with self._refused("invalid_configuration"):
                self.service.configure_repository(
                    self.store, self.acme, ACME_REPO, project_dir=".",
                    manifest_path="target/manifest.json",
                    enforcement_mode=bad)

    def test_the_manifest_path_defaults_to_the_backends_own_default(self):
        from agent.github_app.config import DEFAULT_MANIFEST_PATH

        record = self._configure(self.acme, ACME_REPO, project_dir=".",
                                 manifest_path=None)
        self.assertEqual(record["manifest_path"], DEFAULT_MANIFEST_PATH)

    # -- relium.yml ---------------------------------------------------------

    def test_the_generated_yaml_parses_with_the_real_loader(self):
        """The strongest available check: Relium cannot generate a file its own
        GitHub App would refuse to read."""
        from agent.github_app.config import load_repository_config
        from agent.api.repository_onboarding import render_relium_yml

        rendered = render_relium_yml(
            manifest_path="analytics/target/manifest.json",
            enforcement_mode="enforce")
        parsed = load_repository_config(rendered)
        self.assertEqual(parsed.version, 1)
        self.assertEqual(parsed.manifest_path, "analytics/target/manifest.json")
        self.assertEqual(parsed.enforcement_mode, "enforce")

    def test_the_generated_yaml_uses_only_allowed_keys(self):
        import yaml

        from agent.github_app.config import _ALLOWED_KEYS
        from agent.api.repository_onboarding import render_relium_yml

        rendered = render_relium_yml(manifest_path="target/manifest.json",
                                     enforcement_mode="shadow")
        keys = set(yaml.safe_load(rendered))
        self.assertTrue(keys.issubset(_ALLOWED_KEYS),
                        f"invented keys: {keys - _ALLOWED_KEYS}")

    def test_project_dir_is_not_written_into_the_yaml(self):
        """It is a CI variable. In the YAML it would be an unknown key, and the
        loader rejects unknown keys outright."""
        from agent.api.repository_onboarding import render_relium_yml

        rendered = render_relium_yml(manifest_path="analytics/target/manifest.json",
                                     enforcement_mode="shadow")
        self.assertNotIn("project_dir", rendered)

    def test_ci_variables_keep_the_manifest_path_repository_relative(self):
        from agent.api.repository_onboarding import ci_variables_for

        variables = ci_variables_for(
            project_dir="analytics",
            manifest_path="analytics/target/manifest.json", api_url=API_URL)
        self.assertEqual(variables["RELIUM_DBT_PROJECT_DIR"], "analytics")
        # The workflow resolves this from the checkout root. Stripping the
        # project directory would point a nested dbt project at the wrong file.
        self.assertEqual(variables["RELIUM_MANIFEST_PATH"],
                         "analytics/target/manifest.json")
        self.assertNotIn("RELIUM_CI_TOKEN", variables)

    # -- CI token -----------------------------------------------------------

    def test_a_ci_token_is_issued_once_and_scoped_to_ci(self):
        self._configure(self.acme, ACME_REPO)
        credential = self.service.issue_ci_credential(
            self.store, self.acme, ACME_REPO)
        self.assertTrue(credential.secret.startswith("rlm_"))
        self.assertEqual(credential.delivery, "display_once")

        row = self.store.connection.execute(
            "SELECT scope, organization_id, repository_id FROM api_service_tokens "
            "WHERE token_id = %s", (credential.token_id,)).fetchone()
        self.assertEqual(row["scope"], "ci")
        self.assertEqual(row["organization_id"], "acme-analytics")
        self.assertEqual(row["repository_id"], "analytics")

    def test_only_the_hash_of_the_ci_token_is_stored(self):
        self._configure(self.acme, ACME_REPO)
        credential = self.service.issue_ci_credential(
            self.store, self.acme, ACME_REPO)
        secret = credential.secret.split(".", 1)[1]
        rows = self.store.connection.execute(
            "SELECT secret_hash FROM api_service_tokens").fetchall()
        for row in rows:
            self.assertNotIn(secret, row["secret_hash"])
        # And nothing on the repository row holds it either.
        record = self.store.tenant_repository(self.acme, ACME_REPO)
        self.assertNotIn(secret, str(record))

    def test_the_token_is_not_returned_a_second_time(self):
        self._configure(self.acme, ACME_REPO)
        first = self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        second = self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        self.assertIsNotNone(first.secret)
        self.assertIsNone(second.secret)
        self.assertEqual(first.token_id, second.token_id)

    def test_forcing_a_reissue_revokes_the_previous_token(self):
        """Otherwise a customer accumulates live credentials they cannot see."""
        self._configure(self.acme, ACME_REPO)
        first = self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        second = self.service.issue_ci_credential(
            self.store, self.acme, ACME_REPO, force=True)
        self.assertNotEqual(first.token_id, second.token_id)
        revoked = self.store.connection.execute(
            "SELECT revoked_at FROM api_service_tokens WHERE token_id = %s",
            (first.token_id,)).fetchone()["revoked_at"]
        self.assertIsNotNone(revoked)

    def test_a_ci_token_needs_a_configured_repository(self):
        self.service.select_repository(self.store, self.acme, ACME_REPO)
        with self._refused("configuration_required"):
            self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)

    def test_the_issued_token_authenticates_only_for_manifest_ingest(self):
        """End to end against the real authenticator and capability model."""
        from agent.api.auth import ServiceTokenAuthenticator
        from agent.api.authorization import (
            CI_MANIFEST_INGEST, COLLECTOR_INGEST, DASHBOARD_READ,
            GOVERNANCE_WRITE, CapabilityError, authorize,
        )

        self._configure(self.acme, ACME_REPO)
        credential = self.service.issue_ci_credential(
            self.store, self.acme, ACME_REPO)
        principal = ServiceTokenAuthenticator(self.store).authenticate(
            credential.secret)
        authorize(principal, CI_MANIFEST_INGEST)
        for capability in (COLLECTOR_INGEST, DASHBOARD_READ, GOVERNANCE_WRITE):
            with self.assertRaises(CapabilityError, msg=capability.name):
                authorize(principal, capability)

    def test_the_preferred_secret_path_is_used_when_available(self):
        """When a writer IS available the value never leaves the server."""
        class _Writer:
            available = True

            def __init__(self):
                self.written = []

            def write(self, *, owner, repository, name, value, installation_id):
                self.written.append((owner, repository, name))

        from agent.api.repository_onboarding import RepositoryOnboardingService

        writer = _Writer()
        service = RepositoryOnboardingService(
            client=self.client, jwt_factory=lambda: "app-jwt",
            installation_token_factory=lambda i: f"installation-token-{i}",
            secret_writer=writer, clock=lambda: NOW)
        self._configure(self.acme, ACME_REPO)
        credential = service.issue_ci_credential(self.store, self.acme, ACME_REPO)

        self.assertEqual(credential.delivery, "actions_secret")
        self.assertIsNone(credential.secret,
                          "the token must not reach the browser on this path")
        self.assertEqual(writer.written,
                         [("acme-analytics", "analytics", "RELIUM_CI_TOKEN")])

    def test_a_failing_secret_writer_falls_back_and_records_it(self):
        from agent.api.repository_onboarding import (
            ActionsSecretUnavailable, RepositoryOnboardingService,
        )

        class _Writer:
            available = True

            def write(self, **kwargs):
                raise ActionsSecretUnavailable("no permission")

        service = RepositoryOnboardingService(
            client=self.client, jwt_factory=lambda: "app-jwt",
            installation_token_factory=lambda i: f"installation-token-{i}",
            secret_writer=_Writer(), clock=lambda: NOW)
        self._configure(self.acme, ACME_REPO)
        credential = service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        self.assertEqual(credential.delivery, "display_once")
        self.assertIsNotNone(credential.secret)

    # -- completion ---------------------------------------------------------

    def test_completion_requires_every_precondition(self):
        with self._refused("github_installation_required"):
            self.service.complete_onboarding(
                self.store,
                self.store.upsert_tenant_for_clerk_organization(
                    "org_2empty", organization_name="Empty")["tenant_id"],
                "user_x")

        with self._refused("repository_not_selected"):
            self.service.complete_onboarding(self.store, self.acme, "user_x")

        self._configure(self.acme, ACME_REPO)
        with self._refused("ci_token_required"):
            self.service.complete_onboarding(self.store, self.acme, "user_x")

    def test_completion_succeeds_once_everything_is_in_place(self):
        self._configure(self.acme, ACME_REPO)
        self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        result = self.service.complete_onboarding(self.store, self.acme, "user_a")
        self.assertTrue(result["complete"])
        self.assertTrue(result["created"])
        self.assertEqual(result["repository_id"], ACME_REPO)

    def test_completion_is_idempotent(self):
        self._configure(self.acme, ACME_REPO)
        self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        first = self.service.complete_onboarding(self.store, self.acme, "user_a")
        second = self.service.complete_onboarding(self.store, self.acme, "user_a")
        self.assertEqual(first["completed_at"], second["completed_at"])
        self.assertFalse(second["created"])

    def test_concurrent_completion_completes_exactly_once(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self._configure(self.acme, ACME_REPO)
        self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)

        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def attempt():
            store = None
            try:
                store = PostgresLifecycleStore(DSN)
                barrier.wait(timeout=30)
                result = self.service.complete_onboarding(
                    store, self.acme, "user_a")
                with lock:
                    outcomes.append(result["created"])
            except Exception as exc:
                with lock:
                    outcomes.append(f"error:{type(exc).__name__}")
            finally:
                if store is not None:
                    store.close()

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(outcomes.count(True), 1,
                         f"completion happened more than once: {outcomes}")
        self.assertEqual(outcomes.count(False), 5, outcomes)

    def test_completion_moves_the_step_to_ready(self):
        """The CHECK in migration 0014 refuses a completed row on an
        unfinished step, so this has to move together with completion."""
        self._configure(self.acme, ACME_REPO)
        self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        self.service.complete_onboarding(self.store, self.acme, "user_a")
        state = self.store.onboarding_state_for_tenant(self.acme)
        self.assertEqual(state["current_step"], "ready")
        self.assertIsNotNone(state["completed_at"])

    def test_one_tenants_completion_does_not_complete_another(self):
        self._configure(self.acme, ACME_REPO)
        self.service.issue_ci_credential(self.store, self.acme, ACME_REPO)
        self.service.complete_onboarding(self.store, self.acme, "user_a")
        self.assertIsNone(
            self.store.onboarding_state_for_tenant(self.globex)["completed_at"])

    # -- payload ------------------------------------------------------------

    def test_the_configuration_payload_carries_no_token(self):
        import json as json_module

        from agent.api.repository_onboarding import configuration_payload

        self._configure(self.acme, ACME_REPO)
        credential = self.service.issue_ci_credential(
            self.store, self.acme, ACME_REPO)
        payload = configuration_payload(self.store, self.acme, api_url=API_URL)
        text = json_module.dumps(payload)
        self.assertNotIn(credential.secret, text)
        self.assertNotIn(credential.secret.split(".", 1)[1], text)
        self.assertIs(payload["ci_token_issued"], True)
        self.assertNotIn("RELIUM_CI_TOKEN", payload["ci_variables"])

    def test_the_configuration_payload_is_null_before_configuration(self):
        from agent.api.repository_onboarding import configuration_payload

        self.assertIsNone(
            configuration_payload(self.store, self.acme, api_url=API_URL))


if __name__ == "__main__":
    unittest.main()
