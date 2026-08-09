"""Operator-side provisioning of collector service tokens.

This runs on Relium's side, not the customer's. Authorization is possession of
RELIUM_DATABASE_URL: whoever can reach the evidence store directly is already
an operator, so no second authorization scheme is invented here.

It exists because a design partner needs a token and there was no supported way
to produce one. It is deliberately three operations - issue, list, revoke - and
not an identity system.

The secret is generated, shown once, and never stored. Only sha256(secret) is
persisted, exactly as ServiceTokenAuthenticator already expects, so a token
issued here is verified by the same code path as any other.
"""
from __future__ import annotations

from agent.api.auth import generate_token, hash_secret


class ProvisioningError(RuntimeError):
    """The request cannot be satisfied. Never contains a secret."""


def issue_collector_token(store, *, organization_id, repository_id, environment,
                          description=None, expires_at=None):
    """Create a tenant-scoped collector token.

    Returns (token_id, presented_secret). The caller must show the secret once
    and then forget it: it is not recoverable from the store afterwards, by
    anyone, including Relium.
    """
    if not organization_id or not repository_id:
        raise ProvisioningError("organization and repository are both required")
    if not environment:
        # An unscoped token would let a collector submit evidence for any
        # environment of that repository. A collector reads one warehouse.
        raise ProvisioningError(
            "environment is required: a collector token must be scoped to the "
            "one environment its warehouse represents")

    # Fail before issuing rather than minting a token for a tenant that does
    # not exist, which would authenticate but resolve to nothing.
    store.ensure_tenant(organization_id, repository_id, environment)

    token_id, secret, presented = generate_token()
    store.create_service_token(
        token_id, hash_secret(secret), organization_id, repository_id,
        environment=environment,
        description=description or f"collector token for {environment}",
        expires_at=expires_at)
    return token_id, presented


def list_collector_tokens(store, *, organization_id=None, repository_id=None):
    """Tokens an operator can identify and revoke. Secrets are not returned."""
    return store.list_service_tokens(organization_id=organization_id,
                                     repository_id=repository_id)


def revoke_collector_token(store, token_id):
    """Revoke by token_id. True when this call did it, False if already gone."""
    if not token_id:
        raise ProvisioningError("token_id is required")
    return store.revoke_service_token(token_id)


def token_id_of(presented):
    """The non-secret half of a presented token, for identifying it later.

    Lets an operator answer "which row is this token?" from the value a
    customer still holds, without that value ever being compared to anything
    stored.
    """
    from agent.api.auth import split_token

    parts = split_token(presented or "")
    if parts is None:
        raise ProvisioningError("not a valid Relium token")
    return parts[0]
