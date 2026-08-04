"""Request validation for the public API.

Deliberately hand-rolled against the standard library: the project has no
schema framework, and adding one for this release would change the hash-locked
dependency set for convenience rather than necessity.
"""
from __future__ import annotations

from datetime import datetime, timezone

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class ValidationError(Exception):
    """Raised when a request body or query string fails validation."""

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field

    def as_dict(self) -> dict:
        payload = {"status": "invalid_request", "detail": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


def require_mapping(value, *, field="body") -> dict:
    if not isinstance(value, dict):
        raise ValidationError("expected a JSON object", field=field)
    return value


def require_str(body: dict, field: str, *, max_length: int = 255, allow_empty: bool = False) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be a string", field=field)
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f"'{field}' must not be empty", field=field)
    if len(value) > max_length:
        raise ValidationError(f"'{field}' exceeds {max_length} characters", field=field)
    return value


def optional_str(body: dict, field: str, *, max_length: int = 255) -> str | None:
    if body.get(field) is None:
        return None
    return require_str(body, field, max_length=max_length)


def require_choice(body: dict, field: str, allowed: set[str]) -> str:
    value = require_str(body, field)
    if value not in allowed:
        raise ValidationError(
            f"'{field}' must be one of {sorted(allowed)}", field=field
        )
    return value


def optional_choice(body: dict, field: str, allowed: set[str]) -> str | None:
    if body.get(field) is None:
        return None
    return require_choice(body, field, allowed)


def require_object(body: dict, field: str) -> dict:
    value = body.get(field)
    if not isinstance(value, dict):
        raise ValidationError(f"'{field}' must be a JSON object", field=field)
    return value


def optional_object(body: dict, field: str) -> dict:
    if body.get(field) is None:
        return {}
    return require_object(body, field)


def optional_list(body: dict, field: str) -> list:
    value = body.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"'{field}' must be an array", field=field)
    return value


def require_timestamp(body: dict, field: str) -> datetime:
    raw = body.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"'{field}' must be an ISO-8601 timestamp", field=field)
    return parse_timestamp(raw, field=field)


def optional_timestamp(body: dict, field: str) -> datetime | None:
    if body.get(field) is None:
        return None
    return require_timestamp(body, field)


def parse_timestamp(raw: str, *, field: str = "timestamp") -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"'{field}' must be an ISO-8601 timestamp", field=field
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def require_idempotency_key(body: dict, headers) -> str:
    """Externally retried writes must carry a stable event identity."""
    header_value = headers.get("Idempotency-Key") if headers is not None else None
    value = header_value if isinstance(header_value, str) and header_value.strip() else body.get("idempotency_key")
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "an Idempotency-Key header or 'idempotency_key' field is required",
            field="idempotency_key",
        )
    value = value.strip()
    if len(value) > 255:
        raise ValidationError("idempotency key exceeds 255 characters", field="idempotency_key")
    return value


def pagination(query_params) -> tuple[int, int]:
    """Return a validated ``(limit, offset)`` pair with a bounded page size."""
    limit_raw = query_params.get("limit")
    offset_raw = query_params.get("offset")

    limit = DEFAULT_PAGE_SIZE
    if limit_raw is not None:
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise ValidationError("'limit' must be an integer", field="limit") from None
        if limit < 1:
            raise ValidationError("'limit' must be at least 1", field="limit")
        if limit > MAX_PAGE_SIZE:
            raise ValidationError(
                f"'limit' must not exceed {MAX_PAGE_SIZE}", field="limit"
            )

    offset = 0
    if offset_raw is not None:
        try:
            offset = int(offset_raw)
        except (TypeError, ValueError):
            raise ValidationError("'offset' must be an integer", field="offset") from None
        if offset < 0:
            raise ValidationError("'offset' must not be negative", field="offset")
    return limit, offset


def isoformat(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)
