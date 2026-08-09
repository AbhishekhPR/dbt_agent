"""Dashboard sessions: the human half of the authorization boundary.

The lifecycle, stated once so it can be checked against the code:

  1. Sign-in mints an opaque session id. The browser gets it in a cookie; the
     database gets only ``sha256(session_id)``.
  2. The GitHub user credential obtained during sign-in is encrypted with
     AES-256-GCM and stored on the session row. It never reaches the browser.
  3. Every governance write, and every read on a session whose permission is
     older than ``PERMISSION_TTL``, re-verifies repository permission with
     GitHub *as the user*.
  4. If the access token has expired and a refresh token exists, it is
     refreshed and the rotated pair replaces the stored one.
  5. If it cannot be refreshed — no refresh token, refresh expired, or GitHub
     rejects it — the session is revoked and the operation fails. Stale
     authorization is never reused.
  6. Logout revokes the session and clears the stored credentials.

Point 5 is the one that matters. An earlier draft of this design said the
GitHub token was discarded after sign-in *and* that permission was
re-verified on every write. Both cannot be true: without a user credential
the only thing left to ask GitHub with is the App installation, which
describes what the App may reach, not what the person may reach. Keeping the
credential server-side, encrypted, is what makes re-verification mean
anything.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.api import github_identity as gh_identity
from agent.api.authorization import highest_permission, may_govern, may_read
from agent.api.session_crypto import CredentialEncryptionError, decrypt, encrypt

SESSION_COOKIE = "relium_session"
CSRF_COOKIE = "relium_csrf"
CSRF_HEADER = "X-Relium-CSRF"
OAUTH_NONCE_COOKIE = "relium_oauth"

SESSION_LIFETIME = timedelta(hours=12)
OAUTH_STATE_LIFETIME = timedelta(minutes=10)
#: How long a verified repository permission may be reused for reads. Writes
#: always re-verify regardless of this.
PERMISSION_TTL = timedelta(minutes=5)


class SessionError(Exception):
    """The session is absent, expired, revoked, or no longer verifiable."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class HumanPrincipal:
    """An authenticated GitHub user, scoped to one repository."""

    organization_id: str
    repository_id: str
    environment: str | None
    github_login: str
    github_permission: str
    may_govern: bool
    session_id_hash: str

    is_human = True
    scope = "human"

    @property
    def actor(self) -> str:
        """The identity recorded against anything this principal does."""
        return f"github:{self.github_login}"

    def permits_environment(self, environment):
        if self.environment is None:
            return True
        return environment is None or environment == self.environment

    def require_environment(self, environment):
        from agent.api.auth import AuthorizationError

        if self.environment is not None:
            if environment is not None and environment != self.environment:
                raise AuthorizationError("environment outside session scope")
            return self.environment
        if not environment:
            raise AuthorizationError("environment is required for this session")
        return environment


