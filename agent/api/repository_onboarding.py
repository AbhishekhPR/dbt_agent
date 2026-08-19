"""Repository selection, dbt configuration and CI credentials.

Phase 3 of the onboarding backend. Turns a verified tenant-to-installation
binding into a configured repository that CI can submit manifests for.

###################################################################
# THE BROWSER NEVER NAMES A REPOSITORY RELIUM WILL TRUST.         #
###################################################################

Every operation resolves the repository through this chain, server-side, on
every request:

    verified Clerk token
      -> Relium tenant                     (from the organization in the token)
        -> tenant_github_installations     (bound in Phase 2, verified)
          -> GET /installation/repositories with an INSTALLATION token
            -> the repository, by GitHub's numeric id

The browser supplies one thing: a numeric id. It is treated as a claim and
checked against the set GitHub returns for this tenant's own installation. A id
outside that set is indistinguishable from one that does not exist — both are
404, matching the non-disclosure policy the rest of the API already applies.

WHAT IS NEVER USED FOR AUTHORIZATION
    a repository name or full name, a GitHub organization name, a Clerk
    organization name, an email address, or any tenant id from a request.
All are mutable, unverified, or attacker-chosen. A repository name in
particular can be released and re-registered by somebody else the same day.

REUSED, NOT REINVENTED
    validate_repository_relative_path   the exact path rule the GitHub App
                                        uses when it reads a customer's config
    load_repository_config              the real relium.yml loader — generated
                                        YAML is parsed back through it, so a
                                        file Relium produces cannot be one the
                                        App would later reject
    issue_ci_token                      the existing scoped-token mechanism;
                                        only sha256(secret) is ever stored
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timezone

from agent.github_app.config import (
    DEFAULT_MANIFEST_PATH, RepositoryConfigError, load_repository_config,
    validate_repository_relative_path,
)

logger = logging.getLogger(__name__)

#: The file whose presence defines a dbt project. Not guessed at: this is what
#: dbt itself requires at the root of a project.
DBT_PROJECT_FILE = "dbt_project.yml"

#: Where a dbt project is looked for, in order. Bounded deliberately — an
#: unbounded search would be a per-request tree walk of a customer repository.
#: A project somewhere else is still configurable; it is just not auto-filled.
DBT_SEARCH_DIRECTORIES = (".", "dbt", "analytics", "transform", "warehouse")

#: The name the customer's workflow reads the CI credential from.
CI_TOKEN_SECRET_NAME = "RELIUM_CI_TOKEN"


class RepositoryOnboardingError(Exception):
    """Carries a stable machine-readable code. Never a credential."""

    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


CODE_REPOSITORY_NOT_FOUND = "repository_not_found"
CODE_NO_INSTALLATION = "github_installation_required"
CODE_NO_REPOSITORY_SELECTED = "repository_not_selected"
CODE_INVALID_PATH = "invalid_path"
CODE_INVALID_CONFIG = "invalid_configuration"
CODE_GITHUB_UNAVAILABLE = "github_unavailable"
CODE_CONFIGURATION_REQUIRED = "configuration_required"
CODE_CI_TOKEN_REQUIRED = "ci_token_required"


@dataclass(frozen=True)
class CiCredential:
    """The outcome of issuing a CI token.

    ``secret`` is populated ONLY when delivery is ``display_once`` — the
    fallback path where the customer must create the repository secret
    themselves. It is never stored, never logged, and never returned again.
    """

    token_id: str
    delivery: str
    secret: str | None
    secret_name: str = CI_TOKEN_SECRET_NAME


class ActionsSecretUnavailable(Exception):
    """Relium cannot write the repository secret itself.

    Carries the reason so the caller can record why the flow fell back to
    showing the value once, rather than silently choosing the weaker path.
    """


class NoSecretWriter:
    """The secret writer that refuses, and says exactly what is missing.

    ###############################################################
    # WRITING THE CI SECRET DIRECTLY IS THE PREFERRED DESIGN.      #
    # IT IS NOT AVAILABLE, AND THIS SAYS SO RATHER THAN PRETEND.   #
    ###############################################################

    In the preferred flow the backend issues the token and writes it straight
    to the repository's Actions secrets, so the value never enters a browser,
    a DOM node, or a clipboard. Three things stand between here and there, and
    every one is a deliberate decision rather than a coding task:

      1. THE APP HAS NO SECRETS PERMISSION.
         Production permissions are Checks: write, Contents: read, Issues:
         write, Metadata: read, Pull requests: write. Adding Secrets: write
         widens what a compromise of the App private key reaches, and suspends
         every existing installation until an owner accepts the new scope.

      2. THE APPROVED PERMISSION SET WOULD REFUSE THE TOKEN.
         agent/github_app/auth.py validates every installation token against
         REQUIRED_INSTALLATION_TOKEN_PERMISSIONS and rejects ANY unapproved
         permission — it fails closed. A token carrying `secrets` would be
         refused, breaking PR review, until that set is deliberately changed.

      3. GITHUB REQUIRES A LIBSODIUM SEALED BOX.
         Actions secrets are encrypted to the repository's public key with
         crypto_box_seal. `cryptography` does not implement it, so this needs
         PyNaCl added to the hash-pinned dependency set. A hand-rolled
         approximation is not an option: a subtly wrong sealed box produces a
         secret that decrypts to nothing and a CI failure nobody can explain.

    Until all three are done, the honest behaviour is to fall back and record
    that we did. The interface exists so that wiring a real writer later is a
    small, reviewable change rather than a redesign.
    """

    available = False

    def write(self, *, owner, repository, name, value, installation_id):
        raise ActionsSecretUnavailable(
            "the Relium GitHub App does not hold the Secrets permission")


@dataclass(frozen=True)
class AuthorizedRepository:
    """A repository this tenant's installation genuinely grants access to."""

    github_repository_id: int
    owner_login: str
    name: str
    full_name: str
    default_branch: str
    private: bool
    installation_id: int
    head_sha: str | None = None


