"""Binding a Relium tenant to a GitHub App installation.

###################################################################
# A BROWSER-SUPPLIED installation_id PROVES NOTHING.              #
###################################################################

GitHub redirects the installer's browser to the App's Setup URL carrying
``?installation_id=...``, and GitHub's own documentation warns that this query
parameter can be spoofed. Anyone can type a number into a URL. If Relium bound
on that number, an attacker would attach someone else's installation — and
therefore someone else's repositories — to their own workspace.

So a binding requires three facts, from three different sources, none of them
the browser:

  1. WHO STARTED THE FLOW
     A single-use, expiring, opaque state minted before the redirect and
     bound server-side to a tenant and a Clerk user. The tenant is read back
     from the stored row, never from the request. See
     ``store.consume_github_installation_state``.

  2. WHAT THE INSTALLATION ACTUALLY IS
     ``GET /app/installations/{id}`` with the App's own JWT. GitHub answers
     only for installations of OUR App, so an id belonging to another App or
     to nothing at all is refused rather than believed. The account id, login
     and type come from this response.

  3. THAT THE HUMAN IS REALLY ASSOCIATED WITH IT
     ``GET /user/installations`` with that person's own GitHub credential.
     An attacker can name any installation; they cannot make GitHub list one
     they have no access to under their own token.

Fact 3 is the one that turns the other two into a binding. Without it, facts 1
and 2 together still permit an attack: a legitimate user starts a real flow,
then substitutes a victim's installation id into the redirect. The state is
valid, the installation is real and is ours — and the binding would be wrong.

WHEN FACT 3 CANNOT BE ESTABLISHED, NOTHING IS BOUND.
The flow stops and reports what is missing. It is never completed by falling
back to the browser's number.

THE THREE PRINCIPALS, KEPT SEPARATE
-----------------------------------
  Clerk session          the Relium login identity. Says who is asking.
  GitHub user credential repository/user authority, proved as that human.
  GitHub App installation Relium's machine access to repositories.

None substitutes for another. In particular the App's installation credential
cannot answer "is this person allowed to connect this installation" — it
describes what the App may reach, not what the person may reach.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: How long an installation-flow state stays claimable. Matches
#: OAUTH_STATE_LIFETIME in agent/api/sessions.py: the same kind of value, used
#: over the same kind of round trip, so the same bound applies.
INSTALLATION_STATE_LIFETIME = timedelta(minutes=10)

#: 32 bytes from `secrets`, URL-safe. Same generator and the same width as the
#: OAuth state and session id already minted in sessions.py.
STATE_BYTES = 32

#: Cached App metadata lifetime. The slug changes essentially never; this
#: exists so a per-request fetch does not become a dependency of showing a
#: button.
APP_METADATA_TTL = timedelta(hours=6)


class InstallationBindingError(Exception):
    """The installation could not be bound. Carries a machine-readable code.

    The ``code`` is stable and safe to show a frontend; ``detail`` is for an
    operator. Neither ever contains the presented state value, a token, or
    which specific check failed in a way that would help forge the next
    attempt.
    """

    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


#: Stable codes. The frontend branches on these; prose is never matched.
CODE_STATE_INVALID = "installation_state_invalid"
CODE_INSTALLATION_UNKNOWN = "installation_unknown"
CODE_GITHUB_IDENTITY_REQUIRED = "github_identity_required"
CODE_GITHUB_IDENTITY_UNUSABLE = "github_identity_unusable"
CODE_INSTALLATION_NOT_AUTHORIZED = "installation_not_authorized"
CODE_INSTALLATION_CLAIMED = "installation_already_connected"
CODE_GITHUB_UNAVAILABLE = "github_unavailable"
CODE_APP_NOT_CONFIGURED = "github_app_not_configured"


def hash_state(value: str) -> str:
    """SHA-256 of the state, hex. Only this reaches the database."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_state() -> str:
    return secrets.token_urlsafe(STATE_BYTES)


@dataclass(frozen=True)
class InstallationBinding:
    """The outcome of a successful binding."""

    installation_id: int
    tenant_id: str
    account_login: str
    account_type: str
    account_id: int
    created: bool