class SessionManager:
    """Creates, authenticates and revokes dashboard sessions."""

    def __init__(self, *, client_id, client_secret, encryption_key,
                 identity=gh_identity, clock=None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._key = encryption_key
        self._identity = identity
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- creation ----------------------------------------------------------

    def begin_authorization(self, store, *, redirect_to, redirect_uri):
        """Start one authorization attempt.

        Returns the URL to send the browser to and the nonce that must come
        back in a cookie. The state is single-use and useless without the
        nonce, so a state intercepted from a redirect cannot be completed in
        another browser.
        """
        now = self._clock()
        state = new_secret()
        nonce = new_secret()
        store.create_oauth_state(
            digest(state), nonce_hash=digest(nonce), redirect_to=redirect_to,
            expires_at=now + OAUTH_STATE_LIFETIME)
        url = self._identity.authorize_url(self._client_id, redirect_uri, state)
        return url, nonce

    def complete_authorization(self, store, *, code, state, nonce, redirect_uri,
                               organization_id, repository_id, environment):
        """Finish authorization and mint a session, or refuse.

        Refusal is deliberately uniform: a replayed state, an expired state, a
        mismatched nonce and an unknown state all fail the same way.
        """
        now = self._clock()
        if not code or not state or not nonce:
            raise SessionError("incomplete authorization callback")

        claimed = store.consume_oauth_state(digest(state), now=now)
        if claimed is None:
            raise SessionError("authorization state is unknown, expired or already used")
        if not hmac.compare_digest(claimed["nonce_hash"], digest(nonce)):
            raise SessionError("authorization state does not belong to this browser")

        credential = self._identity.exchange_code(
            client_id=self._client_id, client_secret=self._client_secret,
            code=code, redirect_uri=redirect_uri, now=now)

        viewer = self._identity.fetch_viewer(credential.access_token)
        permissions = self._identity.fetch_repository_permissions(
            credential.access_token, organization_id, repository_id)

        if not may_read(permissions):
            raise SessionError(
                f"{viewer['login']} does not have access to "
                f"{organization_id}/{repository_id}")

        session_id = new_secret()
        session_hash = digest(session_id)
        csrf_token = new_secret()
        store.ensure_tenant(organization_id, repository_id, environment)
        store.create_dashboard_session(
            session_hash,
            organization_id=organization_id,
            repository_id=repository_id,
            environment=environment,
            github_login=viewer["login"],
            github_user_id=viewer.get("user_id"),
            github_permission=highest_permission(permissions),
            may_govern=may_govern(permissions),
            permission_checked_at=now,
            csrf_token=csrf_token,
            expires_at=now + SESSION_LIFETIME,
            github_access_token=encrypt(
                self._key, credential.access_token, associated=session_hash),
            github_access_expires_at=credential.expires_at,
            github_refresh_token=encrypt(
                self._key, credential.refresh_token, associated=session_hash),
            github_refresh_expires_at=credential.refresh_expires_at,
        )
        return {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "redirect_to": claimed.get("redirect_to"),
            "login": viewer["login"],
        }

    # -- authentication ----------------------------------------------------

    def authenticate(self, store, session_id, *, require_fresh_permission=False):
        """Resolve a cookie into a principal, or raise.

        ``require_fresh_permission`` is set for every governance write. It
        forces a live check with GitHub rather than trusting the permission
        recorded at sign-in.
        """
        if not session_id:
            raise SessionError("no session")
        session_hash = digest(session_id)
        record = store.get_dashboard_session(session_hash)
        if record is None:
            raise SessionError("unknown session")
        if record.get("revoked_at") is not None:
            raise SessionError("session revoked")

        now = self._clock()
        if record["expires_at"] <= now:
            raise SessionError("session expired")

        stale = (now - record["permission_checked_at"]) > PERMISSION_TTL
        if require_fresh_permission or stale:
            record = self._reverify(store, record, session_hash, now)

        store.touch_dashboard_session(session_hash)
        return HumanPrincipal(
            organization_id=record["organization_id"],
            repository_id=record["repository_id"],
            environment=record.get("environment"),
            github_login=record["github_login"],
            github_permission=record["github_permission"],
            may_govern=bool(record["may_govern"]),
            session_id_hash=session_hash,
        )

    def _reverify(self, store, record, session_hash, now):
        """Ask GitHub, as the user, what they may do — or end the session."""
        try:
            access_token = self._usable_access_token(store, record, session_hash, now)
            permissions = self._identity.fetch_repository_permissions(
                access_token, record["organization_id"], record["repository_id"])
        except (gh_identity.GitHubIdentityError, CredentialEncryptionError):
            store.revoke_dashboard_session(session_hash, "github_credential_unusable")
            raise SessionError(
                "GitHub access could not be re-verified; sign in again") from None

        if not may_read(permissions):
            store.revoke_dashboard_session(session_hash, "repository_access_lost")
            raise SessionError("repository access has been removed")

        updated = {
            "github_permission": highest_permission(permissions),
            "may_govern": may_govern(permissions),
        }
        store.update_dashboard_session_permission(
            session_hash, permission_checked_at=now, **updated)
        return {**record, **updated, "permission_checked_at": now}

    def _usable_access_token(self, store, record, session_hash, now):
        """Decrypt the stored access token, refreshing it first if it has expired."""
        expires_at = record.get("github_access_expires_at")
        if expires_at is None or expires_at > now + timedelta(seconds=30):
            token = decrypt(self._key, record.get("github_access_token"),
                            associated=session_hash)
            if token:
                return token

        refresh_token = decrypt(self._key, record.get("github_refresh_token"),
                                associated=session_hash)
        refresh_expiry = record.get("github_refresh_expires_at")
        if not refresh_token or (refresh_expiry is not None and refresh_expiry <= now):
            raise gh_identity.GitHubCredentialExpired(
                "the GitHub credential has expired and cannot be refreshed")

        renewed = self._identity.refresh_credential(
            client_id=self._client_id, client_secret=self._client_secret,
            refresh_token=refresh_token, now=now)
        # GitHub rotates the refresh token on use, so the new pair must be
        # persisted before it is relied on again.
        store.update_dashboard_session_credential(
            session_hash,
            github_access_token=encrypt(self._key, renewed.access_token,
                                        associated=session_hash),
            github_access_expires_at=renewed.expires_at,
            github_refresh_token=encrypt(self._key, renewed.refresh_token,
                                         associated=session_hash),
            github_refresh_expires_at=renewed.refresh_expires_at,
        )
        return renewed.access_token

    # -- csrf and revocation ----------------------------------------------

    def verify_csrf(self, store, session_id, presented):
        """Double-submit check, bound to the session and compared in constant time."""
        record = store.get_dashboard_session(digest(session_id or ""))
        if record is None or record.get("revoked_at") is not None:
            return False
        if not isinstance(presented, str) or not presented:
            return False
        return hmac.compare_digest(record["csrf_token"], presented)

    def revoke(self, store, session_id, reason="logout"):
        if not session_id:
            return False
        return store.revoke_dashboard_session(digest(session_id), reason)
