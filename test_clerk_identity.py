"""Clerk session-token verification.

No database and no network: the signing keys are generated in-process, so every
token here is synthetic and every failure mode can be produced deliberately —
including the ones a real Clerk cannot be asked for, like a token signed by
somebody else's key.

NO REAL CREDENTIAL APPEARS IN THIS FILE. The RSA keys are generated per run and
discarded; the issuers are .test hostnames; no publishable key, secret key or
live token is present, printed, or asserted against.
"""
from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric import rsa

from agent.api.clerk_identity import (
    ClerkConfigurationError, ClerkKeysUnavailable, ClerkPrincipal, ClerkSettings,
    ClerkVerificationError, ClerkVerifier,
)

ISSUER = "https://verifier-test.clerk.accounts.test"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(document) -> str:
    return _b64url(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def _b64url_uint(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


class _Signer:
    """One RSA key pair, plus the JWKS entry describing it."""

    def __init__(self, kid="test-key-1", key_size=2048):
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537,
                                                    key_size=key_size)

    def jwk(self):
        numbers = self.private_key.public_key().public_numbers()
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
                "n": _b64url_uint(numbers.n), "e": _b64url_uint(numbers.e)}

    def sign(self, header, payload):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(),
                                          hashes.SHA256())
        return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


class _StubJwks:
    """Stands in for the JWKS cache, with the network removed."""

    def __init__(self, signers, *, unavailable=False):
        self._keys = {s.kid: s.private_key.public_key() for s in signers}
        self._unavailable = unavailable
        self.lookups = 0

    def key_for(self, kid):
        self.lookups += 1
        if self._unavailable:
            raise ClerkKeysUnavailable("keys could not be fetched")
        key = self._keys.get(kid)
        if key is None:
            raise ClerkVerificationError("token key is not recognised")
        return key


def _claims(**overrides):
    base = {
        "iss": ISSUER,
        "sub": "user_2abcdefghijklmnop",
        "sid": "sess_2qrstuvwxyz",
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not _ABSENT}


_ABSENT = object()


def _verifier(signer, *, settings=None, jwks=None, now=NOW):
    settings = settings or ClerkSettings(
        issuer=ISSUER, jwks_url=f"{ISSUER}/.well-known/jwks.json")
    return ClerkVerifier(settings, jwks=jwks or _StubJwks([signer]),
                         clock=lambda: now)


class ClerkTokenAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.signer = _Signer()
        self.verifier = _verifier(self.signer)

    def _token(self, claims=None, header=None):
        return self.signer.sign(
            header or {"alg": "RS256", "typ": "JWT", "kid": self.signer.kid},
            claims or _claims())

    def test_a_well_formed_token_is_accepted(self):
        identity = self.verifier.verify(self._token())
        self.assertEqual(identity.user_id, "user_2abcdefghijklmnop")
        self.assertEqual(identity.session_id, "sess_2qrstuvwxyz")
        self.assertIsNone(identity.organization_id)

    def test_organization_is_read_from_the_v1_claim_shape(self):
        identity = self.verifier.verify(self._token(
            _claims(org_id="org_2acme", org_role="org:admin")))
        self.assertEqual(identity.organization_id, "org_2acme")
        self.assertEqual(identity.organization_role, "org:admin")

    def test_organization_is_read_from_the_v2_claim_shape(self):
        identity = self.verifier.verify(self._token(
            _claims(o={"id": "org_2acme", "rol": "admin"})))
        self.assertEqual(identity.organization_id, "org_2acme")
        self.assertEqual(identity.organization_role, "admin")

    def test_a_personal_account_has_no_organization_rather_than_a_guess(self):
        identity = self.verifier.verify(self._token())
        self.assertIsNone(identity.organization_id)
        self.assertIsNone(identity.organization_role)

    def test_expiry_is_exposed_as_an_aware_datetime(self):
        identity = self.verifier.verify(self._token())
        self.assertIsNotNone(identity.expires_at.tzinfo)


