"""Polar webhook signature verification.

Polar signs webhooks with the Standard Webhooks scheme. This is a direct,
dependency-free implementation of that scheme, matching what the Polar Python
SDK does through ``standardwebhooks``:

    headers
        webhook-id          unique message id, also the de-duplication key
        webhook-timestamp   unix seconds, as text
        webhook-signature   one or more space-separated `v1,<base64>` values

    signed content
        f"{webhook-id}.{webhook-timestamp}.{raw body}"

    signature
        base64( HMAC-SHA256( key, signed content ) )

###################################################################
# THE KEY IS THE SECRET STRING'S OWN BYTES.                       #
###################################################################

Standard Webhooks treats its secret as base64 (optionally `whsec_`-prefixed) and
decodes it. Polar's SDK base64-ENCODES the configured secret before handing it
over, so the two operations cancel and the HMAC key is the exact bytes of the
secret as typed into the Polar dashboard. That round trip is reproduced here
rather than assumed, so a secret that happens to look like base64 — or one that
starts with `whsec_` — is treated identically to how Polar treats it.

WHY THIS IS WRITTEN OUT RATHER THAN IMPORTED
--------------------------------------------
The repository installs from a hash-locked requirement set and the GitHub
webhook path is likewise stdlib hmac (agent/github_app/signatures.py). Adding a
transitive dependency chain to compute one HMAC would be a larger change to the
deployment than the code it replaces.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

#: Standard Webhooks' own tolerance, and the one Polar's SDK applies. A
#: correctly signed body replayed a day later is refused by this and by nothing
#: else, so it is not optional.
TOLERANCE_SECONDS = 300

MAX_SIGNATURE_HEADER_BYTES = 4096
MAX_DELIVERY_ID_LENGTH = 255


class SignatureError(Exception):
    """The delivery is not a verifiable Polar webhook.

    One exception for every failure mode. The caller answers 401 and does not
    say which check failed: naming it would turn the endpoint into an oracle for
    constructing a signature.
    """


def _key(secret) -> bytes:
    if isinstance(secret, bytes):
        raw = secret
    elif isinstance(secret, str):
        raw = secret.encode("utf-8")
    else:
        raise SignatureError("webhook secret must be text")
    if not raw:
        raise SignatureError("webhook secret must not be empty")

    # Polar: base64.b64encode(secret.encode()). Standard Webhooks then strips a
    # `whsec_` prefix if present and base64-decodes. Reproduced exactly.
    encoded = base64.b64encode(raw).decode("ascii")
    if encoded.startswith("whsec_"):
        encoded = encoded[len("whsec_"):]
    try:
        return base64.b64decode(encoded + "==")
    except (binascii.Error, ValueError):  # pragma: no cover - b64encode output
        raise SignatureError("webhook secret is unusable") from None


def verify(*, secret, body, headers, now):
    """Verify a delivery and return its ``webhook-id``, or raise.

    ``headers`` is any case-insensitive mapping (Starlette's ``request.headers``
    is one). ``now`` is a POSIX timestamp supplied by the caller so the
    tolerance window is testable without freezing the clock globally.

    The delivery id is RETURNED rather than read again by the caller, so the
    value used for de-duplication is provably the same one that was signed.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise SignatureError("webhook body must be bytes")

    delivery_id = _header(headers, "webhook-id")
    timestamp = _header(headers, "webhook-timestamp")
    presented = _header(headers, "webhook-signature")
    if not delivery_id or not timestamp or not presented:
        raise SignatureError("missing required webhook headers")
    if len(delivery_id) > MAX_DELIVERY_ID_LENGTH:
        raise SignatureError("webhook id is not usable")
    if len(presented) > MAX_SIGNATURE_HEADER_BYTES:
        # Bounded before any per-signature work, so a huge header cannot be used
        # to spend CPU on HMAC comparisons.
        raise SignatureError("signature header is too large")

    _verify_timestamp(timestamp, now)

    key = _key(secret)
    signed = f"{delivery_id}.{timestamp}.".encode("utf-8") + bytes(body)
    expected = hmac.new(key, signed, hashlib.sha256).digest()

    matched = False
    for candidate in presented.split(" "):
        version, _, value = candidate.partition(",")
        if version != "v1" or not value:
            continue
        try:
            supplied = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            continue
        # Every candidate is compared, and the loop is not short-circuited on a
        # match: the work done is the same whether the first signature matched
        # or the last one did.
        if hmac.compare_digest(expected, supplied):
            matched = True
    if not matched:
        raise SignatureError("no matching signature")
    return delivery_id


def _verify_timestamp(raw, now):
    try:
        timestamp = float(raw)
    except (TypeError, ValueError):
        raise SignatureError("invalid webhook timestamp") from None
    if timestamp != timestamp or timestamp in (float("inf"), float("-inf")):
        raise SignatureError("invalid webhook timestamp")
    if timestamp < now - TOLERANCE_SECONDS:
        raise SignatureError("webhook timestamp is too old")
    if timestamp > now + TOLERANCE_SECONDS:
        raise SignatureError("webhook timestamp is too new")


def _header(headers, name):
    try:
        value = headers.get(name)
    except AttributeError:
        raise SignatureError("headers are not readable") from None
    if value is None:
        # Not every mapping is case-insensitive; Starlette's is, a plain dict
        # in a test is not.
        for key, candidate in getattr(headers, "items", lambda: ())():
            if isinstance(key, str) and key.lower() == name:
                value = candidate
                break
    if value is None:
        return None
    if not isinstance(value, str):
        raise SignatureError("webhook header is not text")
    return value
