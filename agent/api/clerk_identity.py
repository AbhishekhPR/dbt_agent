"""Clerk session-token verification: the human identity half of the API.

Clerk is Relium's identity provider. It answers *who is asking*. It does not
answer what they may do — that stays with :mod:`agent.api.authorization`, and
for anything touching a customer's repository it stays with GitHub. Nothing in
this module produces a permission.

WHAT IS VERIFIED, AND WHY EACH ONE MATTERS
------------------------------------------
A Clerk session token is a JWT. Every check below has a specific attack behind
it, so none of them is optional:

  algorithm   RS256 only, chosen from an ALLOW-list read out of our own
              configuration — never from the token's own header. Honouring the
              header's ``alg`` is the classic JWT break: ``none`` skips
              verification entirely, and ``HS256`` invites the verifier to use
              the public key as an HMAC secret, which is public.
  kid         Required, and must name a key we actually fetched from Clerk's
              JWKS. A token is never verified against a key it supplies.
  signature   RSA PKCS#1 v1.5 over SHA-256, across the exact
              ``header.payload`` bytes as received. Re-encoding first would let
              two different tokens verify as one.
  iss         Exact string match against the configured issuer. This is what
              binds a token to OUR Clerk instance: without it, a token minted
              by anybody else's Clerk application verifies perfectly well.
  exp         Required. An expired session is not a session.
  nbf / iat   Validated when present; a token that is not yet valid is refused.
  azp         Checked against the configured authorized parties when both are
              present. This is Clerk's origin binding, and it is what stops a
              token issued for a different frontend being replayed at ours.
  aud         Checked only when configured, because Clerk session tokens do not
              carry an audience by default.

WHAT IS NEVER TRUSTED
---------------------
The Clerk user id and organization id come from the verified payload and from
nowhere else. A request body claiming ``organization_id`` is data, not
identity — the same rule ``agent.api.auth`` already applies to service tokens,
where tenant scope is resolved from the credential rather than from the caller.

CONFIGURATION
-------------
Everything is environment-driven so a development Clerk instance and a
production one differ only by configuration. No publishable key, instance
hostname or issuer is compiled in.

No secret is required here at all. Verification uses Clerk's PUBLIC JWKS, so
this module never needs, reads, or stores a Clerk Secret Key.

Transport is stdlib urllib, matching agent/api/github_identity.py; nothing here
takes a new dependency. Signature verification uses ``cryptography``, which is
already a direct dependency.

Nothing in this module logs a token, a claim value, or any part of either.
"""
from __future__ import annotations

import base64
import binascii
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

#: The only signature algorithm accepted, as an allow-list. Clerk signs session
#: tokens with RS256. Widening this is a security change, not a compatibility
#: one.
ALLOWED_ALGORITHMS = frozenset({"RS256"})

#: Clock skew tolerated on exp/nbf/iat, in seconds. Matches Clerk's own SDK
#: default. Large values are not a kindness: they extend the life of a token
#: that has already expired.
DEFAULT_LEEWAY_SECONDS = 5

#: How long a fetched JWKS is reused before being refetched.
DEFAULT_JWKS_CACHE_SECONDS = 600

#: Minimum gap between JWKS refetches triggered by an unrecognised ``kid``.
#: Without it, a stream of tokens carrying forged key ids would turn every
#: request into an outbound fetch — a cheap amplification against Clerk and
#: against us.
DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS = 30

USER_AGENT = "relium-api"

#: Socket timeout for a JWKS fetch, in seconds. urllib exposes one timeout
#: covering connect and read rather than two, so this bounds both.
DEFAULT_JWKS_TIMEOUT_SECONDS = 5.0

#: Largest JWKS body accepted. A real key set is a few kilobytes; this is
#: generous while still refusing to hold an unbounded response in memory.
DEFAULT_JWKS_MAX_BYTES = 256 * 1024

#: Base delay before retrying after a failed fetch, doubling per consecutive
#: failure up to the cap below. Without it, an outage turns every request into
#: an outbound call and Relium becomes part of the problem.
DEFAULT_JWKS_FAILURE_BACKOFF_SECONDS = 5
DEFAULT_JWKS_MAX_FAILURE_BACKOFF_SECONDS = 300