class ClerkTokenRejectionTests(unittest.TestCase):
    """Every check has an attack behind it. Each one is exercised."""

    def setUp(self):
        self.signer = _Signer()
        self.verifier = _verifier(self.signer)

    def _token(self, claims=None, header=None, signer=None):
        signer = signer or self.signer
        return signer.sign(
            header or {"alg": "RS256", "typ": "JWT", "kid": signer.kid},
            claims or _claims())

    def test_missing_token_is_refused(self):
        for absent in (None, "", "   "):
            with self.assertRaises(ClerkVerificationError):
                self.verifier.verify(absent)

    def test_malformed_token_is_refused(self):
        for malformed in ("not-a-jwt", "a.b", "a.b.c.d", "...",
                          "!!!.###.$$$", "eyJhbGciOiJSUzI1NiJ9"):
            with self.assertRaises(ClerkVerificationError):
                self.verifier.verify(malformed)

    def test_a_token_signed_by_another_key_is_refused(self):
        """The signature is the whole point. A valid-looking token from an
        attacker's key must not verify."""
        attacker = _Signer(kid=self.signer.kid)
        forged = self._token(signer=attacker)
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(forged)

    def test_a_tampered_payload_is_refused(self):
        """Editing a claim after signing must invalidate the token.

        This is the attack that matters most here: escalating from one Clerk
        organization to another by rewriting org_id.
        """
        token = self._token(_claims(org_id="org_2mine"))
        header, payload, signature = token.split(".")
        tampered_payload = _b64url_json(_claims(org_id="org_2someone_else"))
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(f"{header}.{tampered_payload}.{signature}")

    def test_alg_none_is_refused(self):
        """The classic JWT break: a header asking not to be verified."""
        header = _b64url_json({"alg": "none", "typ": "JWT", "kid": self.signer.kid})
        payload = _b64url_json(_claims())
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(f"{header}.{payload}.")

    def test_hmac_algorithm_confusion_is_refused(self):
        """HS256 signed with the public key must not verify.

        The algorithm is chosen from our allow-list, not from the header, so
        this never reaches an HMAC comparison at all.
        """
        import hashlib
        import hmac as hmac_module

        from cryptography.hazmat.primitives import serialization

        public_pem = self.signer.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)
        header = _b64url_json({"alg": "HS256", "typ": "JWT", "kid": self.signer.kid})
        payload = _b64url_json(_claims())
        signing_input = f"{header}.{payload}".encode("ascii")
        forged = _b64url(hmac_module.new(public_pem, signing_input,
                                         hashlib.sha256).digest())
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(f"{header}.{payload}.{forged}")

    def test_a_token_without_a_kid_is_refused(self):
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(self._token(header={"alg": "RS256", "typ": "JWT"}))

    def test_an_unknown_kid_is_refused(self):
        other = _Signer(kid="rotated-away")
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(self._token(signer=other))

    def test_an_expired_token_is_refused(self):
        expired = _claims(exp=int((NOW - timedelta(minutes=1)).timestamp()))
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(self._token(expired))

    def test_a_token_with_no_expiry_is_refused(self):
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(self._token(_claims(exp=_ABSENT)))

    def test_a_not_yet_valid_token_is_refused(self):
        future = _claims(nbf=int((NOW + timedelta(minutes=5)).timestamp()))
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(self._token(future))

    def test_a_wrong_issuer_is_refused(self):
        """A token from somebody else's Clerk application is perfectly valid,
        correctly signed by its own issuer — and must not be accepted here."""
        foreign = _Signer()
        verifier = _verifier(foreign)
        token = foreign.sign(
            {"alg": "RS256", "typ": "JWT", "kid": foreign.kid},
            _claims(iss="https://someone-else.clerk.accounts.test"))
        with self.assertRaises(ClerkVerificationError):
            verifier.verify(token)

    def test_an_issuer_that_merely_starts_with_ours_is_refused(self):
        token = self._token(_claims(iss=f"{ISSUER}.evil.test"))
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(token)

    def test_a_token_without_a_subject_is_refused(self):
        with self.assertRaises(ClerkVerificationError):
            self.verifier.verify(self._token(_claims(sub=_ABSENT)))

    def test_an_unaccepted_authorized_party_is_refused(self):
        settings = ClerkSettings(
            issuer=ISSUER, jwks_url=f"{ISSUER}/jwks",
            authorized_parties=frozenset({"https://app.relium.test"}))
        verifier = _verifier(self.signer, settings=settings)
        with self.assertRaises(ClerkVerificationError):
            verifier.verify(self._token(_claims(azp="https://evil.test")))

    def test_an_accepted_authorized_party_passes(self):
        settings = ClerkSettings(
            issuer=ISSUER, jwks_url=f"{ISSUER}/jwks",
            authorized_parties=frozenset({"https://app.relium.test"}))
        verifier = _verifier(self.signer, settings=settings)
        identity = verifier.verify(self._token(_claims(azp="https://app.relium.test")))
        self.assertEqual(identity.user_id, "user_2abcdefghijklmnop")

    def test_an_unaccepted_audience_is_refused(self):
        settings = ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks",
                                 audiences=frozenset({"relium-api"}))
        verifier = _verifier(self.signer, settings=settings)
        with self.assertRaises(ClerkVerificationError):
            verifier.verify(self._token(_claims(aud="someone-else")))

    def test_keys_unavailable_is_not_reported_as_a_bad_token(self):
        """An outage must be distinguishable from a forgery.

        They map to different HTTP statuses: 503 keeps a signed-in customer
        signed in, 401 signs everybody out at once.
        """
        verifier = _verifier(self.signer, jwks=_StubJwks([self.signer], unavailable=True))
        with self.assertRaises(ClerkKeysUnavailable):
            verifier.verify(self._token())

    def test_refusal_messages_do_not_echo_the_token(self):
        """A refusal must not become an oracle, or a way to log a credential."""
        token = self._token(_claims(exp=int((NOW - timedelta(days=1)).timestamp())))
        with self.assertRaises(ClerkVerificationError) as caught:
            self.verifier.verify(token)
        message = str(caught.exception)
        self.assertNotIn(token, message)
        for segment in token.split("."):
            self.assertNotIn(segment, message)


