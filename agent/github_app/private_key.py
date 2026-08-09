"""Where the GitHub App private key comes from.

Two sources, one rule, one place.

    RELIUM_GITHUB_PRIVATE_KEY       the PEM itself, from a secret manager
    RELIUM_GITHUB_PRIVATE_KEY_PATH  a readable PEM file on disk

The inline value wins when it is set and non-empty. That is the documented
precedence and it is deliberately not "whichever exists": a platform that
injects the key as an environment variable is the production case, and a stale
path left in configuration must not quietly take priority over the secret the
operator actually set.

The key is never written to the repository or the workspace, and no temporary
file is materialised: `create_app_jwt` signs from the PEM bytes directly, so
there is nothing on disk to clean up. Nothing here logs the key, and no error
raised here contains key material — a malformed key is reported by shape, not
by content, because a configuration error message is one of the easiest places
for a secret to escape.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

INLINE_VAR = "RELIUM_GITHUB_PRIVATE_KEY"
PATH_VAR = "RELIUM_GITHUB_PRIVATE_KEY_PATH"

PEM_HEADER = b"-----BEGIN"


class PrivateKeyError(ValueError):
    """The App private key is absent, unreadable, or not usable key material."""


def _normalise(raw) -> bytes | None:
    if raw is None:
        return None
    value = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if not value.strip():
        return None
    # Platforms that carry multi-line secrets through a single-line field
    # deliver the PEM with literal backslash-n and no real newlines. Accepting
    # that shape is the difference between a working deploy and a signing
    # error nobody can explain. A value that already has real newlines is left
    # exactly as it is.
    if b"\n" not in value and b"\\n" in value:
        value = value.replace(b"\\r\\n", b"\n").replace(b"\\n", b"\n")
    return value


def resolve_private_key(values: Mapping[str, str], *, required: bool = True):
    """Return ``(pem_bytes, source)``, or ``(None, None)`` when not configured.

    ``source`` is the name of the variable the key came from, so startup can
    say where it looked without saying what it found.
    """
    inline = _normalise(values.get(INLINE_VAR))
    if inline is not None:
        _validate(inline, INLINE_VAR)
        return inline, INLINE_VAR

    configured_path = values.get(PATH_VAR)
    if isinstance(configured_path, str) and configured_path.strip():
        path = Path(configured_path.strip()).expanduser()
        try:
            pem = path.read_bytes()
        except OSError:
            # The path is named, not the reason: a permission error and a
            # missing file are equally "cannot read this".
            raise PrivateKeyError(
                f"{PATH_VAR} must name a readable file.") from None
        if not pem.strip():
            raise PrivateKeyError(f"{PATH_VAR} must not be empty.")
        _validate(pem, PATH_VAR)
        return pem, PATH_VAR

    if not required:
        return None, None
    raise PrivateKeyError(
        f"A GitHub App private key is required. Set {INLINE_VAR} to the PEM "
        f"value, or {PATH_VAR} to a readable PEM file.")


def _validate(pem: bytes, source: str) -> None:
    """Fail at startup on anything that will not sign later.

    Loading the key is the only honest check: a value can begin with a PEM
    header and still be truncated, wrongly encoded, or passphrase-protected,
    and each of those fails at the first webhook rather than at boot.
    """
    if PEM_HEADER not in pem:
        raise PrivateKeyError(
            f"{source} does not contain PEM key material "
            f"(no '-----BEGIN' header).")
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:  # pragma: no cover - dependency is pinned
        return
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except TypeError:
        raise PrivateKeyError(
            f"{source} is encrypted with a passphrase; Relium cannot sign with "
            f"a protected key. Provide an unencrypted PEM.") from None
    except ValueError:
        raise PrivateKeyError(
            f"{source} is not a valid PEM private key.") from None
    if not hasattr(key, "sign"):
        raise PrivateKeyError(f"{source} is not a signing key.")
