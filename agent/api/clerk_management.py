"""Bounded server-side access to Clerk's organization membership authority."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.api.workspace_membership import (
    ClerkMembership,
    ClerkOrganizationSnapshot,
    WorkspaceMembershipUnavailable,
)


CLERK_API_BASE = "https://api.clerk.com/v1"
DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_MEMBERSHIPS = 10_000


class ClerkMembershipUnavailable(WorkspaceMembershipUnavailable):
    """Clerk could not establish one complete authoritative membership view."""


class ClerkResourceAbsent(ClerkMembershipUnavailable):
    """Clerk authoritatively reports that the selected resource is absent."""


@dataclass(frozen=True)
class ClerkUserOrganizationMembership:
    organization_id: str
    clerk_membership_id: str
    clerk_role_key: str
    source_version: str | None = None


@dataclass(frozen=True)
class ClerkManagementSettings:
    secret_key: str = field(repr=False)
    api_base: str = CLERK_API_BASE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self):
        if self.api_base.rstrip("/") != CLERK_API_BASE:
            raise ValueError("Clerk management API must use the official HTTPS origin")
        if self.timeout_seconds < 1 or self.timeout_seconds > 30:
            raise ValueError("Clerk management timeout must be between 1 and 30 seconds")
        if self.max_response_bytes < 1 or self.max_response_bytes > 10 * 1024 * 1024:
            raise ValueError("Clerk response limit is not safe")

    @classmethod
    def from_environ(cls, environ):
        if environ is None:
            import os
            environ = os.environ
        secret = (environ.get("RELIUM_CLERK_SECRET_KEY") or "").strip()
        if not secret:
            return None
        return cls(secret_key=secret)


class ClerkManagementClient:
    def __init__(self, settings: ClerkManagementSettings, *, opener=None,
                 page_size=DEFAULT_PAGE_SIZE,
                 max_memberships=DEFAULT_MAX_MEMBERSHIPS):
        if not settings or not settings.secret_key:
            raise ValueError("Clerk management credentials are required")
        if page_size < 1 or page_size > 100:
            raise ValueError("Clerk membership page size must be between 1 and 100")
        self.settings = settings
        self._opener = opener
        self._page_size = page_size
        self._max_memberships = max_memberships

    def organization_snapshot(self, organization_id: str) -> ClerkOrganizationSnapshot:
        if not isinstance(organization_id, str) or not organization_id.strip():
            raise ClerkMembershipUnavailable("Clerk organization id is missing")
        organization_id = organization_id.strip()
        encoded = urllib.parse.quote(organization_id, safe="")
        organization = self._request_json(f"/organizations/{encoded}")
        if not isinstance(organization, dict) or organization.get("id") != organization_id:
            raise ClerkMembershipUnavailable("Clerk returned a different organization")

        creator = organization.get("created_by")
        if not isinstance(creator, str) or not creator:
            creator = None
        organization_version = _source_version(organization)

        memberships = []
        offset = 0
        expected_total = None
        while expected_total is None or offset < expected_total:
            page = self._request_json(
                f"/organizations/{encoded}/memberships"
                f"?limit={self._page_size}&offset={offset}"
            )
            if not isinstance(page, dict):
                raise ClerkMembershipUnavailable("Clerk returned malformed pagination")
            data = page.get("data")
            total = page.get("total_count")
            if (not isinstance(data, list) or not isinstance(total, int)
                    or isinstance(total, bool) or total < 0):
                raise ClerkMembershipUnavailable("Clerk returned malformed pagination")
            if expected_total is None:
                expected_total = total
                if expected_total > self._max_memberships:
                    raise ClerkMembershipUnavailable("Clerk membership count exceeds limit")
            elif total != expected_total:
                raise ClerkMembershipUnavailable("Clerk membership pagination changed")
            if not data and offset < expected_total:
                raise ClerkMembershipUnavailable("Clerk returned incomplete pagination")

            for document in data:
                memberships.append(_parse_membership(document, organization_id))
            offset += len(data)
            if len(memberships) > expected_total:
                raise ClerkMembershipUnavailable("Clerk returned too many memberships")

        if len(memberships) != expected_total:
            raise ClerkMembershipUnavailable("Clerk returned incomplete pagination")
        return ClerkOrganizationSnapshot(
            organization_id=organization_id,
            created_by_user_id=creator,
            memberships=tuple(memberships),
            source_version=organization_version,
        )

    def user_organization_memberships(
            self, user_id: str) -> tuple[ClerkUserOrganizationMembership, ...]:
        """Return one complete server-authoritative membership inventory."""
        if not isinstance(user_id, str) or not user_id.strip():
            raise ClerkMembershipUnavailable("Clerk user id is missing")
        user_id = user_id.strip()
        encoded = urllib.parse.quote(user_id, safe="")
        memberships = []
        offset = 0
        expected_total = None
        while expected_total is None or offset < expected_total:
            page = self._request_json(
                f"/users/{encoded}/organization_memberships"
                f"?limit={self._page_size}&offset={offset}")
            if not isinstance(page, dict):
                raise ClerkMembershipUnavailable("Clerk returned malformed pagination")
            data, total = page.get("data"), page.get("total_count")
            if (not isinstance(data, list) or not isinstance(total, int)
                    or isinstance(total, bool) or total < 0):
                raise ClerkMembershipUnavailable("Clerk returned malformed pagination")
            if expected_total is None:
                expected_total = total
                if total > self._max_memberships:
                    raise ClerkMembershipUnavailable("Clerk membership count exceeds limit")
            elif total != expected_total:
                raise ClerkMembershipUnavailable("Clerk membership pagination changed")
            if not data and offset < expected_total:
                raise ClerkMembershipUnavailable("Clerk returned incomplete pagination")
            memberships.extend(
                _parse_user_membership(item, user_id) for item in data)
            offset += len(data)
            if len(memberships) > expected_total:
                raise ClerkMembershipUnavailable("Clerk returned too many memberships")
        if len(memberships) != expected_total:
            raise ClerkMembershipUnavailable("Clerk returned incomplete pagination")
        return tuple(memberships)

    def delete_organization_membership(self, organization_id: str,
                                       user_id: str):
        organization = urllib.parse.quote(_identifier(
            organization_id, "Clerk organization id"), safe="")
        user = urllib.parse.quote(_identifier(user_id, "Clerk user id"), safe="")
        return self._request_json(
            f"/organizations/{organization}/memberships/{user}",
            method="DELETE")

    def delete_organization(self, organization_id: str):
        organization = urllib.parse.quote(_identifier(
            organization_id, "Clerk organization id"), safe="")
        return self._request_json(
            f"/organizations/{organization}", method="DELETE")

    def delete_user(self, user_id: str):
        user = urllib.parse.quote(_identifier(user_id, "Clerk user id"), safe="")
        return self._request_json(f"/users/{user}", method="DELETE")

    def _request_json(self, path, *, method="GET"):
        request = urllib.request.Request(self.settings.api_base + path, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Bearer {self.settings.secret_key}")
        request.add_header("User-Agent", "relium-membership/1")
        send = self._opener or _default_opener().open
        try:
            with send(request, timeout=self.settings.timeout_seconds) as response:
                payload = response.read(self.settings.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ClerkResourceAbsent("Clerk resource is absent") from None
            raise ClerkMembershipUnavailable(
                f"Clerk membership authority returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError):
            raise ClerkMembershipUnavailable(
                "Clerk membership authority is unavailable") from None
        if len(payload) > self.settings.max_response_bytes:
            raise ClerkMembershipUnavailable("Clerk response exceeded the size limit")
        if not payload:
            return {}
        try:
            document = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ClerkMembershipUnavailable("Clerk returned malformed JSON") from None
        return document


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = None


def _default_opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            _RefuseRedirects, urllib.request.HTTPSHandler())
    return _OPENER


def _source_version(document):
    value = document.get("updated_at")
    if value is None:
        return None
    return str(value)


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ClerkMembershipUnavailable("Clerk membership timestamp is malformed")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _parse_membership(document, expected_organization_id):
    if not isinstance(document, dict):
        raise ClerkMembershipUnavailable("Clerk membership is malformed")
    membership_id = document.get("id")
    role = document.get("role")
    public_user = document.get("public_user_data")
    user_id = public_user.get("user_id") if isinstance(public_user, dict) else None
    organization = document.get("organization")
    if isinstance(organization, dict):
        returned_organization_id = organization.get("id")
        if returned_organization_id != expected_organization_id:
            raise ClerkMembershipUnavailable(
                "Clerk membership belongs to a different organization")
    if not all(isinstance(value, str) and value for value in
               (membership_id, user_id, role)):
        raise ClerkMembershipUnavailable(
            "Clerk membership is missing authoritative provenance")
    return ClerkMembership(
        clerk_user_id=user_id,
        clerk_membership_id=membership_id,
        clerk_role_key=role,
        source_version=_source_version(document),
        source_updated_at=_timestamp(document.get("updated_at")),
    )


def _parse_user_membership(document, expected_user_id):
    if not isinstance(document, dict):
        raise ClerkMembershipUnavailable("Clerk membership is malformed")
    membership_id = document.get("id")
    role = document.get("role")
    organization = document.get("organization")
    organization_id = (organization.get("id")
                       if isinstance(organization, dict) else None)
    public_user = document.get("public_user_data")
    user_id = public_user.get("user_id") if isinstance(public_user, dict) else None
    if user_id != expected_user_id:
        raise ClerkMembershipUnavailable(
            "Clerk membership belongs to a different user")
    if not all(isinstance(value, str) and value for value in
               (membership_id, role, organization_id)):
        raise ClerkMembershipUnavailable(
            "Clerk membership is missing authoritative provenance")
    return ClerkUserOrganizationMembership(
        organization_id=organization_id,
        clerk_membership_id=membership_id,
        clerk_role_key=role,
        source_version=_source_version(document),
    )


def _identifier(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ClerkMembershipUnavailable(f"{label} is missing")
    return value.strip()
