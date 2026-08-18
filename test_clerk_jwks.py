"""JWKS fetching: caching, rotation, backoff, bounds and SSRF resistance.

No network. The opener is a scripted stand-in, so an outage, a rotation, a
hostile oversized body and a redirect can each be produced on demand — none of
which a real Clerk can be asked for.

NO REAL CREDENTIAL APPEARS IN THIS FILE. Keys are generated per run.
"""
from __future__ import annotations

import base64
import io
import json
import unittest
import urllib.error

from cryptography.hazmat.primitives.asymmetric import rsa

from agent.api.clerk_identity import (
    ClerkKeysUnavailable, ClerkVerificationError, JwksCache,
)

JWKS_URL = "https://jwks-test.clerk.accounts.test/.well-known/jwks.json"


def _b64url_uint(value: int) -> str:
    return base64.urlsafe_b64encode(
        value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode("ascii")


def _jwk(kid, key=None, **overrides):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    document = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                "n": _b64url_uint(numbers.n), "e": _b64url_uint(numbers.e)}
    document.update(overrides)
    return document


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Opener:
    """A scripted JWKS endpoint. Records every call it receives."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.calls.append(request.full_url)
        self.timeouts.append(timeout)
        outcome = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, bytes):
            return _Response(outcome)
        return _Response(json.dumps(outcome).encode("utf-8"))


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class JwksCachingTests(unittest.TestCase):
    def test_a_successful_fetch_is_cached(self):
        opener = _Opener({"keys": [_jwk("k1")]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock, cache_seconds=600)

        for _ in range(5):
            cache.key_for("k1")
        self.assertEqual(len(opener.calls), 1, "cache did not prevent refetching")

    def test_the_cache_expires(self):
        opener = _Opener({"keys": [_jwk("k1")]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock, cache_seconds=600)

        cache.key_for("k1")
        clock.advance(601)
        cache.key_for("k1")
        self.assertEqual(len(opener.calls), 2)

    def test_the_configured_timeout_is_passed_to_every_fetch(self):
        opener = _Opener({"keys": [_jwk("k1")]})
        cache = JwksCache(JWKS_URL, opener=opener, clock=_Clock(), timeout=3.5)
        cache.key_for("k1")
        self.assertEqual(opener.timeouts, [3.5])

    def test_a_default_timeout_is_always_set(self):
        """An unbounded fetch would hold a request thread indefinitely."""
        opener = _Opener({"keys": [_jwk("k1")]})
        cache = JwksCache(JWKS_URL, opener=opener, clock=_Clock())
        cache.key_for("k1")
        self.assertIsNotNone(opener.timeouts[0])
        self.assertGreater(opener.timeouts[0], 0)


class JwksRotationTests(unittest.TestCase):
    def test_an_unknown_kid_triggers_one_refetch(self):
        """What a legitimate key rotation looks like from here."""
        first, second = _jwk("old"), _jwk("new")
        opener = _Opener({"keys": [first]}, {"keys": [first, second]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock)

        cache.key_for("old")
        self.assertEqual(len(opener.calls), 1)
        cache.key_for("new")          # unknown -> refetch -> found
        self.assertEqual(len(opener.calls), 2)

    def test_repeated_unknown_kids_are_rate_limited(self):
        """A stream of forged key ids must not become an outbound fetch each.

        Without the cooldown this is a free amplifier pointed at Clerk.
        """
        opener = _Opener({"keys": [_jwk("k1")]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock,
                          refresh_cooldown=30)

        cache.key_for("k1")
        for index in range(20):
            with self.assertRaises(ClerkVerificationError):
                cache.key_for(f"forged-{index}")
        self.assertLessEqual(len(opener.calls), 2,
                             f"unknown kids caused {len(opener.calls)} fetches")

    def test_the_cooldown_eventually_allows_another_refresh(self):
        first = _jwk("k1")
        rotated = _jwk("k2")
        # Fetch 1 populates the cache. Fetch 2 is the rotation probe, which
        # arrives before Clerk has published the new key. Fetch 3 finds it.
        opener = _Opener({"keys": [first]}, {"keys": [first]},
                         {"keys": [first, rotated]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock,
                          refresh_cooldown=30, cache_seconds=600)

        cache.key_for("k1")
        self.assertEqual(len(opener.calls), 1)

        with self.assertRaises(ClerkVerificationError):
            cache.key_for("k2")
        self.assertEqual(len(opener.calls), 2, "the first unknown kid must probe")

        with self.assertRaises(ClerkVerificationError):
            cache.key_for("k2")
        self.assertEqual(len(opener.calls), 2, "the cooldown must suppress the retry")

        clock.advance(31)
        cache.key_for("k2")
        self.assertEqual(len(opener.calls), 3)

    def test_a_retired_key_stops_verifying(self):
        """Keys are replaced wholesale, not merged. A key Clerk has withdrawn
        must stop working here too."""
        opener = _Opener({"keys": [_jwk("retiring")]}, {"keys": [_jwk("current")]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock, cache_seconds=600)

        cache.key_for("retiring")
        clock.advance(601)
        cache.key_for("current")
        with self.assertRaises(ClerkVerificationError):
            cache.key_for("retiring")


class JwksFailureTests(unittest.TestCase):
    def test_a_first_fetch_failure_is_an_outage_not_a_bad_token(self):
        opener = _Opener(urllib.error.URLError("down"))
        cache = JwksCache(JWKS_URL, opener=opener, clock=_Clock())
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_failures_back_off_instead_of_retrying_every_request(self):
        """During an outage Relium must not become part of the problem."""
        opener = _Opener(urllib.error.URLError("down"))
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock,
                          failure_backoff=5, max_failure_backoff=300)

        for _ in range(50):
            with self.assertRaises(ClerkKeysUnavailable):
                cache.key_for("k1")
        self.assertLessEqual(len(opener.calls), 2,
                             f"backoff did not hold: {len(opener.calls)} fetches")

    def test_backoff_grows_and_is_capped(self):
        opener = _Opener(urllib.error.URLError("down"))
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock,
                          failure_backoff=5, max_failure_backoff=60)

        for _ in range(12):
            with self.assertRaises(ClerkKeysUnavailable):
                cache.key_for("k1")
            clock.advance(1000)  # past any backoff, so each attempt is made
        self.assertLessEqual(cache._backoff_seconds(), 60)
        self.assertGreater(cache._backoff_seconds(), 0)

    def test_cached_keys_keep_working_while_refreshes_fail(self):
        """A Clerk outage must not sign every customer out.

        The signature is still verified against keys Clerk published; only
        their freshness is relaxed, and only for a bounded window.
        """
        opener = _Opener({"keys": [_jwk("k1")]}, urllib.error.URLError("down"))
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock,
                          cache_seconds=600, stale_grace_seconds=3600,
                          failure_backoff=0)

        cache.key_for("k1")
        clock.advance(700)                  # cache expired, refresh will fail
        self.assertIsNotNone(cache.key_for("k1"))

    def test_the_stale_grace_window_is_bounded(self):
        """Indefinitely stale keys would be a different bug."""
        opener = _Opener({"keys": [_jwk("k1")]}, urllib.error.URLError("down"))
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock,
                          cache_seconds=600, stale_grace_seconds=3600,
                          failure_backoff=0)

        cache.key_for("k1")
        clock.advance(600 + 3600 + 1)
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_recovery_clears_the_failure_state(self):
        opener = _Opener(urllib.error.URLError("down"), {"keys": [_jwk("k1")]})
        clock = _Clock()
        cache = JwksCache(JWKS_URL, opener=opener, clock=clock, failure_backoff=5)

        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")
        clock.advance(10)
        cache.key_for("k1")
        self.assertEqual(cache._backoff_seconds(), 0)

    def test_an_http_error_is_an_outage(self):
        error = urllib.error.HTTPError(JWKS_URL, 500, "boom", {}, None)
        cache = JwksCache(JWKS_URL, opener=_Opener(error), clock=_Clock())
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_a_socket_timeout_is_an_outage(self):
        cache = JwksCache(JWKS_URL, opener=_Opener(TimeoutError("slow")),
                          clock=_Clock())
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_an_empty_key_set_is_an_outage_not_an_empty_cache(self):
        cache = JwksCache(JWKS_URL, opener=_Opener({"keys": []}), clock=_Clock())
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")


class JwksBoundsTests(unittest.TestCase):
    def test_an_oversized_body_is_refused(self):
        """A bare read() holds whatever the far end sends."""
        huge = b'{"keys":[' + b'0' * (300 * 1024) + b']}'
        cache = JwksCache(JWKS_URL, opener=_Opener(huge), clock=_Clock(),
                          max_bytes=256 * 1024)
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_a_body_at_the_limit_is_still_parsed(self):
        payload = json.dumps({"keys": [_jwk("k1")]}).encode("utf-8")
        cache = JwksCache(JWKS_URL, opener=_Opener(payload), clock=_Clock(),
                          max_bytes=len(payload))
        self.assertIsNotNone(cache.key_for("k1"))

    def test_malformed_json_is_an_outage(self):
        cache = JwksCache(JWKS_URL, opener=_Opener(b"<html>not json</html>"),
                          clock=_Clock())
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_a_non_object_body_is_refused(self):
        cache = JwksCache(JWKS_URL, opener=_Opener(b'["not", "an", "object"]'),
                          clock=_Clock())
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("k1")

    def test_unusable_entries_are_skipped_without_discarding_the_set(self):
        good = _jwk("good")
        cache = JwksCache(JWKS_URL, clock=_Clock(), opener=_Opener({"keys": [
            "not-an-object",
            _jwk("ec-key", kty="EC"),
            _jwk("encryption-key", use="enc"),
            _jwk("wrong-alg", alg="RS512"),
            {"kty": "RSA", "kid": "malformed", "n": "!!!", "e": "AQAB"},
            good,
        ]}))
        self.assertIsNotNone(cache.key_for("good"))
        for rejected in ("ec-key", "encryption-key", "wrong-alg", "malformed"):
            with self.assertRaises(ClerkVerificationError, msg=rejected):
                cache.key_for(rejected)

    def test_a_weak_rsa_key_is_refused(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        cache = JwksCache(JWKS_URL, clock=_Clock(),
                          opener=_Opener({"keys": [_jwk("weak", key=weak)]}))
        with self.assertRaises(ClerkKeysUnavailable):
            cache.key_for("weak")


class JwksSsrfTests(unittest.TestCase):
    """The token must never influence which host is contacted."""

    def test_only_the_configured_url_is_ever_fetched(self):
        opener = _Opener({"keys": [_jwk("k1")]})
        cache = JwksCache(JWKS_URL, opener=opener, clock=_Clock())

        cache.key_for("k1")
        for hostile in ("http://169.254.169.254/latest/meta-data/",
                        "https://evil.test/jwks", "file:///etc/passwd",
                        "../../../etc/passwd", "http://localhost:5432/"):
            try:
                cache.key_for(hostile)
            except (ClerkVerificationError, ClerkKeysUnavailable):
                pass
        self.assertEqual(set(opener.calls), {JWKS_URL},
                         f"a kid influenced the fetch target: {set(opener.calls)}")

    def test_the_verifier_never_reads_a_url_from_the_token(self):
        """`jku` and `x5u` are JOSE headers that name a key location. They must
        be ignored outright: honouring one turns verification into 'trust
        whoever the token points at'."""
        with io.open("agent/api/clerk_identity.py", encoding="utf-8") as handle:
            source = handle.read()
        for header in ("jku", "x5u", "x5c", "jwk"):
            self.assertNotIn(f'"{header}"', source,
                             f"the {header} header must not be consulted")

    def test_redirects_are_refused(self):
        """A redirect would move the fetch to a host that never passed the
        https configuration check."""
        from agent.api.clerk_identity import _RefuseRedirects

        handler = _RefuseRedirects()
        request = urllib.request.Request(JWKS_URL)
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {},
                                     "http://169.254.169.254/")

    def test_the_default_opener_does_not_follow_redirects(self):
        from agent.api.clerk_identity import _RefuseRedirects, _default_opener

        handlers = [type(h) for h in _default_opener().handlers]
        self.assertIn(_RefuseRedirects, handlers)


class JwksConfigurationSourceTests(unittest.TestCase):
    def test_the_jwks_url_comes_from_settings_only(self):
        from agent.api.clerk_identity import ClerkSettings

        settings = ClerkSettings.from_environ({
            "RELIUM_CLERK_ISSUER": "https://configured.clerk.accounts.test"})
        self.assertTrue(settings.jwks_url.startswith(
            "https://configured.clerk.accounts.test/"))

    def test_an_http_jwks_url_is_refused_at_configuration_time(self):
        from agent.api.clerk_identity import ClerkConfigurationError, ClerkSettings

        with self.assertRaises(ClerkConfigurationError):
            ClerkSettings.from_environ({
                "RELIUM_CLERK_ISSUER": "https://ok.clerk.accounts.test",
                "RELIUM_CLERK_JWKS_URL": "http://169.254.169.254/jwks"})


if __name__ == "__main__":
    import urllib.request  # noqa: F401  (used by the redirect tests)

    unittest.main()
