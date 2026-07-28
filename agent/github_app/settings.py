import math
import os
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
    )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{name} is required.")
    return value


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
) -> float:
    raw = values.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a valid number.") from None
    if not math.isfinite(value) or value <= minimum_exclusive:
        raise SettingsError(f"{name} is outside the allowed range.")
    return value


def _resolved_path(value: str, name: str) -> Path:
    try:
        return Path(value).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise SettingsError(f"{name} must be a valid path.") from None