class RepositoryOnboardingService:
    def __init__(self, *, client, jwt_factory, installation_token_factory=None,
                 secret_writer=None, clock=None):
        self._client = client
        self._jwt_factory = jwt_factory
        self._installation_token = (
            installation_token_factory or self._default_installation_token)
        self._secret_writer = secret_writer or NoSecretWriter()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _default_installation_token(self, installation_id):
        from agent.github_app.auth import AuthenticationError, get_installation_token

        try:
            return get_installation_token(
                self._client, installation_id, self._jwt_factory())
        except AuthenticationError:
            # Includes the fail-closed permission check. An installation whose
            # granted permissions no longer match the approved set must not be
            # used, not quietly used anyway.
            raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE) from None

    # -- listing ------------------------------------------------------------

    def list_repositories(self, store, tenant_id):
        """Every repository this tenant's installations authorise.

        Produced entirely server-side. The result is the authorization source
        for every later operation, so it is derived from installation tokens
        and never from anything a caller sent.
        """
        bindings = store.tenant_github_installations(tenant_id)
        if not bindings:
            raise RepositoryOnboardingError(CODE_NO_INSTALLATION)

        repositories = []
        for binding in bindings:
            if binding["status"] != "active":
                # A suspended installation grants nothing. Listing its
                # repositories would offer choices that cannot work.
                continue
            repositories.extend(
                self._installation_repositories(binding["github_installation_id"]))
        cached = ({row["github_repository_id"]: row
                   for row in store.tenant_repository_detections(tenant_id)}
                  if hasattr(store, "tenant_repository_detections") else {})
        for repository in repositories:
            current = cached.get(repository.github_repository_id)
            if (current and current.get("dbt_detected") is not None
                    and current.get("default_branch") == repository.default_branch
                    and current.get("dbt_checked_commit_sha") == repository.head_sha):
                continue
            detection = self.detect_dbt_project(repository)
            if hasattr(store, "upsert_tenant_repository_detection"):
                store.upsert_tenant_repository_detection(
                    tenant_id=tenant_id,
                    github_repository_id=repository.github_repository_id,
                    github_installation_id=repository.installation_id,
                    owner_login=repository.owner_login,
                    name=repository.name,
                    default_branch=repository.default_branch,
                    private=repository.private,
                    dbt_detected=detection["detected"],
                    dbt_project_dir=detection["project_dir"],
                    dbt_checked_at=self._clock(),
                    dbt_checked_commit_sha=repository.head_sha,
                )
        return repositories

    def _installation_repositories(self, installation_id):
        from agent.github_app.client import GitHubAPIError

        token = self._installation_token(installation_id)
        client = self._client.with_token(token)
        found = []
        seen_total = None
        for page in range(1, 11):
            try:
                document = self._client.list_installation_repositories(
                    token, page=page)
            except GitHubAPIError:
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE) from None
            if not isinstance(document, dict):
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE)
            batch = document.get("repositories")
            if not isinstance(batch, list):
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE)
            for item in batch:
                parsed = _parse_repository(item, installation_id)
                if parsed is not None:
                    found.append(parsed)
            seen_total = document.get("total_count")
            if not batch or (isinstance(seen_total, int) and len(found) >= seen_total):
                break
        enriched = []
        for repository in found:
            if not hasattr(self._client, "get_branch"):
                enriched.append(repository)
                continue
            try:
                branch = client.get_branch(
                    repository.owner_login, repository.name,
                    repository.default_branch)
            except GitHubAPIError:
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE) from None
            try:
                sha = branch["commit"]["sha"]
            except (KeyError, TypeError):
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE) from None
            if not isinstance(sha, str) or not sha:
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE)
            enriched.append(dataclass_replace(repository, head_sha=sha))
        return enriched

    # -- authorization ------------------------------------------------------

    def authorized_repository(self, store, tenant_id, github_repository_id):
        """Resolve an id to a repository this tenant may actually configure.

        THE ONE PLACE repository authorization is decided. Every mutating
        operation goes through it, so there is no route that could forget.

        Raises ``repository_not_found`` for an id that is unknown, belongs to
        another tenant, or is outside this tenant's installation — all
        identical, because distinguishing them would disclose the existence of
        a private repository to someone with no access to it.
        """
        if isinstance(github_repository_id, bool):
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)
        if not isinstance(github_repository_id, int) or github_repository_id <= 0:
            raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)

        for repository in self.list_repositories(store, tenant_id):
            if repository.github_repository_id == github_repository_id:
                return repository
        raise RepositoryOnboardingError(CODE_REPOSITORY_NOT_FOUND)

    # -- selection ----------------------------------------------------------

    def select_repository(self, store, tenant_id, github_repository_id):
        """Record the tenant's chosen repository, after authorizing it."""
        from agent.postgres_lifecycle_store import TenantRepositoryConflict

        repository = self.authorized_repository(
            store, tenant_id, github_repository_id)
        detection = self.detect_dbt_project(repository)
        try:
            record = store.select_tenant_repository(
                repository.github_repository_id,
                tenant_id=tenant_id,
                github_installation_id=repository.installation_id,
                owner_login=repository.owner_login,
                name=repository.name,
                default_branch=repository.default_branch,
                private=repository.private,
                dbt_detected=detection["detected"],
                dbt_project_dir=detection["project_dir"],
                dbt_checked_at=self._clock(),
            )
        except TenantRepositoryConflict as exc:
            # Claimed by another tenant. Non-disclosing: the caller learns that
            # they cannot have it, not who does.
            logger.info("repository_claimed_by_another_tenant",
                        extra={"operation": "select_repository"})
            raise RepositoryOnboardingError(
                CODE_REPOSITORY_NOT_FOUND, str(exc)) from None
        return record

    # -- dbt detection ------------------------------------------------------

    def detect_dbt_project(self, repository):
        """Look for a dbt_project.yml in a bounded set of directories.

        Reports what was seen, never what is likely. A repository with no
        detectable project is still configurable by hand — the customer may
        know where it is — so this fills in a suggestion rather than gating.
        """
        from agent.github_app.client import GitHubAPIError, GitHubNotFoundError

        token = self._installation_token(repository.installation_id)
        client = self._client.with_token(token)
        for directory in DBT_SEARCH_DIRECTORIES:
            path = (DBT_PROJECT_FILE if directory == "."
                    else f"{directory}/{DBT_PROJECT_FILE}")
            try:
                content = client.get_file(
                    repository.owner_login, repository.name, path,
                    repository.default_branch)
            except GitHubNotFoundError:
                continue
            except GitHubAPIError:
                # Unreachable is not absent. Saying "no dbt project" during an
                # outage would send the customer to fix the wrong thing.
                raise RepositoryOnboardingError(CODE_GITHUB_UNAVAILABLE) from None
            if content is not None:
                return {"detected": True, "project_dir": directory,
                        "manifest_path": _default_manifest_for(directory)}
        return {"detected": False, "project_dir": None, "manifest_path": None}

    # -- configuration ------------------------------------------------------

    def configure_repository(self, store, tenant_id, github_repository_id, *,
                             project_dir, manifest_path, enforcement_mode):
        """Validate and persist the dbt configuration.

        Validation is the backend's own, not a second implementation of it:
        paths go through validate_repository_relative_path, and the rendered
        relium.yml is parsed back through load_repository_config. A file this
        produces therefore cannot be one the GitHub App would later reject.
        """
        self.authorized_repository(store, tenant_id, github_repository_id)

        project_dir = _validate_project_dir(project_dir)
        manifest_path = _validate_manifest_path(manifest_path)
        enforcement_mode = _validate_enforcement_mode(enforcement_mode)

        rendered = render_relium_yml(manifest_path=manifest_path,
                                     enforcement_mode=enforcement_mode)
        # Round-trip through the REAL loader. If this raises, we were about to
        # hand the customer a file their own Relium install would refuse.
        try:
            parsed = load_repository_config(rendered)
        except RepositoryConfigError as exc:
            raise RepositoryOnboardingError(CODE_INVALID_CONFIG, str(exc)) from None
        if parsed.manifest_path != manifest_path:
            raise RepositoryOnboardingError(
                CODE_INVALID_CONFIG,
                "the generated configuration did not round-trip")

        return store.configure_tenant_repository(
            github_repository_id, tenant_id=tenant_id, project_dir=project_dir,
            manifest_path=manifest_path, enforcement_mode=enforcement_mode,
            configured_at=self._clock())

    # -- CI credential ------------------------------------------------------

    def issue_ci_credential(self, store, tenant_id, github_repository_id, *,
                            force=False):
        """Issue the repository-scoped CI token, preferring not to show it.

        Uses the existing issue_ci_token, so the secret is generated
        server-side, only sha256(secret) is persisted, and the scope is `ci` —
        which grants manifest submission and nothing else. A leaked CI token
        cannot read the dashboard or perform governance.
        """
        from agent.collector.provisioning import issue_ci_token

        repository = self.authorized_repository(
            store, tenant_id, github_repository_id)
        stored = store.tenant_repository(tenant_id, github_repository_id)
        if stored is None or not stored.get("manifest_path"):
            # Issuing a credential for an unconfigured repository would hand
            # out a token nothing can use yet.
            raise RepositoryOnboardingError(CODE_CONFIGURATION_REQUIRED)

        if stored.get("ci_token_id") and not force:
            # Already issued. Not re-minted, because that would leave the
            # customer holding a credential that silently stopped working.
            return CiCredential(token_id=stored["ci_token_id"],
                                delivery=stored.get("ci_token_delivery")
                                or "display_once",
                                secret=None)

        previous = stored.get("ci_token_id")
        token_id, presented = issue_ci_token(
            store,
            organization_id=repository.owner_login,
            repository_id=repository.name,
            description=f"Relium onboarding — {repository.full_name}")

        # Revoke the old one in the same operation. Otherwise a re-issue leaves
        # live credentials the customer cannot see and cannot revoke.
        if previous:
            try:
                store.revoke_service_token(previous)
            except Exception:  # pragma: no cover - revocation is best effort
                logger.error("ci_token_revocation_failed",
                             extra={"error_category": "internal"})

        delivery = "display_once"
        secret = presented
        if getattr(self._secret_writer, "available", False):
            try:
                self._secret_writer.write(
                    owner=repository.owner_login, repository=repository.name,
                    name=CI_TOKEN_SECRET_NAME, value=presented,
                    installation_id=repository.installation_id)
                delivery = "actions_secret"
                # The preferred path: the value never enters the browser.
                secret = None
            except ActionsSecretUnavailable:
                logger.info("ci_secret_write_unavailable",
                            extra={"operation": "issue_ci_credential"})

        store.record_tenant_repository_ci_token(
            github_repository_id, tenant_id=tenant_id, ci_token_id=token_id,
            delivery=delivery, issued_at=self._clock())

        # Deliberately no logging of the token or any part of it: a partial
        # credential in a log narrows a brute force.
        logger.info("ci_token_issued", extra={
            "operation": "issue_ci_credential", "delivery": delivery})
        return CiCredential(token_id=token_id, delivery=delivery, secret=secret)

    # -- completion ---------------------------------------------------------

    def complete_onboarding(self, store, tenant_id, clerk_user_id):
        """Mark setup finished, after re-checking the preconditions.

        IDEMPOTENT: an already-complete tenant returns its original
        completion rather than an error, so a refreshed final step is not a
        failure.

        The preconditions are re-checked here rather than trusted from the UI.
        A caller hitting this endpoint directly must not be able to mark an
        unconfigured tenant complete and land on an empty dashboard.
        """
        existing = store.onboarding_state_for_tenant(tenant_id)
        if existing is not None and existing.get("completed_at") is not None:
            return {"complete": True,
                    "completed_at": existing["completed_at"],
                    "repository_id": existing.get("completed_repository_id"),
                    "created": False}

        if not store.tenant_github_installations(tenant_id):
            raise RepositoryOnboardingError(CODE_NO_INSTALLATION)

        configured = store.configured_tenant_repository(tenant_id)
        if configured is None:
            raise RepositoryOnboardingError(CODE_NO_REPOSITORY_SELECTED)
        if not configured.get("manifest_path"):
            raise RepositoryOnboardingError(CODE_CONFIGURATION_REQUIRED)
        if not configured.get("ci_token_id"):
            raise RepositoryOnboardingError(CODE_CI_TOKEN_REQUIRED)

        completed = store.complete_tenant_onboarding(
            tenant_id,
            completed_at=self._clock(),
            repository_id=configured["github_repository_id"],
            clerk_user_id=clerk_user_id)
        return {"complete": True, "completed_at": completed["completed_at"],
                "repository_id": completed.get("completed_repository_id"),
                "created": completed["created"]}


