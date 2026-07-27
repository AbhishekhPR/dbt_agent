import re
from enum import Enum
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
)
_SENSITIVE_EXACT_KEYS = {
    "compiled_code",
    "environment",
    "env",
    "query",
    "raw_code",
    "sql",
}

_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:api[_-]?key|authorization|credential|password|passwd|"
    r"private[_-]?key|secret|token)\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    flags=re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", flags=re.IGNORECASE)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b"
)


def redact_text(value: Any) -> str:
    text = str(value)
    text = _ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        text,
    )
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    return _KNOWN_TOKEN_RE.sub(REDACTED, text)


def redact_sensitive_data(value: Any, *, key: Any = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            item_key: redact_sensitive_data(item, key=item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_EXACT_KEYS
        or any(part in normalized for part in _SENSITIVE_KEY_PARTS)
    )
