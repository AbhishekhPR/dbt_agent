import base64
import json
import time


class AuthenticationError(ValueError):
    """Raised when GitHub App credentials or token responses are invalid."""


def create_app_jwt(app_id, private_key_pem, *, now=None, signer=None) -> str:
    if isinstance(app_id, bool) or not str(app_id).isdigit() or int(app_id) <= 0:
        raise AuthenticationError("GitHub App id must be a positive integer.")
    if not isinstance(private_key_pem, (str, bytes)) or not private_key_pem:
        raise AuthenticationError("GitHub App private key is required.")
    issued_at = int(time.time() if now is None else now) - 60
    header = _encode_json({"alg": "RS256", "typ": "JWT"})
    payload = _encode_json({"iat": issued_at, "exp": issued_at + 600, "iss": str(app_id)})
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = (signer or _rsa_sha256_sign)(private_key_pem, signing_input)
    return f"{header}.{payload}.{_b64url(signature)}"


def get_installation_token(client, installation_id: int, app_jwt: str) -> str:
    if isinstance(installation_id, bool) or not isinstance(installation_id, int) or installation_id <= 0:
        raise AuthenticationError("Installation id must be a positive integer.")
    response = client.create_installation_access_token(installation_id, app_jwt)
    token = response.get("token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise AuthenticationError("GitHub did not return an installation token.")
    return token


def _encode_json(value) -> str:
    return _b64url(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _rsa_sha256_sign(private_key_pem, message: bytes) -> bytes:
    """Sign through cryptography when available; kept injectable for unit tests."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise AuthenticationError(
            "RS256 signing requires the optional cryptography package or an injected signer."
        ) from exc
    key_bytes = private_key_pem.encode("utf-8") if isinstance(private_key_pem, str) else private_key_pem
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("GitHub App private key is invalid.") from exc