# --------------------------------------------------------------- validation

def _validate_project_dir(value):
    """The CI variable RELIUM_DBT_PROJECT_DIR, not a relium.yml key.

    `.` means the project is at the repository root, which is legitimate and
    which validate_repository_relative_path rejects — correctly, since it is
    not a path to a file inside the repository. So it is special-cased here and
    everything else goes through the shared rule.
    """
    if value is None or (isinstance(value, str) and value.strip() in ("", ".")):
        return "."
    try:
        return validate_repository_relative_path(value, field_name="project_dir")
    except RepositoryConfigError as exc:
        raise RepositoryOnboardingError(CODE_INVALID_PATH, str(exc)) from None


def _validate_manifest_path(value):
    if value is None or not str(value).strip():
        value = DEFAULT_MANIFEST_PATH
    try:
        return validate_repository_relative_path(value, field_name="manifest_path")
    except RepositoryConfigError as exc:
        raise RepositoryOnboardingError(CODE_INVALID_PATH, str(exc)) from None


def _validate_enforcement_mode(value):
    if value is None:
        return "shadow"
    if value not in ("shadow", "enforce"):
        raise RepositoryOnboardingError(
            CODE_INVALID_CONFIG,
            "enforcement_mode must be shadow or enforce")
    return value


def render_relium_yml(*, manifest_path, enforcement_mode):
    """The customer's relium.yml, using only keys the real loader accepts.

    _ALLOWED_KEYS in agent/github_app/config.py is {version, enabled,
    manifest_path, mode, enforcement_mode, evidence_policy} and an unknown key
    is a hard error. Nothing is invented here: `project_dir` in particular is
    absent, because it is a CI variable and putting it in the YAML would
    produce a file the App refuses to load.
    """
    return (
        "# Managed by Relium onboarding.\n"
        "version: 1\n"
        f"manifest_path: {manifest_path}\n"
        f"enforcement_mode: {enforcement_mode}\n"
    )