#: How long already-fetched keys keep being served past their normal expiry
#: while refreshes are failing. Signing keys are long-lived, so continuing to
#: verify against the last known-good set is far better than signing every
#: customer out for the duration of a Clerk outage.
DEFAULT_JWKS_STALE_GRACE_SECONDS = 3600



class ClerkConfigurationError(Exception):
    """Clerk authentication is not configured for this deployment."""


class ClerkVerificationError(Exception):
    """The presented token is absent, malformed, or not verifiable.

    Deliberately one class with uniform, non-specific messages. Telling a
    caller *which* check failed — bad signature vs wrong issuer vs expired —
    is a probing oracle. The operator gets the detail in the audit log; the
    caller gets 401.
    """


class ClerkKeysUnavailable(Exception):
    """Clerk's JWKS could not be reached, so no verdict is possible.

    Distinct from :class:`ClerkVerificationError` because the correct response
    differs: a token we cannot verify is 401, but an outage that prevents us
    verifying anything is 503. Answering 401 during a JWKS outage would sign
    every customer out at once.
    """


def _b64url_decode(segment: str) -> bytes:
    """Decode base64url without padding, rejecting anything malformed."""
    if not isinstance(segment, str) or not segment:
        raise ClerkVerificationError("token is malformed")
    padding_needed = (-len(segment)) % 4
    try:
        return base64.urlsafe_b64decode(segment + ("=" * padding_needed))
    except (binascii.Error, ValueError):
        raise ClerkVerificationError("token is malformed") from None


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def _json_segment(segment: str) -> dict:
    try:
        document = json.loads(_b64url_decode(segment))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ClerkVerificationError("token is malformed") from None
    if not isinstance(document, dict):
        raise ClerkVerificationError("token is malformed")
    return document


@dataclass(frozen=True)
class ClerkIdentity:
    """A verified Clerk session, and nothing more.

    There is no permission on this object on purpose. It says who the person
    is and which Clerk organization was active when the token was minted. What
    they may do is resolved afterwards, from Relium's own records.
    """

    #: Clerk user id, from ``sub``. Stable, opaque, and the only durable
    #: identifier for the human. A display name or email is neither.
    user_id: str
    #: Active Clerk organization id, or None for a personal account.
    organization_id: str | None
    #: Clerk's role string within that organization, or None. Recorded for
    #: audit; it is NOT used as a Relium permission.
    organization_role: str | None
    #: Clerk session id, from ``sid``. Useful for correlating an audit trail.
    session_id: str | None
    expires_at: datetime


@dataclass(frozen=True)
class ClerkSettings:
    """Where this deployment's Clerk instance lives.

    ``issuer`` is the only required value, and it is what pins verification to
    one Clerk instance. A development instance and a production instance differ
    by this string alone.
    """

    issuer: str
    jwks_url: str
    #: Accepted ``azp`` values — the frontend origins allowed to present tokens
    #: here. Empty means unchecked, which is acceptable for local development
    #: and should be populated in production.
    authorized_parties: frozenset = frozenset()
    #: Accepted ``aud``. Empty means unchecked; Clerk session tokens carry no
    #: audience unless one is configured in the Clerk dashboard.
    audiences: frozenset = frozenset()
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS
    cache_seconds: int = DEFAULT_JWKS_CACHE_SECONDS

    @classmethod
    def from_environ(cls, environ):
        """Build settings from environment variables, or return None.

        Returning None rather than raising is deliberate: a deployment that has
        not configured Clerk is a valid deployment (the GitHub App server runs
        without the API at all), and it should start normally with the
        onboarding routes answering "not configured" instead of failing to boot.
        """
        # None means "the process environment", matching load_settings in
        # agent/github_app/settings.py. Without this the production bootstrap
        # -- which passes environ through from main() and normally has None --
        # would raise on a None lookup instead of reading the environment.
        if environ is None:
            import os as _os

            environ = _os.environ

        issuer = (environ.get("RELIUM_CLERK_ISSUER") or "").strip().rstrip("/")
        if not issuer:
            return None
        if not issuer.startswith("https://"):
            # An http issuer would allow the JWKS to be substituted in transit,
            # which defeats the whole verification.
            raise ClerkConfigurationError(
                "RELIUM_CLERK_ISSUER must be an https URL")

        jwks_url = (environ.get("RELIUM_CLERK_JWKS_URL") or "").strip()
        if not jwks_url:
            jwks_url = f"{issuer}/.well-known/jwks.json"
        if not jwks_url.startswith("https://"):
            raise ClerkConfigurationError(
                "RELIUM_CLERK_JWKS_URL must be an https URL")

        def _set(name):
            raw = environ.get(name) or ""
            return frozenset(part.strip() for part in raw.split(",") if part.strip())

        leeway = environ.get("RELIUM_CLERK_LEEWAY_SECONDS")
        try:
            leeway_seconds = int(leeway) if leeway else DEFAULT_LEEWAY_SECONDS
        except (TypeError, ValueError):
            raise ClerkConfigurationError(
                "RELIUM_CLERK_LEEWAY_SECONDS must be an integer") from None
        if leeway_seconds < 0 or leeway_seconds > 300:
            raise ClerkConfigurationError(
                "RELIUM_CLERK_LEEWAY_SECONDS must be between 0 and 300")

        return cls(
            issuer=issuer,
            jwks_url=jwks_url,
            authorized_parties=_set("RELIUM_CLERK_AUTHORIZED_PARTIES"),
            audiences=_set("RELIUM_CLERK_AUDIENCE"),
            leeway_seconds=leeway_seconds,
        )