class GitHubAppIdentity:
    """The App's own identity, read from GitHub and cached.

    ###############################################################
    # THE SLUG IS NEVER TAKEN FROM A REQUEST OR HARD-CODED.       #
    ###############################################################

    ``GET /app`` answers for whichever App the JWT belongs to, so the slug is
    necessarily the App this backend authenticates as. That rules out three
    failure modes at once: a stale value in configuration, a frontend
    supplying its own slug, and anybody accidentally shipping the ``relium-e2e``
    test App to a customer.

    The App private key is used only to sign the JWT, inside the credential
    factory. It is never returned, logged, or exposed.
    """

    def __init__(self, client, jwt_factory, *, clock=None, ttl=APP_METADATA_TTL):
        self._client = client
        self._jwt_factory = jwt_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl
        self._cached = None
        self._cached_at = None

    def slug(self):
        return self.metadata().get("slug")

    def app_id(self):
        return self.metadata().get("id")

    def metadata(self):
        now = self._clock()
        if self._cached is not None and self._cached_at is not None:
            if (now - self._cached_at) < self._ttl:
                return self._cached
        document = self._client.get_app(self._jwt_factory())
        if not isinstance(document, dict) or not document.get("slug"):
            raise InstallationBindingError(
                CODE_APP_NOT_CONFIGURED,
                "GitHub did not return a usable App identity")
        self._cached = document
        self._cached_at = now
        return document

    def installation_url(self, state):
        """Where the customer goes to install, with the flow state attached.

        GitHub supports a ``state`` parameter on the installation URL precisely
        so an installation can be correlated with the flow that started it, and
        returns it on the Setup redirect.
        """
        import urllib.parse

        slug = self.slug()
        if not slug:
            raise InstallationBindingError(CODE_APP_NOT_CONFIGURED,
                                           "the GitHub App has no slug")
        query = urllib.parse.urlencode({"state": state})
        return (f"https://github.com/apps/{urllib.parse.quote(slug)}"
                f"/installations/new?{query}")


