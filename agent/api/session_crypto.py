"""Application-level encryption for the GitHub credentials a session holds.

The dashboard session needs a GitHub user credential so repository permission
can be re-verified later. That credential is the user's, not the App's, and it
must survive in the database without being readable from it — a database dump,
a replica, or a backup restored onto a laptop must not yield working GitHub
tokens.

AES-256-GCM, one random 96-bit nonce per encryption, with the session id bound
in as associated data so a ciphertext lifted from one session row cannot be
pasted into another.

The key comes from configuration and never from the database. Losing it makes
stored credentials undecryptable, which invalidates sessions and forces
re-authentication — the correct failure, and the reason nothing else depends
on it.
"""
from __future__ import annotations

import base64
import os

NONCE_BYTES = 12
KEY_BYTES = 32


class CredentialEncryptionError(Exception):
    """The key is unusable, or a stored credential could not be decrypted."""


def load_key(raw: str | bytes | None) -> bytes:
    """Decode a base64 32-byte key. Anything else is refused at startup."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise CredentialEncryptionError(
            "RELIUM_SESSION_ENCRYPTION_KEY is required to store GitHub session "
            "credentials. Generate one with: "
            "python -c \"import base64,os;print(base64.b64encode(os.urandom(32)).decode())\""
        )
    text = raw.decode("ascii") if isinstance(raw, bytes) else raw
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CredentialEncryptionError(
            "RELIUM_SESSION_ENCRYPTION_KEY must be base64.") from exc
    if len(key) != KEY_BYTES:
        raise CredentialEncryptionError(
            f"RELIUM_SESSION_ENCRYPTION_KEY must decode to {KEY_BYTES} bytes, "
            f"got {len(key)}.")
    return key


def generate_key() -> str:
    """A fresh base64 key, for operators and for tests."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def _cipher(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise CredentialEncryptionError(
            "storing GitHub session credentials requires the cryptography "
            "package") from exc
    return AESGCM(key)


def encrypt(key: bytes, plaintext: str | None, *, associated: str) -> bytes | None:
    """Encrypt one credential. ``None`` stays ``None`` — absence is not a secret."""
    if plaintext is None:
        return None
    nonce = os.urandom(NONCE_BYTES)
    sealed = _cipher(key).encrypt(
        nonce, plaintext.encode("utf-8"), associated.encode("utf-8"))
    return nonce + sealed


def decrypt(key: bytes, stored: bytes | memoryview | None, *, associated: str) -> str | None:
    """Decrypt one credential, or fail closed.

    A failure here is never recoverable by guessing: the caller invalidates the
    session and makes the user authenticate again.
    """
    if stored is None:
        return None
    raw = bytes(stored)
    if len(raw) <= NONCE_BYTES:
        raise CredentialEncryptionError("stored credential is truncated")
    nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    try:
        opened = _cipher(key).decrypt(nonce, sealed, associated.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        # Deliberately opaque: the reason a credential will not open is not
        # something an error message should help anyone narrow down.
        raise CredentialEncryptionError("stored credential could not be decrypted") from exc
    return opened.decode("utf-8")