class JwksCache:
    """Clerk's public signing keys, fetched over https and cached.

    ###################################################################
    # THE FETCH TARGET IS CONFIGURATION, NEVER THE TOKEN.             #
    ###################################################################

    ``jwks_url`` comes from ``ClerkSettings``, which reads it from the
    environment and requires https. Nothing in a presented token — not
    ``iss``, not ``kid``, not any header — can influence which host is
    contacted. A verifier that fetched keys from a URL inside the token would
    accept any token an attacker could host a key set for, and would double as
    an SSRF primitive pointed at whatever the backend can reach.

    Redirects are refused for the same reason: following one would let the
    configured host hand the fetch off to an arbitrary other host, including a
    link-local or internal address, after configuration had already been
    validated.

    AVAILABILITY
    ------------
    Signing keys are long-lived and rotate rarely, so a JWKS fetch failure
    should not sign every customer out. Three behaviours follow from that:

      * a successful fetch is cached for ``cache_seconds``;
      * a failed fetch is remembered, and further attempts back off, so an
        outage does not turn every request into an outbound call;
      * cached keys keep being served for ``stale_grace_seconds`` past their
        normal expiry while refreshes are failing. Verification stays real —
        the signature is still checked against keys Clerk published — it is
        only their freshness that is relaxed, and only for a bounded window.

    Thread-safe: the API runs store work in a threadpool, so two requests can
    arrive here at once.
    """

    def __init__(self, jwks_url, *, cache_seconds=DEFAULT_JWKS_CACHE_SECONDS,
                 opener=None, clock=time.monotonic,
                 timeout=DEFAULT_JWKS_TIMEOUT_SECONDS,
                 refresh_cooldown=DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS,
                 max_bytes=DEFAULT_JWKS_MAX_BYTES,
                 failure_backoff=DEFAULT_JWKS_FAILURE_BACKOFF_SECONDS,
                 max_failure_backoff=DEFAULT_JWKS_MAX_FAILURE_BACKOFF_SECONDS,
                 stale_grace_seconds=DEFAULT_JWKS_STALE_GRACE_SECONDS):
        self._url = jwks_url
        self._cache_seconds = cache_seconds
        self._opener = opener
        self._clock = clock
        self._timeout = timeout
        self._refresh_cooldown = refresh_cooldown
        self._max_bytes = max_bytes
        self._failure_backoff = failure_backoff
        self._max_failure_backoff = max_failure_backoff
        self._stale_grace_seconds = stale_grace_seconds

        self._lock = threading.Lock()
        self._keys = {}
        self._fetched_at = None
        self._last_attempt_at = None
        # Tracked separately from _last_attempt_at. The rotation cooldown has
        # to be measured from the last rotation PROBE, not from the last fetch
        # of any kind: measuring it from any fetch means the probe that
        # populated the cache also starts the cooldown, and the first unknown
        # kid after it — the actual rotation — is refused for a whole TTL.
        self._last_rotation_probe_at = None
        self._consecutive_failures = 0

    # -- public ------------------------------------------------------------

    def key_for(self, kid):
        """Return the public key for ``kid``, refetching when appropriate."""
        with self._lock:
            if self._is_fresh() and kid in self._keys:
                return self._keys[kid]

            if self._should_attempt_refresh(kid):
                try:
                    self._refresh_locked()
                except ClerkKeysUnavailable:
                    self._record_failure()
                    # Serving keys that are stale but still inside the grace
                    # window beats refusing every request during an outage.
                    if not self._has_usable_stale_keys():
                        raise

            key = self._keys.get(kid)
            if key is None:
                if not self._keys:
                    # We have never held a key set, so we cannot say whether
                    # this token is bad. That is an outage, not a forgery.
                    raise ClerkKeysUnavailable(
                        "Clerk signing keys are not available")
                # We hold Clerk's keys and this is not one of them.
                raise ClerkVerificationError("token key is not recognised")
            return key

    # -- refresh policy ----------------------------------------------------

    def _is_fresh(self):
        return (self._fetched_at is not None
                and (self._clock() - self._fetched_at) < self._cache_seconds)

    def _has_usable_stale_keys(self):
        if not self._keys or self._fetched_at is None:
            return False
        age = self._clock() - self._fetched_at
        return age < (self._cache_seconds + self._stale_grace_seconds)

    def _backoff_seconds(self):
        """Exponential, capped. Keeps an outage from becoming a stampede."""
        if self._consecutive_failures <= 0:
            return 0
        delay = self._failure_backoff * (2 ** (self._consecutive_failures - 1))
        return min(delay, self._max_failure_backoff)

    def _should_attempt_refresh(self, kid):
        now = self._clock()
        if self._last_attempt_at is not None:
            if (now - self._last_attempt_at) < self._backoff_seconds():
                return False
        if self._fetched_at is None:
            return True
        if not self._is_fresh():
            return True
        # Fresh, but this kid is absent — what a legitimate key rotation looks
        # like. The first such kid is probed immediately, so a rotation is
        # picked up at once rather than after the cache expires. Subsequent
        # ones are rate-limited, because a stream of tokens carrying forged key
        # ids would otherwise turn every request into an outbound fetch.
        if kid not in self._keys:
            due = (self._last_rotation_probe_at is None
                   or (now - self._last_rotation_probe_at) >= self._refresh_cooldown)
            if due:
                self._last_rotation_probe_at = now
            return due
        return False

    def _record_failure(self):
        self._consecutive_failures += 1

    # -- fetching ----------------------------------------------------------

    def _refresh_locked(self):
        self._last_attempt_at = self._clock()
        document = self._fetch()
        keys = {}
        for jwk in document.get("keys") or ():
            if not isinstance(jwk, dict):
                continue
            # Only RSA signing keys are usable here, and only with the one
            # algorithm we accept. A JWKS entry declaring anything else is
            # skipped rather than coerced.
            if jwk.get("kty") != "RSA":
                continue
            if jwk.get("use") not in (None, "sig"):
                continue
            if jwk.get("alg") not in (None, "RS256"):
                continue
            kid = jwk.get("kid")
            if not isinstance(kid, str) or not kid:
                continue
            try:
                keys[kid] = _rsa_public_key(jwk)
            except Exception:
                # One unusable entry must not discard the rest of the set.
                continue
        if not keys:
            raise ClerkKeysUnavailable("Clerk published no usable signing keys")
        # Replaced wholesale rather than merged: a key Clerk has retired must
        # stop verifying tokens here too, which merging would prevent.
        self._keys = keys
        self._fetched_at = self._clock()
        self._consecutive_failures = 0

    def _fetch(self):
        request = urllib.request.Request(self._url, method="GET")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        send = self._opener or _default_opener().open
        try:
            with send(request, timeout=self._timeout) as response:
                # Bounded read. A bare `response.read()` holds whatever the far
                # end sends, so a hostile or broken endpoint could exhaust
                # memory. One byte over the cap is read deliberately, so an
                # oversized body is detected rather than silently truncated
                # into a parse error.
                payload = response.read(self._max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise ClerkKeysUnavailable(
                "Clerk returned HTTP %s for its signing keys" % exc.code) from None
        except urllib.error.URLError:
            raise ClerkKeysUnavailable(
                "Clerk was unreachable for its signing keys") from None
        except OSError:
            # Socket timeouts and connection resets arrive here.
            raise ClerkKeysUnavailable(
                "Clerk was unreachable for its signing keys") from None

        if len(payload) > self._max_bytes:
            raise ClerkKeysUnavailable("Clerk key set exceeded the size limit")
        try:
            document = json.loads(payload or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ClerkKeysUnavailable("Clerk returned a malformed key set") from None
        if not isinstance(document, dict):
            raise ClerkKeysUnavailable("Clerk returned a malformed key set")
        return document


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect away from the configured JWKS host.

    The configured URL is validated as https at load time. Following a redirect
    would let that host move the fetch to any other host — including an
    internal or link-local address — after validation had already passed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects are not followed for JWKS",
            headers, fp)


_OPENER = None


def _default_opener():
    """An https opener that does not follow redirects.

    Built once. ``urllib.request.urlopen`` uses a global opener with redirect
    handling installed, which is exactly what must not happen here.
    """
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            _RefuseRedirects, urllib.request.HTTPSHandler())
    return _OPENER