class InstallationBinder:
    """Performs the three verifications and writes the binding.

    ``github_identity`` is the module used to talk to GitHub as a user; it is
    injected so tests can script GitHub without a network.
    """

    def __init__(self, *, app_identity, client, jwt_factory, session_key,
                 github_identity=None, clock=None):
        from agent.api import github_identity as default_identity

        self._app = app_identity
        self._client = client
        self._jwt_factory = jwt_factory
        self._session_key = session_key
        self._identity = github_identity or default_identity
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- starting the flow -------------------------------------------------

    def start(self, store, *, tenant_id, clerk_user_id):
        """Mint a state and return the URL to send the customer to.

        The returned value is the only thing the browser gets. The tenant id is
        NOT in it — it stays in the database row, so the state cannot be
        decoded, edited or re-pointed at another workspace.
        """
        state = new_state()
        now = self._clock()
        store.create_github_installation_state(
            state_hash=hash_state(state),
            tenant_id=tenant_id,
            clerk_user_id=clerk_user_id,
            # Both timestamps from this clock, so the stored lifetime is
            # exactly INSTALLATION_STATE_LIFETIME regardless of database skew.
            created_at=now,
            expires_at=now + INSTALLATION_STATE_LIFETIME,
        )
        return {
            "install_url": self._app.installation_url(state),
            "expires_at": now + INSTALLATION_STATE_LIFETIME,
        }

    # -- completing the flow -----------------------------------------------

    def complete(self, store, *, presented_state, installation_id,
                 clerk_user_id=None):
        """Verify everything, then bind. Raises InstallationBindingError.

        ``installation_id`` is attacker-controlled and is treated as a claim to
        be checked, never as a fact. ``clerk_user_id`` is the Clerk session at
        the browser, when there is one; it must agree with the session that
        minted the state.
        """
        # ---- 1. the state ------------------------------------------------
        if not isinstance(presented_state, str) or not presented_state:
            raise InstallationBindingError(CODE_STATE_INVALID)

        claimed = store.consume_github_installation_state(
            hash_state(presented_state), now=self._clock())
        if claimed is None:
            # Unknown, expired, already consumed and tampered all fail
            # identically. Distinguishing them would tell an attacker which
            # part of a guess was right.
            raise InstallationBindingError(CODE_STATE_INVALID)

        tenant_id = claimed["tenant_id"]
        state_owner = claimed["clerk_user_id"]

        # The state belongs to the person who minted it. If a Clerk session is
        # present it must be the same one — otherwise a state intercepted from
        # a URL could be completed by whoever else is signed in on that
        # machine, binding the installation to the wrong tenant.
        if clerk_user_id is not None and clerk_user_id != state_owner:
            raise InstallationBindingError(CODE_STATE_INVALID)

        if not isinstance(installation_id, int) or isinstance(installation_id, bool):
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)
        if installation_id <= 0:
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)

        # ---- 2. what the installation actually is ------------------------
        facts = self._installation_facts(installation_id)

        # ---- 3. that the human is associated with it ---------------------
        verified_github_user_id = self._verify_human(
            store, clerk_user_id=state_owner, installation_id=installation_id)

        # ---- persist -----------------------------------------------------
        created = self._persist_binding(
            store, installation_id=installation_id, facts=facts,
            tenant_id=tenant_id, clerk_user_id=state_owner,
            verified_github_user_id=verified_github_user_id,
            bound_via_state_id=claimed["installation_state_id"])

        logger.info("github_installation_bound", extra={
            "operation": "install_setup",
            # Ids only. No token, no state, no account name.
            "installation_id": installation_id,
        })
        return InstallationBinding(
            installation_id=installation_id,
            tenant_id=tenant_id,
            account_login=facts["account_login"],
            account_type=facts["account_type"],
            account_id=facts["account_id"],
            created=created,
        )

    def reconcile(self, store, *, tenant_id, clerk_user_id,
                  selected_installation_id=None):
        """Discover and bind an installation that predates onboarding.

        The optional id is a browser preference, never evidence.  A candidate
        is eligible only after it appears in a fresh user-scoped installation
        list and the App, authenticated with its own JWT, independently returns
        the same installation.  All candidates are verified before any write so
        ambiguity and verification failures leave tenant bindings untouched.
        """
        access_token, github_user_id = self.reconciliation_identity(
            store, clerk_user_id=clerk_user_id)
        discovery = self.discover_reconciliation(
            access_token,
            selected_installation_id=selected_installation_id)
        return self.complete_reconciliation(
            store, discovery=discovery, tenant_id=tenant_id,
            clerk_user_id=clerk_user_id, github_user_id=github_user_id)

    def reconciliation_identity(self, store, *, clerk_user_id):
        """Load the linked-user credential before releasing the store."""
        return self._linked_human(store, clerk_user_id=clerk_user_id)

    def discover_reconciliation(self, access_token, *,
                                selected_installation_id=None):
        """Fetch and App-verify candidates without holding a store connection.

        The returned evidence is process-local and must be passed directly to
        :meth:`complete_reconciliation`; it is never an API payload.
        """
        from agent.api.github_identity import (
            GitHubCredentialExpired, GitHubIdentityError,
        )

        if selected_installation_id is not None:
            if (isinstance(selected_installation_id, bool)
                    or not isinstance(selected_installation_id, int)
                    or selected_installation_id <= 0):
                raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)

        try:
            documents = self._identity.fetch_user_installations(access_token)
        except GitHubCredentialExpired:
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_UNUSABLE) from None
        except GitHubIdentityError:
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE) from None

        if not isinstance(documents, list):
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE)

        installation_ids = []
        seen = set()
        for document in documents:
            if not isinstance(document, dict):
                raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE)
            installation_id = document.get("id")
            if (isinstance(installation_id, bool)
                    or not isinstance(installation_id, int)
                    or installation_id <= 0):
                raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE)
            if installation_id not in seen:
                seen.add(installation_id)
                installation_ids.append(installation_id)

        if selected_installation_id is not None:
            if selected_installation_id not in seen:
                raise InstallationBindingError(CODE_INSTALLATION_NOT_AUTHORIZED)
        elif not installation_ids:
            return {"status": "not_found", "verified_candidates": []}

        from agent.github_app.client import GitHubAPIError

        try:
            expected_app_id = self._app.app_id()
        except InstallationBindingError:
            raise
        except GitHubAPIError:
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE) from None
        if (isinstance(expected_app_id, bool)
                or not isinstance(expected_app_id, int)
                or expected_app_id <= 0):
            raise InstallationBindingError(CODE_APP_NOT_CONFIGURED)

        candidates = []
        for installation_id in installation_ids:
            facts = self._installation_facts(
                installation_id, allow_not_found=True)
            # /user/installations lists every App visible to the linked user.
            # A 404 from this App-scoped endpoint means this entry belongs to
            # another App, so it is not a candidate during discovery.
            if facts is None:
                continue
            app_id = facts["app_id"]
            if (isinstance(app_id, bool) or not isinstance(app_id, int)
                    or app_id <= 0 or app_id != expected_app_id
                    or facts["repository_selection"] not in ("all", "selected")):
                raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)
            candidates.append({
                "installation_id": installation_id,
                "account_id": facts["account_id"],
                "account_login": facts["account_login"],
                "account_type": facts["account_type"],
                "repository_selection": facts["repository_selection"],
                "facts": facts,
            })

        if selected_installation_id is not None:
            chosen = next(
                (candidate for candidate in candidates
                 if candidate["installation_id"] == selected_installation_id),
                None)
            if chosen is None:
                raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)
            return {
                "status": "ready",
                "verified_candidates": candidates,
                "chosen_installation_id": selected_installation_id,
            }

        if not candidates:
            return {"status": "not_found", "verified_candidates": []}

        if len(candidates) > 1:
            return {
                "status": "selection_required",
                "verified_candidates": candidates,
            }

        return {
            "status": "ready",
            "verified_candidates": candidates,
            "chosen_installation_id": candidates[0]["installation_id"],
        }

    def complete_reconciliation(self, store, *, discovery, tenant_id,
                                clerk_user_id, github_user_id):
        """Apply verified evidence inside the caller's transaction."""
        candidates = discovery["verified_candidates"]
        for candidate in candidates:
            owner = store.tenant_for_github_installation(
                candidate["installation_id"])
            if owner is not None and owner != tenant_id:
                raise InstallationBindingError(CODE_INSTALLATION_CLAIMED)

        if discovery["status"] == "not_found":
            return {"status": "not_found", "candidates": []}
        if discovery["status"] == "selection_required":
            return {
                "status": "selection_required",
                "candidates": [self._public_candidate(c) for c in candidates],
            }

        chosen = next(
            candidate for candidate in candidates
            if candidate["installation_id"] == discovery["chosen_installation_id"])
        self._persist_binding(
            store, installation_id=chosen["installation_id"],
            facts=chosen["facts"], tenant_id=tenant_id,
            clerk_user_id=clerk_user_id,
            verified_github_user_id=github_user_id,
            bound_via_state_id=None)
        logger.info("github_installation_bound", extra={
            "operation": "install_reconcile",
            "installation_id": chosen["installation_id"],
        })
        return {
            "status": "connected",
            "candidates": [],
            "installation_id": chosen["installation_id"],
        }

    @staticmethod
    def _public_candidate(candidate):
        return {key: candidate[key] for key in (
            "installation_id", "account_id", "account_login", "account_type",
            "repository_selection")}

    def _persist_binding(self, store, *, installation_id, facts, tenant_id,
                         clerk_user_id, verified_github_user_id,
                         bound_via_state_id):
        store.record_github_installation(
            installation_id,
            github_app_id=facts["app_id"],
            github_account_id=facts["account_id"],
            github_account_login=facts["account_login"],
            github_account_type=facts["account_type"],
            repository_selection=facts["repository_selection"],
            status="active",
        )

        from agent.postgres_lifecycle_store import TenantInstallationConflict

        try:
            _, created = store.bind_github_installation_to_tenant(
                installation_id,
                tenant_id=tenant_id,
                bound_by_clerk_user_id=clerk_user_id,
                verified_github_user_id=verified_github_user_id,
                bound_via_state_id=bound_via_state_id,
            )
        except TenantInstallationConflict as exc:
            raise InstallationBindingError(CODE_INSTALLATION_CLAIMED,
                                           str(exc)) from None
        return created

    # -- the individual verifications --------------------------------------

    def _installation_facts(self, installation_id, *, allow_not_found=False):
        """Ask GitHub, as the App, what this installation is.

        A 404 here is the answer for both "no such installation" and
        "an installation of a different GitHub App", because this endpoint is
        scoped to the App the JWT belongs to. Either way it must not be bound.
        """
        from agent.github_app.client import GitHubAPIError, GitHubNotFoundError

        try:
            document = self._client.get_installation(
                installation_id, self._jwt_factory())
        except GitHubNotFoundError:
            if allow_not_found:
                return None
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN) from None
        except GitHubAPIError:
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE) from None

        if not isinstance(document, dict):
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)

        # GitHub's own id, compared to the one presented. They should match;
        # if GitHub answered about something else, stop.
        reported_id = document.get("id")
        if reported_id != installation_id:
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)

        account = document.get("account")
        if not isinstance(account, dict):
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)
        account_id = account.get("id")
        account_login = account.get("login")
        account_type = account.get("type")
        if (not isinstance(account_id, int) or isinstance(account_id, bool)
                or account_id <= 0):
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)
        if not isinstance(account_login, str) or not account_login:
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)
        if account_type not in ("User", "Organization"):
            raise InstallationBindingError(CODE_INSTALLATION_UNKNOWN)

        app_id = document.get("app_id")
        selection = document.get("repository_selection")
        if selection not in (None, "all", "selected"):
            selection = None

        return {
            "app_id": app_id if isinstance(app_id, int) else None,
            "account_id": account_id,
            "account_login": account_login,
            "account_type": account_type,
            "repository_selection": selection,
        }

    def _verify_human(self, store, *, clerk_user_id, installation_id):
        """Ask GitHub, as the person, whether they can see this installation.

        Returns the verified numeric GitHub user id.

        If no GitHub identity is linked to this Clerk user, this raises rather
        than skipping the check. Binding without it is precisely the failure
        this module exists to prevent.
        """
        from agent.api.github_identity import (
            GitHubCredentialExpired, GitHubIdentityError,
        )

        access_token, github_user_id = self._linked_human(
            store, clerk_user_id=clerk_user_id)

        try:
            authorized = self._identity.user_can_access_installation(
                access_token, installation_id)
        except GitHubCredentialExpired:
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_UNUSABLE) from None
        except GitHubIdentityError:
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE) from None

        if not authorized:
            # The person completing the flow has no access to the installation
            # they named. This is exactly what a forged installation_id looks
            # like from here.
            logger.info("github_installation_not_authorized", extra={
                "operation": "install_setup",
                "installation_id": installation_id,
            })
            raise InstallationBindingError(CODE_INSTALLATION_NOT_AUTHORIZED)

        return github_user_id

    def _linked_human(self, store, *, clerk_user_id):
        """Return the decrypted linked-user token and immutable GitHub id."""
        from agent.api.session_crypto import CredentialEncryptionError, decrypt

        link = store.clerk_github_identity(clerk_user_id)
        if link is None:
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_REQUIRED)
        try:
            access_token = decrypt(self._session_key, link.get("access_token"),
                                   associated=clerk_user_id)
        except CredentialEncryptionError:
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_UNUSABLE) from None
        if not access_token:
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_REQUIRED)
        expires_at = link.get("access_expires_at")
        if expires_at is not None and expires_at <= self._clock():
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_UNUSABLE)
        github_user_id = link.get("github_user_id")
        if (isinstance(github_user_id, bool)
                or not isinstance(github_user_id, int)
                or github_user_id <= 0):
            raise InstallationBindingError(CODE_GITHUB_IDENTITY_UNUSABLE)
        return access_token, github_user_id


