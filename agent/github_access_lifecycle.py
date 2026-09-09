"""Distinct personal, repository, installation and uninstall controls."""
from __future__ import annotations

from agent.github_app.client import GitHubAPIError, GitHubNotFoundError
from agent.lifecycle_authorization import RecentVerificationRequired, require_recent_verification
from agent.workspace_credential_revocation import revoke_current_user_github_identity


EXTERNAL_HISTORY_NOTICE = "historical_external_publications_are_not_erased"


class GitHubAccessBlocked(RuntimeError):
    def __init__(self, category, *, disposition="blocked"):
        super().__init__(category.replace("_", " "))
        self.category, self.disposition = category, disposition


def disconnect_personal_github(*, principal, store):
    _require_recent(principal)
    result = revoke_current_user_github_identity(principal=principal, store=store)
    return {**result, "external_history": EXTERNAL_HISTORY_NOTICE}


class GitHubAccessLifecycle:
    def __init__(self, *, authorizer, store, github_client, github_app_jwt):
        self.authorizer, self.store = authorizer, store
        self.github_client, self.github_app_jwt = github_client, github_app_jwt

    def disconnect_repository(self, *, principal, repository_id):
        _require_recent(principal)
        context = self.authorizer.require_admin_or_owner(principal)
        result = self.store.begin_github_access_operation(
            tenant_id=context.tenant_id,
            initiated_by_clerk_user_id=context.clerk_user_id,
            operation_kind="repository_disconnect",
            github_repository_id=repository_id)
        return {**result, "external_history": EXTERNAL_HISTORY_NOTICE}

    def disconnect_installation(self, *, principal, installation_id):
        _require_recent(principal)
        context = self.authorizer.require_admin_or_owner(principal)
        result = self.store.begin_github_access_operation(
            tenant_id=context.tenant_id,
            initiated_by_clerk_user_id=context.clerk_user_id,
            operation_kind="installation_disconnect",
            github_installation_id=installation_id)
        return {**result, "external_history": EXTERNAL_HISTORY_NOTICE}

    def uninstall(self, *, principal, installation_id):
        _require_recent(principal)
        context = self.authorizer.require_owner(principal)
        operation = self.store.begin_github_access_operation(
            tenant_id=context.tenant_id,
            initiated_by_clerk_user_id=context.clerk_user_id,
            operation_kind="installation_uninstall",
            github_installation_id=installation_id)
        try:
            try:
                self.github_client.delete_installation(
                    installation_id, self.github_app_jwt())
            except GitHubNotFoundError:
                pass
            try:
                self.github_client.get_installation(
                    installation_id, self.github_app_jwt())
            except GitHubNotFoundError:
                result = self.store.complete_github_access_operation(
                    tenant_id=context.tenant_id,
                    operation_id=operation["operation_id"],
                    provider_absence_verified=True)
                return {**result, "external_history": EXTERNAL_HISTORY_NOTICE}
            raise GitHubAccessBlocked("github_not_terminal", disposition="retryable")
        except GitHubAccessBlocked as exc:
            self.store.fail_github_access_operation(
                tenant_id=context.tenant_id, operation_id=operation["operation_id"],
                disposition=exc.disposition, failure_category=exc.category)
            raise
        except GitHubAPIError as exc:
            category = "github_provider_retryable" if exc.retryable or exc.status_code is None \
                or exc.status_code == 429 or (exc.status_code and exc.status_code >= 500) \
                else "github_provider_refused"
            self.store.fail_github_access_operation(
                tenant_id=context.tenant_id, operation_id=operation["operation_id"],
                disposition="retryable" if category.endswith("retryable") else "blocked",
                failure_category=category)
            raise GitHubAccessBlocked(category,
                                      disposition="retryable" if category.endswith("retryable") else "blocked") from None


def _require_recent(principal):
    try:
        require_recent_verification(principal)
    except RecentVerificationRequired as exc:
        raise GitHubAccessBlocked(exc.category) from None