def _rsa_public_key(jwk):
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    modulus = _b64url_uint(jwk["n"])
    exponent = _b64url_uint(jwk["e"])
    if modulus.bit_length() < 2048:
        # Anything smaller is not a key Clerk issues, and accepting it would
        # accept a key weak enough to be worth attacking.
        raise ValueError("RSA modulus is too small")
    return RSAPublicNumbers(exponent, modulus).public_key()


class ClerkVerifier:
    """Verifies Clerk session tokens against one configured Clerk instance."""

    def __init__(self, settings: ClerkSettings, *, jwks=None, clock=None):
        self._settings = settings
        self._jwks = jwks or JwksCache(settings.jwks_url,
                                       cache_seconds=settings.cache_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def issuer(self):
        return self._settings.issuer

    def verify(self, token: str | None) -> ClerkIdentity:
        """Verify a token and return the identity in it, or raise.

        Raises :class:`ClerkVerificationError` for anything wrong with the
        token, and :class:`ClerkKeysUnavailable` when Clerk's keys could not be
        fetched. Callers must map those to 401 and 503 respectively.
        """
        if not isinstance(token, str) or not token.strip():
            raise ClerkVerificationError("no token presented")
        token = token.strip()

        parts = token.split(".")
        if len(parts) != 3:
            raise ClerkVerificationError("token is malformed")
        header_segment, payload_segment, signature_segment = parts

        header = _json_segment(header_segment)

        # The algorithm comes from OUR allow-list. The header only selects
        # within it, and a header naming anything else is refused outright.
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise ClerkVerificationError("token algorithm is not accepted")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ClerkVerificationError("token does not name a signing key")

        public_key = self._jwks.key_for(kid)
        signature = _b64url_decode(signature_segment)

        # Signed over the received bytes, not over a re-serialisation of the
        # decoded values.
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        self._verify_signature(public_key, signature, signing_input)

        payload = _json_segment(payload_segment)
        return self._verify_claims(payload)

    def _verify_signature(self, public_key, signature, signing_input):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            public_key.verify(signature, signing_input,
                              padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise ClerkVerificationError("token signature is invalid") from None

    def _verify_claims(self, payload) -> ClerkIdentity:
        settings = self._settings
        now = int(self._clock().timestamp())
        leeway = settings.leeway_seconds

        # Issuer. This is what makes the token OURS rather than merely
        # well-formed. Compared exactly, with no prefix or suffix matching.
        issuer = payload.get("iss")
        if not isinstance(issuer, str) or issuer.rstrip("/") != settings.issuer:
            raise ClerkVerificationError("token issuer is not accepted")

        expires_at = payload.get("exp")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise ClerkVerificationError("token has no expiry")
        if expires_at + leeway <= now:
            raise ClerkVerificationError("token has expired")

        not_before = payload.get("nbf")
        if not_before is not None:
            if not isinstance(not_before, int) or isinstance(not_before, bool):
                raise ClerkVerificationError("token is malformed")
            if not_before - leeway > now:
                raise ClerkVerificationError("token is not yet valid")

        issued_at = payload.get("iat")
        if issued_at is not None:
            if not isinstance(issued_at, int) or isinstance(issued_at, bool):
                raise ClerkVerificationError("token is malformed")
            if issued_at - leeway > now:
                raise ClerkVerificationError("token is not yet valid")

        # Authorized party: Clerk's binding to the frontend origin that the
        # token was minted for.
        if settings.authorized_parties:
            azp = payload.get("azp")
            if not isinstance(azp, str) or azp not in settings.authorized_parties:
                raise ClerkVerificationError("token authorized party is not accepted")

        if settings.audiences:
            audience = payload.get("aud")
            presented = {audience} if isinstance(audience, str) else set(
                a for a in (audience or ()) if isinstance(a, str))
            if not presented & set(settings.audiences):
                raise ClerkVerificationError("token audience is not accepted")

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ClerkVerificationError("token does not identify a user")

        organization_id, organization_role = _organization_from(payload)

        session_id = payload.get("sid")
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        return ClerkIdentity(
            user_id=subject,
            organization_id=organization_id,
            organization_role=organization_role,
            session_id=session_id,
            expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc),
        )


def _organization_from(payload):
    """Read the active organization from either Clerk claim shape.

    Clerk's original session claims put the active organization in ``org_id`` /
    ``org_role``. The newer default claim set nests it as ``o.id`` / ``o.rol``.
    Both are read so a Clerk instance on either version verifies, and neither
    is invented when absent — a personal account legitimately has no
    organization, and that is represented as None rather than guessed.
    """
    organization = payload.get("o")
    if isinstance(organization, dict):
        identifier = organization.get("id")
        role = organization.get("rol")
        if isinstance(identifier, str) and identifier:
            return identifier, role if isinstance(role, str) and role else None

    identifier = payload.get("org_id")
    role = payload.get("org_role")
    if isinstance(identifier, str) and identifier:
        return identifier, role if isinstance(role, str) and role else None
    return None, None


@dataclass(frozen=True)
class ClerkPrincipal:
    """A human authenticated by Clerk, resolved to a Relium tenant.

    ###################################################################
    # THIS PRINCIPAL CARRIES NO GITHUB AUTHORITY.                     #
    ###################################################################

    ``github_permission`` is None and ``may_govern`` is False, permanently and
    by construction. Clerk knows who someone is; it knows nothing about their
    access to a customer's repository, so there is nothing here from which a
    repository permission could honestly be derived. Fabricating one — by
    reading a Clerk organization role, say — would hand governance authority to
    whoever administers a Clerk organization, which is not the same set of
    people as those with write access to the repository.

    ``identity_provider`` is what keeps that separation enforced rather than
    merely intended: capabilities declare which identity providers may hold
    them, and GOVERNANCE_WRITE and DASHBOARD_READ accept ``github`` only. See
    agent/api/authorization.py.

    When Clerk-issued sessions later need governance authority, the GitHub
    authorization context gets attached to this object — from a real GitHub
    credential, checked live against GitHub, exactly as
    agent/api/sessions.py does today. Not from the Clerk token.
    """

    clerk_user_id: str
    clerk_organization_id: str | None
    #: The Relium tenant this principal operates inside. None before the
    #: workspace has been created, which is a legitimate state during first-run
    #: onboarding and no other time.
    tenant_id: str | None
    clerk_session_id: str | None = None

    is_human = True
    identity_provider = "clerk"
    scope = "clerk"

    #: Never populated from Clerk. See the class docstring.
    github_permission = None
    may_govern = False

    @property
    def actor(self) -> str:
        """The identity recorded against anything this principal does."""
        return f"clerk:{self.clerk_user_id}"