class GitHubIdentityLinker:
    """Proves that a Clerk user controls a particular GitHub account.

    ###############################################################
    # THE IDENTITY IS PROVED, NEVER INFERRED.                     #
    ###############################################################

    The only way a link is created is by completing GitHub OAuth as that
    account. Nothing here reads a GitHub identity out of a Clerk email
    address, a display name, a Clerk organization name, a GitHub organization
    name or an installation account. Every one of those is either mutable,
    unverified, or chosen by whoever is attacking — and matching on any of them
    would let someone claim a GitHub identity they do not hold.

    What is stored is the immutable numeric GitHub user id, plus the user
    credential encrypted with the same AES-256-GCM helper the dashboard
    sessions use. The credential exists for exactly one purpose: asking GitHub,
    as this person, which App installations they can actually see.

    This is a SEPARATE credential from the dashboard session in
    agent/api/sessions.py, and deliberately so. That one is scoped to a
    configured repository and re-verifies a repository permission; this one
    exists before any repository is known. Neither grants the other's
    authority, and this one grants no governance capability at all.
    """

    def __init__(self, *, client_id, client_secret, redirect_uri, session_key,
                 github_identity=None, clock=None,
                 lifetime=INSTALLATION_STATE_LIFETIME):
        from agent.api import github_identity as default_identity

        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._session_key = session_key
        self._identity = github_identity or default_identity
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lifetime = lifetime

    def expires_at(self):
        return self._clock() + self._lifetime

    def authorize_url(self, state):
        return self._identity.authorize_url(
            self._client_id, self._redirect_uri, state)

    def complete(self, store, *, code, presented_state):
        """Exchange the code, confirm who it belongs to, and store the link."""
        from agent.api.github_identity import GitHubIdentityError
        from agent.api.session_crypto import encrypt

        if not code or not isinstance(presented_state, str) or not presented_state:
            raise InstallationBindingError(CODE_STATE_INVALID)

        # Same single-use, expiring, atomically-claimed state as the
        # installation flow, and named with its own purpose so a state minted
        # for one flow cannot be spent on the other.
        claimed = store.consume_github_installation_state(
            hash_state(presented_state), now=self._clock(),
            purpose="github_identity_link")
        if claimed is None:
            raise InstallationBindingError(CODE_STATE_INVALID)

        try:
            credential = self._identity.exchange_code(
                client_id=self._client_id, client_secret=self._client_secret,
                code=code, redirect_uri=self._redirect_uri, now=self._clock())
            viewer = self._identity.fetch_viewer(credential.access_token)
        except GitHubIdentityError:
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE) from None

        github_user_id = viewer.get("user_id")
        login = viewer.get("login")
        if (not isinstance(github_user_id, int) or isinstance(github_user_id, bool)
                or github_user_id <= 0 or not isinstance(login, str) or not login):
            raise InstallationBindingError(CODE_GITHUB_UNAVAILABLE)

        clerk_user_id = claimed["clerk_user_id"]
        record = store.upsert_clerk_github_identity(
            clerk_user_id,
            github_user_id=github_user_id,
            github_login=login,
            # Encrypted before it reaches the database, bound to the Clerk user
            # id so a row lifted into another user's record fails to decrypt.
            access_token=encrypt(self._session_key, credential.access_token,
                                 associated=clerk_user_id),
            access_expires_at=credential.expires_at,
            refresh_token=encrypt(self._session_key, credential.refresh_token,
                                  associated=clerk_user_id),
            refresh_expires_at=credential.refresh_expires_at,
        )
        logger.info("github_identity_linked", extra={
            "operation": "github_link_callback",
            # An id, not a token and not an email.
            "github_user_id": github_user_id,
        })
        return record


