"""Authoritative Clerk membership projection and workspace authorization.

Clerk is the membership source.  Values in onboarding, GitHub, billing, or a
request body never enter this module's role decisions.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


OWNER_ROLE_KEYS = frozenset({"owner", "org:owner"})
ADMIN_ROLE_KEYS = frozenset({"admin", "org:admin"})


@dataclass(frozen=True)
class ClerkMembership:
    clerk_user_id: str
    clerk_membership_id: str
    clerk_role_key: str
    source_version: str | None = None
    source_updated_at: datetime | None = None


@dataclass(frozen=True)
class ClerkOrganizationSnapshot:
    organization_id: str
    created_by_user_id: str | None
    memberships: tuple[ClerkMembership, ...]
    source_version: str | None = None


@dataclass(frozen=True)
class ProjectedMembership:
    clerk_user_id: str
    clerk_membership_id: str
    role: str
    clerk_role_key: str
    role_basis: str
    source_version: str | None
    source_updated_at: datetime | None


@dataclass(frozen=True)
class MembershipProjection:
    organization_id: str
    memberships: tuple[ProjectedMembership, ...]
    ownership_status: str
    active_owner_count: int
    source_version: str | None
    source_fingerprint: str


class WorkspaceMembershipUnavailable(Exception):
    """A current, complete Clerk membership verdict cannot be established."""


class WorkspaceRoleDenied(Exception):
    """The current verified member lacks the required workspace role."""


@dataclass(frozen=True)
class WorkspaceAuthorizationContext:
    tenant_id: str
    clerk_user_id: str
    role: str
    ownership_status: str
    active_owner_count: int
    sync_generation: str
    clerk_membership_id: str | None = None


def project_memberships(snapshot: ClerkOrganizationSnapshot) -> MembershipProjection:
    """Normalize one complete Clerk organization snapshot.

    The organization creator is an owner fallback only when Clerk reports no
    explicit owner role and the creator remains a current administrator. If
    neither source establishes an owner, the result remains ambiguous rather
    than promoting an arbitrary administrator or downgraded creator.
    """
    if not isinstance(snapshot.organization_id, str) or not snapshot.organization_id:
        raise ValueError("Clerk organization snapshot has no organization id")

    users = set()
    identifiers = set()
    for membership in snapshot.memberships:
        if not membership.clerk_user_id or not membership.clerk_membership_id:
            raise ValueError("Clerk membership is missing an immutable identifier")
        if membership.clerk_user_id in users:
            raise ValueError("Clerk returned duplicate organization members")
        if membership.clerk_membership_id in identifiers:
            raise ValueError("Clerk returned a duplicate membership identifier")
        users.add(membership.clerk_user_id)
        identifiers.add(membership.clerk_membership_id)

    has_explicit_owner = any(
        membership.clerk_role_key in OWNER_ROLE_KEYS
        for membership in snapshot.memberships
    )
    creator_is_current_admin = bool(
        snapshot.created_by_user_id
        and any(
            membership.clerk_user_id == snapshot.created_by_user_id
            and membership.clerk_role_key in ADMIN_ROLE_KEYS
            for membership in snapshot.memberships
        )
    )

    projected = []
    for membership in snapshot.memberships:
        if membership.clerk_role_key in OWNER_ROLE_KEYS:
            role, basis = "owner", "explicit_clerk_role"
        elif (not has_explicit_owner and creator_is_current_admin
              and membership.clerk_user_id == snapshot.created_by_user_id):
            role, basis = "owner", "organization_creator"
        elif membership.clerk_role_key in ADMIN_ROLE_KEYS:
            role, basis = "admin", "clerk_membership"
        else:
            role, basis = "member", "clerk_membership"
        projected.append(ProjectedMembership(
            clerk_user_id=membership.clerk_user_id,
            clerk_membership_id=membership.clerk_membership_id,
            role=role,
            clerk_role_key=membership.clerk_role_key,
            role_basis=basis,
            source_version=membership.source_version,
            source_updated_at=membership.source_updated_at,
        ))

    projected.sort(key=lambda row: (row.clerk_user_id, row.clerk_membership_id))
    owner_count = sum(row.role == "owner" for row in projected)
    fingerprint_document = {
        "organization_id": snapshot.organization_id,
        "created_by_user_id": snapshot.created_by_user_id,
        "source_version": snapshot.source_version,
        "memberships": [
            {
                "id": row.clerk_membership_id,
                "user_id": row.clerk_user_id,
                "role_key": row.clerk_role_key,
                "source_version": row.source_version,
                "source_updated_at": (
                    row.source_updated_at.isoformat()
                    if row.source_updated_at is not None else None
                ),
            }
            for row in projected
        ],
    }
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_document, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return MembershipProjection(
        organization_id=snapshot.organization_id,
        memberships=tuple(projected),
        ownership_status="authoritative" if owner_count else "ambiguous",
        active_owner_count=owner_count,
        source_version=snapshot.source_version,
        source_fingerprint=fingerprint,
    )


class WorkspaceMembershipAuthorizer:
    """Refresh Clerk and authorize only against the persisted projection."""

    def __init__(self, *, store, source, clock=None, generation_factory=None):
        self._store = store
        self._source = source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._generation_factory = generation_factory or (
            lambda: f"wms_{uuid.uuid4().hex}")

    def current_workspace_role(self, principal) -> str:
        return self._context(principal, require_authoritative_ownership=False).role

    def authorization_context(self, principal) -> WorkspaceAuthorizationContext:
        """Return the refreshed authoritative context for sensitive flows."""
        return self._context(principal, require_authoritative_ownership=True)

    def require_owner(self, principal) -> WorkspaceAuthorizationContext:
        context = self._context(principal, require_authoritative_ownership=True)
        if context.role != "owner":
            raise WorkspaceRoleDenied("workspace owner role is required")
        return context

    def require_admin_or_owner(self, principal) -> WorkspaceAuthorizationContext:
        context = self._context(principal, require_authoritative_ownership=True)
        if context.role not in {"owner", "admin"}:
            raise WorkspaceRoleDenied("workspace administrator role is required")
        return context

    def active_owner_count(self, principal) -> int:
        return self._context(
            principal, require_authoritative_ownership=True,
        ).active_owner_count

    def is_sole_owner(self, principal) -> bool:
        context = self._context(principal, require_authoritative_ownership=True)
        return context.role == "owner" and context.active_owner_count == 1

    def _context(self, principal, *, require_authoritative_ownership):
        if (principal is None
                or getattr(principal, "identity_provider", None) != "clerk"
                or not getattr(principal, "tenant_id", None)
                or not getattr(principal, "clerk_user_id", None)
                or not getattr(principal, "clerk_organization_id", None)):
            raise WorkspaceMembershipUnavailable(
                "authenticated Clerk workspace context is required")

        generation = self._generation_factory()
        synchronized_at = self._clock()
        try:
            self._store.begin_tenant_membership_sync(
                tenant_id=principal.tenant_id,
                clerk_organization_id=principal.clerk_organization_id,
                sync_generation=generation,
            )
            first_snapshot = self._source.organization_snapshot(
                principal.clerk_organization_id)
            second_snapshot = self._source.organization_snapshot(
                principal.clerk_organization_id)
            if (first_snapshot.organization_id != principal.clerk_organization_id
                    or second_snapshot.organization_id
                    != principal.clerk_organization_id):
                raise ValueError("organization mismatch")
            first_projection = project_memberships(first_snapshot)
            projection = project_memberships(second_snapshot)
            if first_projection.source_fingerprint != projection.source_fingerprint:
                raise WorkspaceMembershipUnavailable(
                    "Clerk membership changed during synchronization")
            self._store.replace_tenant_membership_projection(
                tenant_id=principal.tenant_id,
                projection=projection,
                sync_generation=generation,
                synchronized_at=synchronized_at,
            )
            row = self._store.tenant_membership_authorization(
                tenant_id=principal.tenant_id,
                clerk_user_id=principal.clerk_user_id,
                sync_generation=generation,
            )
        except Exception as exc:
            category = (
                "authority_unavailable"
                if isinstance(exc, WorkspaceMembershipUnavailable)
                else "projection_failed"
            )
            try:
                self._store.record_tenant_membership_sync_failure(
                    tenant_id=principal.tenant_id,
                    sync_generation=generation,
                    attempted_at=synchronized_at,
                    failure_category=category,
                )
            except Exception:
                # The authorization verdict remains fail-closed even if the
                # diagnostic status cannot be persisted.
                pass
            if isinstance(exc, WorkspaceMembershipUnavailable):
                raise
            raise WorkspaceMembershipUnavailable(
                "current Clerk membership could not be established") from exc

        if (not row or row.get("tenant_id") != principal.tenant_id
                or row.get("clerk_user_id") != principal.clerk_user_id
                or row.get("sync_generation") != generation
                or row.get("status") != "active"
                or row.get("role") not in {"owner", "admin", "member"}):
            raise WorkspaceMembershipUnavailable(
                "current user is not an active workspace member")
        if (require_authoritative_ownership
                and row.get("ownership_status") != "authoritative"):
            raise WorkspaceMembershipUnavailable(
                "workspace ownership has not been reconciled")
        owner_count = row.get("active_owner_count")
        if (not isinstance(owner_count, int) or isinstance(owner_count, bool)
                or owner_count < 0):
            raise WorkspaceMembershipUnavailable(
                "workspace owner count is not authoritative")
        return WorkspaceAuthorizationContext(
            tenant_id=row["tenant_id"],
            clerk_user_id=row["clerk_user_id"],
            role=row["role"],
            ownership_status=row["ownership_status"],
            active_owner_count=owner_count,
            sync_generation=generation,
            clerk_membership_id=row.get("clerk_membership_id"),
        )
