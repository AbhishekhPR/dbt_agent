"""Service-token authentication and tenant-scope resolution for the public API.

A token is presented as ``rlm_<token_id>.<secret>``. Only ``sha256(secret)`` is
persisted; the secret itself is never stored, logged or returned after issuance.
Verification looks the row up by its non-secret ``token_id`` and then compares
digests with :func:`hmac.compare_digest`, so the comparison is constant time.

Tenant scope is resolved *from the token*, never from caller-supplied body or
query fields.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

TOKEN_PREFIX = "rlm_"


class AuthenticationError(Exception):
    """Credentials are absent or invalid."""


class AuthorizationError(Exception):
    """Credentials are valid but do not grant access to the requested scope."""


@dataclass(frozen=True)
class TenantScope:
    """The authorization boundary a request is confined to."""

    organization_id: str
    repository_id: str
    environment: str | None = None
    token_id: str = ""

    def permits_environment(self, environment: str | None) -> bool:
        if self.environment is None:
            return True
        return environment is None or environment == self.environment

    def require_environment(self, environment: str | None) -> str:
        """Resolve the effective environment for a request.

        A token scoped to one environment pins requests to it. An unscoped
        token requires the caller to name an environment explicitly.
        """
        if self.environment is not None:
            if environment is not None and environment != self.environment:
                raise AuthorizationError("environment outside token scope")
            return self.environment
        if not environment:
            raise AuthorizationError("environment is required for this token")
        return environment


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Return ``(token_id, secret, presented_token)`` for a newly issued token."""
    token_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    return token_id, secret, f"{TOKEN_PREFIX}{token_id}.{secret}"


def split_token(presented: str) -> tuple[str, str] | None:
    if not isinstance(presented, str) or not presented.startswith(TOKEN_PREFIX):
        return None
    remainder = presented[len(TOKEN_PREFIX):]
    token_id, separator, secret = remainder.partition(".")
    if not separator or not token_id or not secret:
        return None
    if not token_id.isalnum():
        return None
    return token_id, secret


def bearer_token(authorization_header: str | None) -> str | None:
    if not isinstance(authorization_header, str):
        return None
    scheme, separator, value = authorization_header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


class ServiceTokenAuthenticator:
    """Verifies presented tokens against their stored hashes."""

    def __init__(self, store):
        self._store = store

    def authenticate(self, presented: str | None, *, now: datetime | None = None) -> TenantScope:
        now = now or datetime.now(timezone.utc)
        parts = split_token(presented or "")
        if parts is None:
            raise AuthenticationError("malformed or absent credentials")
        token_id, secret = parts

        record = self._store.get_service_token(token_id)
        presented_hash = hash_secret(secret)
        if record is None:
            # Compare against a dummy digest so a missing token and a wrong
            # secret take the same comparison path.
            hmac.compare_digest(presented_hash, hash_secret("absent"))
            raise AuthenticationError("invalid credentials")

        if not hmac.compare_digest(record["secret_hash"], presented_hash):
            raise AuthenticationError("invalid credentials")

        if record.get("revoked_at") is not None:
            raise AuthenticationError("credentials revoked")
        expires_at = record.get("expires_at")
        if expires_at is not None and expires_at <= now:
            raise AuthenticationError("credentials expired")

        return TenantScope(
            organization_id=record["organization_id"],
            repository_id=record["repository_id"],
            environment=record.get("environment"),
            token_id=token_id,
        )