def github_identity_payload(store, clerk_user_id):
    """Whether this Clerk user has proved a GitHub identity.

    Returns the login for display and nothing else. No token, no scope, no
    expiry — none of which the browser needs and none of which it should hold.
    """
    link = store.clerk_github_identity(clerk_user_id)
    if link is None:
        return {"linked": False, "login": None}
    return {"linked": True, "login": link.get("github_login")}


def installations_payload(store, tenant_id):
    """The GitHub section of the onboarding state, from stored facts only.

    No installation access token, and no credential of any kind, is included —
    none is stored, and none would belong in a browser response if it were.
    """
    rows = store.tenant_github_installations(tenant_id)
    if not rows:
        return {"status": "not_connected", "installations": []}

    installations = [{
        "installation_id": row["github_installation_id"],
        "account_login": row["github_account_login"],
        "account_type": row["github_account_type"],
        "account_id": row["github_account_id"],
        "repository_selection": row.get("repository_selection"),
        "status": row["status"],
        "connected_at": row["bound_at"].isoformat() if row.get("bound_at") else None,
    } for row in rows]

    # "connected" means at least one installation is actually usable. An
    # installation that GitHub has suspended is present and reported, but it
    # is not working, and saying "connected" would send the customer looking
    # for a different problem.
    active = any(row["status"] == "active" for row in rows)
    return {
        "status": "connected" if active else "suspended",
        "installations": installations,
    }
