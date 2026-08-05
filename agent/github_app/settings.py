import math
import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class SettingsError(ValueError):
    """Raised when server configuration is missing or unsafe."""


@dataclass(frozen=True)
class GitHubAppSettings:
    app_id: int
    webhook_secret: str = field(repr=False)
    private_key_path: Path = field(repr=False)
    private_key: bytes = field(repr=False)
    storage_root: Path
    worker_count: int = 2
    queue_capacity: int = 100
    max_retries: int = 3
    retry_base_seconds: float = 1.0
    host: str = "0.0.0.0"
    port: int = 8000
    max_body_bytes: int = 2 * 1024 * 1024
    request_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 10.0
    slack_webhook_url: str | None = field(default=None, repr=False)
    slack_notify_warn: bool = False
    slack_max_retries: int = 2
    slack_retry_base_seconds: float = 1.0
    # Public lifecycle/dashboard API. Absent DSN leaves the API unregistered.
    database_url: str | None = field(default=None, repr=False)
    # Metadata review defaults ON whenever a database is configured. A
    # deployment that wants filesystem-only review must say so explicitly
    # rather than get it by accident.
    metadata_review_enabled: bool = True
    metadata_review_environment: str = "production"
    api_pool_size: int = 5


def load_settings(environ: Mapping[str, str] | None = None) -> GitHubAppSettings:
    values = os.environ if environ is None else environ
    app_id = _integer(values, "RELIUM_GITHUB_APP_ID", minimum=1)
    webhook_secret = _required(values, "RELIUM_GITHUB_WEBHOOK_SECRET")
    private_key_path = _resolved_path(
        _required(values, "RELIUM_GITHUB_PRIVATE_KEY_PATH"),
        "RELIUM_GITHUB_PRIVATE_KEY_PATH",
    )
    try:
        private_key = private_key_path.read_bytes()
    except OSError:
        raise SettingsError(
            "RELIUM_GITHUB_PRIVATE_KEY_PATH must name a readable file."
        ) from None
    if not private_key:
        raise SettingsError("RELIUM_GITHUB_PRIVATE_KEY_PATH must not be empty.")

    storage_root = _resolved_path(
        _required(values, "RELIUM_STORAGE_ROOT"), "RELIUM_STORAGE_ROOT"
    )
    if storage_root == Path(storage_root.anchor) or (
        storage_root.exists() and not storage_root.is_dir()
    ):
        raise SettingsError("RELIUM_STORAGE_ROOT must be a safe directory path.")

    host = values.get("RELIUM_HOST", "0.0.0.0")
    if not isinstance(host, str) or not host.strip():
        raise SettingsError("RELIUM_HOST must be non-empty text.")

    return GitHubAppSettings(
        app_id=app_id,
        webhook_secret=webhook_secret,
        private_key_path=private_key_path,
        private_key=private_key,
        storage_root=storage_root,
        worker_count=_integer(values, "RELIUM_WORKER_COUNT", default="2", minimum=1),
        queue_capacity=_integer(
            values, "RELIUM_QUEUE_CAPACITY", default="100", minimum=1
        ),
        max_retries=_integer(values, "RELIUM_MAX_RETRIES", default="3", minimum=0),
        retry_base_seconds=_number(
            values, "RELIUM_RETRY_BASE_SECONDS", default="1", minimum_exclusive=0
        ),
        host=host.strip(),
        port=_integer(
            values, "RELIUM_PORT", default="8000", minimum=1, maximum=65535
        ),
        max_body_bytes=_integer(
            values,
            "RELIUM_MAX_BODY_BYTES",
            default=str(2 * 1024 * 1024),
            minimum=1,
        ),
        slack_webhook_url=_slack_webhook_url(values),
        slack_notify_warn=_boolean(
            values, "RELIUM_SLACK_NOTIFY_WARN", default="false"
        ),
        slack_max_retries=_integer(
            values,
            "RELIUM_SLACK_MAX_RETRIES",
            default="2",
            minimum=0,
            maximum=5,
        ),
        slack_retry_base_seconds=_number(
            values,
            "RELIUM_SLACK_RETRY_BASE_SECONDS",
            default="1",
            minimum_exclusive=0,
            maximum=10,
        ),
        database_url=_database_url(values),
        metadata_review_enabled=_metadata_review_enabled(values),
        metadata_review_environment=values.get(
            "RELIUM_METADATA_REVIEW_ENVIRONMENT", "production"),
        api_pool_size=_integer(
            values, "RELIUM_API_POOL_SIZE", default="5", minimum=1, maximum=50
        ),
    )


def _metadata_review_enabled(values: Mapping[str, str]) -> bool:
    """Metadata review follows the database unless explicitly disabled.

    Returning True with no database is intentional: build_review_lifecycle
    then fails loudly instead of starting in a degraded mode that looks
    healthy.
    """
    raw = values.get("RELIUM_METADATA_REVIEW_ENABLED")
    if raw is None:
        return bool(_database_url(values))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _database_url(values: Mapping[str, str]) -> str | None:
    value = values.get("RELIUM_DATABASE_URL")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError("RELIUM_DATABASE_URL must be non-empty text when set.")
    value = value.strip()
    if not value.startswith(("postgresql://", "postgres://")):
        raise SettingsError(
            "RELIUM_DATABASE_URL must be a PostgreSQL DSN; the public API has no "
            "SQLite or in-memory fallback."
        )
    return value


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{name} is required.")
    return value


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{name} must be text when configured.")
    return value.strip() or None


def _slack_webhook_url(values: Mapping[str, str]) -> str | None:
    name = "RELIUM_SLACK_WEBHOOK_URL"
    value = _optional(values, name)
    if value is None:
        return None
    try:
        parsed = urllib.parse.urlparse(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and parsed.path.startswith("/services/")
            and bool(parsed.path.removeprefix("/services/").strip("/"))
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SettingsError(f"{name} must be a valid Slack HTTPS webhook URL.")
    return value


def _boolean(values: Mapping[str, str], name: str, *, default: str) -> bool:
    raw = values.get(name, default)
    if not isinstance(raw, str) or raw.strip().lower() not in {"true", "false"}:
        raise SettingsError(f"{name} must be true or false.")
    return raw.strip().lower() == "true"


def _integer(
    values: Mapping[str, str],
    name: str,
    *,
    default: str | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = values.get(name, default)
    try:
        if isinstance(raw, bool) or raw is None or str(int(raw)) != str(raw).strip():
            raise ValueError
        value = int(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a valid integer.") from None
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name} is outside the allowed range.")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} is outside the allowed range.")
    return value


def _number(
    values: Mapping[str, str],
    name: str,
    *,
    default: str,
    minimum_exclusive: float,
    maximum: float | None = None,
) -> float:
    raw = values.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a valid number.") from None
    if not math.isfinite(value) or value <= minimum_exclusive:
        raise SettingsError(f"{name} is outside the allowed range.")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} is outside the allowed range.")
    return value


def _resolved_path(value: str, name: str) -> Path:
    try:
        return Path(value).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise SettingsError(f"{name} must be a valid path.") from None