def ci_variables_for(*, project_dir, manifest_path, api_url):
    """The repository VARIABLES the workflow needs. Never the secret.

    RELIUM_MANIFEST_PATH is relative to RELIUM_DBT_PROJECT_DIR, because the
    workflow has already changed into the project directory. relium.yml's
    manifest_path is repository-relative. The two differ and both are correct;
    conflating them breaks the handoff.
    """
    within_project = manifest_path
    if project_dir and project_dir != ".":
        prefix = f"{project_dir}/"
        if manifest_path.startswith(prefix):
            within_project = manifest_path[len(prefix):]
    return {
        "RELIUM_DBT_PROJECT_DIR": project_dir or ".",
        "RELIUM_MANIFEST_PATH": within_project,
        "RELIUM_API_URL": api_url,
    }


def _default_manifest_for(directory):
    if directory == ".":
        return DEFAULT_MANIFEST_PATH
    return f"{directory}/{DEFAULT_MANIFEST_PATH}"


def _parse_repository(item, installation_id):
    """Read GitHub's repository object, or skip it.

    A repository without a usable numeric id is dropped rather than stored with
    a name standing in for identity.
    """
    if not isinstance(item, dict):
        return None
    identifier = item.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        return None
    if identifier <= 0:
        return None
    name = item.get("name")
    owner = item.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(owner_login, str) or not owner_login:
        return None
    default_branch = item.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        default_branch = "main"
    return AuthorizedRepository(
        github_repository_id=identifier,
        owner_login=owner_login,
        name=name,
        full_name=f"{owner_login}/{name}",
        default_branch=default_branch,
        private=bool(item.get("private")),
        installation_id=installation_id,
    )


