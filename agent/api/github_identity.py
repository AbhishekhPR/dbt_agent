"""GitHub user-to-server identity, for the dashboard login.

This is the *user* half of the GitHub App. Installation authentication proves
what the App may reach; it says nothing about what the person at the browser
may reach, and using it to answer that question would authorize everyone who
can load the page. So the dashboard asks GitHub, as the user, what that user
may do with the repository.

Transport is stdlib urllib, matching agent/github_app/client.py; nothing here
takes a new dependency.

Nothing in this module logs a code, a token, or the client secret. The values
are carried in locals and returned to a caller that encrypts them.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
API_ROOT = "https://api.github.com"
USER_AGENT = "relium-dashboard"


class GitHubIdentityError(Exception):
    """GitHub refused, or answered in a shape that cannot be trusted."""


class GitHubCredentialExpired(GitHubIdentityError):
    """The stored user credential is no longer usable and cannot be refreshed."""


@dataclass(frozen=True)
class UserCredential:
    """A GitHub user access token, and the refresh material that renews it.

    ``expires_at`` is None when the App issues non-expiring user tokens, which
    is a per-App setting. Both shapes are supported because both are real.
    """

    access_token: str
    expires_at: datetime | None
    refresh_token: str | None
    refresh_expires_at: datetime | None


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Where to send the browser to start authorization."""
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def _post_form(url, fields, *, timeout, opener=None):
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", USER_AGENT)
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise GitHubIdentityError(
            f"GitHub returned HTTP {exc.code} for a token request") from None
    except urllib.error.URLError:
        raise GitHubIdentityError("GitHub was unreachable for a token request") from None
    try:
        document = json.loads(payload or b"{}")
    except json.JSONDecodeError:
        raise GitHubIdentityError("GitHub returned a malformed token response") from None
    if not isinstance(document, dict):
        raise GitHubIdentityError("GitHub returned a malformed token response")
    # OAuth failures arrive as HTTP 200 with an error field. The description is
    # GitHub's own and carries no credential, so it is surfaced.
    if document.get("error"):
        raise GitHubIdentityError(
            f"GitHub refused the authorization: {document.get('error')}")
    return document


def _credential_from(document, *, now) -> UserCredential:
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GitHubIdentityError("GitHub returned no access token")

    def _expiry(seconds_field):
        seconds = document.get(seconds_field)
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        return now + timedelta(seconds=int(seconds))

    refresh_token = document.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = None

    return UserCredential(
        access_token=access_token,
        expires_at=_expiry("expires_in"),
        refresh_token=refresh_token,
        refresh_expires_at=_expiry("refresh_token_expires_in") if refresh_token else None,
    )


def exchange_code(*, client_id, client_secret, code, redirect_uri,
                  timeout=10.0, opener=None, now=None) -> UserCredential:
    """Turn a one-time authorization code into a user credential."""
    now = now or datetime.now(timezone.utc)
    document = _post_form(ACCESS_TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }, timeout=timeout, opener=opener)
    return _credential_from(document, now=now)


def refresh_credential(*, client_id, client_secret, refresh_token,
                       timeout=10.0, opener=None, now=None) -> UserCredential:
    """Renew an expiring user credential.

    GitHub rotates the refresh token on every use, so the value returned here
    replaces the stored one. Reusing the old one afterwards fails, which is why
    the caller must persist the result before relying on it again.
    """
    now = now or datetime.now(timezone.utc)
    document = _post_form(ACCESS_TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=timeout, opener=opener)
    return _credential_from(document, now=now)


def _get(path, access_token, *, timeout, opener=None):
    request = urllib.request.Request(f"{API_ROOT}{path}", method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {access_token}")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # The credential is gone or no longer sufficient. Callers treat
            # this as "re-authenticate", never as "assume the last answer".
            raise GitHubCredentialExpired(
                f"GitHub rejected the user credential with HTTP {exc.code}") from None
        if exc.code == 404:
            return 404, {}
        raise GitHubIdentityError(f"GitHub returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise GitHubIdentityError("GitHub was unreachable") from None


def fetch_viewer(access_token, *, timeout=10.0, opener=None) -> dict:
    """The authenticated user's own identity."""
    status, document = _get("/user", access_token, timeout=timeout, opener=opener)
    login = document.get("login") if isinstance(document, dict) else None
    if status != 200 or not isinstance(login, str) or not login:
        raise GitHubIdentityError("GitHub did not identify the authenticated user")
    return {"login": login, "user_id": document.get("id"),
            "name": document.get("name")}


def fetch_user_installations(access_token, *, timeout=10.0, opener=None,
                             max_pages=10) -> list:
    """Which App installations the AUTHENTICATED USER can access.

    ###################################################################
    # THIS IS WHAT MAKES A SPOOFED installation_id USELESS.           #
    ###################################################################

    GitHub warns that the ``installation_id`` on the Setup URL redirect can be
    forged, so it proves nothing on its own. ``GET /user/installations`` is the
    other half: it is answered AS THE USER, and lists only installations that
    user genuinely has access to. An attacker can name any installation id in a
    URL; they cannot make this endpoint return one they have no access to,
    because the answer is scoped to their own credential.

    So the check is: the id from the redirect must appear in this list. That
    joins a browser-supplied number to a human GitHub has independently
    confirmed is associated with the installation.

    Returns GitHub's installation objects verbatim. Paginated, because a user
    in many organizations can have more than one page, and stopping at the
    first would silently fail verification for exactly those customers.
    """
    installations = []
    for page in range(1, max_pages + 1):
        status, document = _get(
            f"/user/installations?per_page=100&page={page}",
            access_token, timeout=timeout, opener=opener)
        if status != 200 or not isinstance(document, dict):
            raise GitHubIdentityError(
                "GitHub did not return the user's installations")
        batch = document.get("installations")
        if not isinstance(batch, list):
            raise GitHubIdentityError(
                "GitHub returned a malformed installation list")
        installations.extend(item for item in batch if isinstance(item, dict))
        # `total_count` is the whole set, not this page.
        total = document.get("total_count")
        if not isinstance(total, int) or len(installations) >= total or not batch:
            break
    return installations


def user_can_access_installation(access_token, installation_id, *,
                                 timeout=10.0, opener=None) -> bool:
    """Whether this user is associated with this installation.

    A narrow, boolean answer over :func:`fetch_user_installations`, compared on
    the numeric id. Never on an account login: a login can be renamed and the
    old name claimed by somebody else.
    """
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        return False
    for installation in fetch_user_installations(
            access_token, timeout=timeout, opener=opener):
        candidate = installation.get("id")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            if candidate == installation_id:
                return True
    return False


def fetch_repository_permissions(access_token, owner, repository, *,
                                 timeout=10.0, opener=None) -> dict | None:
    """The authenticated user's permissions on one repository.

    Returns GitHub's ``permissions`` object verbatim, or None when the user
    cannot see the repository at all — which GitHub reports as 404 rather than
    403, precisely so that a private repository's existence is not disclosed.
    """
    status, document = _get(
        f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repository)}",
        access_token, timeout=timeout, opener=opener)
    if status == 404:
        return None
    permissions = document.get("permissions") if isinstance(document, dict) else None
    if not isinstance(permissions, dict):
        # Seeing the repository without a permissions object means the answer
        # to "what may this user do" is unknown, and unknown is not read access.
        return None
    return permissions
