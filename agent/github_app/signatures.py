import hashlib
import hmac
import re


_SHA256_SIGNATURE = re.compile(r"sha256=([0-9a-fA-F]{64})\Z")


class SignatureConfigurationError(ValueError):
    """Raised when webhook signature verification is not configured safely."""


def verify_webhook_signature(*, secret, body, signature_header) -> bool:
    secret_bytes = _secret_bytes(secret)
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("Webhook body must be bytes.")
    if not isinstance(signature_header, str):
        return False

    match = _SHA256_SIGNATURE.fullmatch(signature_header)
    if match is None:
        return False

    expected = hmac.new(secret_bytes, bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1).lower())


def _secret_bytes(secret) -> bytes:
    if isinstance(secret, str):
        encoded = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        encoded = secret
    else:
        raise SignatureConfigurationError("Webhook secret must be text or bytes.")
    if not encoded:
        raise SignatureConfigurationError("Webhook secret must not be empty.")
    return encoded