def repositories_payload(store, tenant_id, repositories):
    """The listing as the API exposes it, merged with what is stored."""
    stored = {row["github_repository_id"]: row
              for row in store.tenant_repositories(tenant_id)}
    detections = ({row["github_repository_id"]: row
                  for row in store.tenant_repository_detections(tenant_id)}
                 if hasattr(store, "tenant_repository_detections") else {})
    payload = []
    for repository in repositories:
        record = stored.get(repository.github_repository_id, {})
        detection = detections.get(repository.github_repository_id, record)
        payload.append({
            "repository_id": repository.github_repository_id,
            "full_name": repository.full_name,
            "owner": repository.owner_login,
            "name": repository.name,
            "default_branch": repository.default_branch,
            "private": repository.private,
            "dbt_detected": detection.get("dbt_detected"),
            "dbt_project_dir": detection.get("dbt_project_dir"),
            "selected": bool(record),
            "configured": bool(record.get("manifest_path")),
        })
    payload.sort(key=lambda entry: entry["full_name"])
    return payload


def configuration_payload(store, tenant_id, *, api_url=""):
    """The dbt configuration section of the onboarding state.

    Reports ``ci_token_issued`` as a boolean and the delivery route. The token
    itself is not here, is not stored, and is not recoverable.
    """
    record = store.configured_tenant_repository(tenant_id)
    if record is None:
        return None
    manifest_path = record.get("manifest_path")
    project_dir = record.get("project_dir") or "."
    return {
        "repository_id": record["github_repository_id"],
        "full_name": f"{record['owner_login']}/{record['name']}",
        "project_dir": project_dir,
        "manifest_path": manifest_path,
        "enforcement_mode": record.get("enforcement_mode") or "shadow",
        "relium_yml": render_relium_yml(
            manifest_path=manifest_path,
            enforcement_mode=record.get("enforcement_mode") or "shadow"),
        "ci_variables": ci_variables_for(
            project_dir=project_dir, manifest_path=manifest_path,
            api_url=api_url),
        "ci_token_issued": bool(record.get("ci_token_id")),
        "ci_token_delivery": record.get("ci_token_delivery"),
        "configured_at": record["configured_at"].isoformat()
        if record.get("configured_at") else None,
    }
