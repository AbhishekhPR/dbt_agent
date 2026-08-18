"""Entering the dashboard after onboarding, without weakening the dashboard.

###################################################################
# A CLERK SESSION DOES NOT BECOME DASHBOARD ACCESS.               #
###################################################################

Onboarding leaves a customer with a verified Clerk session, a verified GitHub
identity, a bound installation and a configured repository. None of that is
dashboard authorization. The dashboard authorizes against a LIVE GitHub
repository permission, and that is a different question with a different
answer — a person can finish setting up a workspace and still have no access
to the repository it points at, or have read access and no write.

So this bridge does not translate a Clerk session into dashboard access. It
establishes, server-side, whether the human behind the Clerk session has
GitHub access to the tenant's repository — by asking GitHub — and mints the
ordinary dashboard session only if the answer is yes. The resulting session is
the same row, with the same authority, that GitHub sign-in would have produced.

THE CHAIN, ALL SERVER-SIDE
--------------------------
  1. Clerk token verified against Clerk's JWKS
  2. Clerk organization  -> Relium tenant           (from the token, not a body)
  3. Clerk user          -> linked GitHub identity  (established by OAuth)
  4. credential decrypted and still usable
  5. tenant              -> bound GitHub installation
  6. tenant              -> configured repository    (owner/name from OUR record)
  7. GitHub, as the App  -> repository is in that installation
  8. GitHub, as the user -> live repository permission
  9. session scope derived from 6, never from the request

Nothing in the request body names a tenant, a repository, an installation, an
owner or a login. The request has no body at all: everything is resolved from
the verified token and the tenant's own records.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CODE_WORKSPACE_REQUIRED = "workspace_required"
CODE_ONBOARDING_INCOMPLETE = "onboarding_incomplete"
CODE_GITHUB_IDENTITY_REQUIRED = "github_identity_required"
CODE_GITHUB_IDENTITY_UNUSABLE = "github_identity_unusable"
CODE_INSTALLATION_REQUIRED = "github_installation_required"
CODE_REPOSITORY_REQUIRED = "repository_not_configured"
CODE_NO_REPOSITORY_ACCESS = "github_repository_access_required"
CODE_GITHUB_UNAVAILABLE = "github_unavailable"


class DashboardBridgeError(Exception):
    """Carries a stable code. Never a credential, never a repository name."""

    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class DashboardSessionBridge:
    """Establishes a dashboard session for an onboarded tenant."""

    def __init__(self, *, session_manager, session_key, repository_service,
                 environment="production", github_identity=None):
        from agent.api import github_identity as default_identity

        self._sessions = session_manager
        self._key = session_key
        self._repositories = repository_service
        self._environment = environment
        self._identity = github_identity or default_identity

    def establish(self, store, *, clerk_user_id, tenant_id):
        """Run the chain and mint the session, or raise with a stable code."""
        from agent.api.github_identity import (
            GitHubCredentialExpired, GitHubIdentityError,
        )
        from agent.api.repository_onboarding import RepositoryOnboardingError
        from agent.api.session_crypto import CredentialEncryptionError, decrypt
        from agent.api.sessions import SessionError

        # 3. The Clerk user must have proved a GitHub identity. Without one
        #    there is no credential to ask GitHub with, and the honest outcome
        #    is to refuse rather than to trust the tenant record.
        link = store.clerk_github_identity(clerk_user_id)
        if link is None:
            raise DashboardBridgeError(CODE_GITHUB_IDENTITY_REQUIRED)

        # 4. ...and it must still be usable.
        try:
            access_token = decrypt(self._key, link.get("access_token"),
                                   associated=clerk_user_id)
        except CredentialEncryptionError:
            raise DashboardBridgeError(CODE_GITHUB_IDENTITY_UNUSABLE) from None
        if not access_token:
            raise DashboardBridgeError(CODE_GITHUB_IDENTITY_REQUIRED)
        expires_at = link.get("access_expires_at")
        if expires_at is not None and expires_at <= self._sessions._clock():
            raise DashboardBridgeError(CODE_GITHUB_IDENTITY_UNUSABLE)

        # 5. The tenant must have a bound installation. Onboarding completion
        #    implies it, but this is re-checked because an installation can be
        #    removed after onboarding and the binding is what scopes access.
        installations = store.tenant_github_installations(tenant_id)
        if not any(row["status"] == "active" for row in installations):
            raise DashboardBridgeError(CODE_INSTALLATION_REQUIRED)

        # 6. The repository, and its owner and name, come from OUR record of
        #    what this tenant configured — never from the request.
        configured = store.configured_tenant_repository(tenant_id)
        if configured is None or not configured.get("manifest_path"):
            raise DashboardBridgeError(CODE_REPOSITORY_REQUIRED)

        # 7. Re-authorise the repository against the live installation, using
        #    the same one function every repository operation goes through. A
        #    repository that has since been de-selected in GitHub stops being
        #    reachable here too, rather than staying reachable because a row
        #    remembers it.
        try:
            repository = self._repositories.authorized_repository(
                store, tenant_id, configured["github_repository_id"])
        except RepositoryOnboardingError as exc:
            if exc.code == "github_unavailable":
                raise DashboardBridgeError(CODE_GITHUB_UNAVAILABLE) from None
            raise DashboardBridgeError(CODE_REPOSITORY_REQUIRED) from None

        # 8 & 9. Live permission check and session creation. The scope passed
        #        in is derived above; the session manager fetches the
        #        permission itself and refuses if GitHub reports none.
        try:
            established = self._sessions.establish_from_verified_identity(
                store,
                access_token=access_token,
                organization_id=repository.owner_login,
                repository_id=repository.name,
                environment=self._environment,
                refresh_token=decrypt(self._key, link.get("refresh_token"),
                                      associated=clerk_user_id),
                access_expires_at=link.get("access_expires_at"),
                refresh_expires_at=link.get("refresh_expires_at"),
            )
        except SessionError:
            # GitHub says this person cannot see the repository. Their tenant
            # owning it changes nothing.
            raise DashboardBridgeError(CODE_NO_REPOSITORY_ACCESS) from None
        except GitHubCredentialExpired:
            raise DashboardBridgeError(CODE_GITHUB_IDENTITY_UNUSABLE) from None
        except GitHubIdentityError:
            raise DashboardBridgeError(CODE_GITHUB_UNAVAILABLE) from None

        logger.info("dashboard_session_established", extra={
            "operation": "dashboard_bridge",
            # Ids and outcome only. No token, no login, no repository name.
            "may_govern": established["may_govern"],
        })
        return established