class ClerkSettingsTests(unittest.TestCase):
    def test_absent_issuer_yields_no_settings_rather_than_an_error(self):
        """A deployment without Clerk is valid and must still start."""
        self.assertIsNone(ClerkSettings.from_environ({}))
        self.assertIsNone(ClerkSettings.from_environ({"RELIUM_CLERK_ISSUER": "  "}))

    def test_jwks_url_defaults_to_the_issuer_well_known_path(self):
        settings = ClerkSettings.from_environ({"RELIUM_CLERK_ISSUER": ISSUER})
        self.assertEqual(settings.jwks_url, f"{ISSUER}/.well-known/jwks.json")

    def test_a_trailing_slash_on_the_issuer_is_normalised(self):
        settings = ClerkSettings.from_environ({"RELIUM_CLERK_ISSUER": ISSUER + "/"})
        self.assertEqual(settings.issuer, ISSUER)

    def test_an_insecure_issuer_is_refused(self):
        with self.assertRaises(ClerkConfigurationError):
            ClerkSettings.from_environ({"RELIUM_CLERK_ISSUER": "http://insecure.test"})

    def test_an_insecure_jwks_url_is_refused(self):
        with self.assertRaises(ClerkConfigurationError):
            ClerkSettings.from_environ({
                "RELIUM_CLERK_ISSUER": ISSUER,
                "RELIUM_CLERK_JWKS_URL": "http://insecure.test/jwks"})

    def test_authorized_parties_and_audiences_are_parsed_as_lists(self):
        settings = ClerkSettings.from_environ({
            "RELIUM_CLERK_ISSUER": ISSUER,
            "RELIUM_CLERK_AUTHORIZED_PARTIES": "https://a.test, https://b.test",
            "RELIUM_CLERK_AUDIENCE": "relium-api"})
        self.assertEqual(settings.authorized_parties,
                         frozenset({"https://a.test", "https://b.test"}))
        self.assertEqual(settings.audiences, frozenset({"relium-api"}))

    def test_an_absurd_leeway_is_refused(self):
        for value in ("-1", "3600", "not-a-number"):
            with self.assertRaises(ClerkConfigurationError):
                ClerkSettings.from_environ({"RELIUM_CLERK_ISSUER": ISSUER,
                                            "RELIUM_CLERK_LEEWAY_SECONDS": value})

    def test_no_publishable_or_secret_key_is_required(self):
        """Verification uses Clerk's PUBLIC keys. If a secret were needed here,
        someone would eventually put one in the wrong place."""
        settings = ClerkSettings.from_environ({"RELIUM_CLERK_ISSUER": ISSUER})
        self.assertNotIn("secret", repr(settings).lower())
        self.assertNotIn("pk_", repr(settings))


class ClerkPrincipalTests(unittest.TestCase):
    """The principal must not be able to acquire GitHub authority."""

    def _principal(self, **overrides):
        fields = {"clerk_user_id": "user_2abc", "clerk_organization_id": "org_2acme",
                  "tenant_id": "ten_" + "0" * 32}
        fields.update(overrides)
        return ClerkPrincipal(**fields)

    def test_a_clerk_principal_never_carries_a_github_permission(self):
        principal = self._principal()
        self.assertIsNone(principal.github_permission)
        self.assertFalse(principal.may_govern)

    def test_a_clerk_principal_is_immutable(self):
        """may_govern must not be assignable after construction."""
        principal = self._principal()
        with self.assertRaises((AttributeError, TypeError)):
            principal.may_govern = True

    def test_the_actor_names_the_identity_provider(self):
        self.assertEqual(self._principal().actor, "clerk:user_2abc")

    def test_a_tenant_is_optional_before_the_workspace_exists(self):
        self.assertIsNone(self._principal(tenant_id=None).tenant_id)


if __name__ == "__main__":
    unittest.main()
