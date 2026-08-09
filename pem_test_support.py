"""A real RSA private key, for tests that need one.

Startup now loads the App private key rather than pattern-matching it, so a
placeholder string is correctly rejected. Tests that used `"test-private-key"`
were only ever passing because nothing checked; using genuine key material
means they exercise the same path production does.

Generated once per process — RSA key generation is slow enough that doing it
per test is noticeable across a suite this size.
"""
from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def test_private_key_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def write_test_private_key(path) -> str:
    """Write a usable PEM to ``path`` and return the path as text."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(test_private_key_pem())
    return str(target)
