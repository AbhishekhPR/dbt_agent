"""Collector configuration.

Everything sensitive is read from the environment and never rendered. The
dataclass deliberately overrides __repr__ so an accidental log, traceback or
debugger frame cannot spill the API token or the warehouse DSN.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_ENVIRONMENT = "production"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000

COLLECTOR_VERSION = "relium-collector/0.1.0"


class CollectorConfigError(RuntimeError):
    """Configuration is absent or unusable. Never contains a secret value."""


@dataclass(frozen=True)
class CollectorConfig:
    api_url: str
    api_token: str = field(repr=False)
    warehouse_dsn: str = field(repr=False)
    environment: str = DEFAULT_ENVIRONMENT
    collector_id: str = "relium-collector"
    adapter_type: str = "postgres"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Never let a repr carry the token or the DSN, in any context.
        return (f"CollectorConfig(api_url={self.api_url!r}, "
                f"environment={self.environment!r}, "
                f"collector_id={self.collector_id!r}, "
                f"api_token=<redacted>, warehouse_dsn=<redacted>)")

    __str__ = __repr__

    @classmethod
    def from_env(cls, env=None, **overrides):
        env = os.environ if env is None else env
        api_url = overrides.get("api_url") or env.get("RELIUM_API_URL")
        api_token = overrides.get("api_token") or env.get("RELIUM_API_TOKEN")
        dsn = overrides.get("warehouse_dsn") or env.get("RELIUM_WAREHOUSE_DSN")

        missing = [name for name, value in (
            ("RELIUM_API_URL", api_url),
            ("RELIUM_API_TOKEN", api_token),
            ("RELIUM_WAREHOUSE_DSN", dsn),
        ) if not value]
        if missing:
            raise CollectorConfigError(
                "missing required configuration: " + ", ".join(missing))

        return cls(
            api_url=str(api_url).rstrip("/"),
            api_token=str(api_token),
            warehouse_dsn=str(dsn),
            environment=(overrides.get("environment")
                         or env.get("RELIUM_ENVIRONMENT") or DEFAULT_ENVIRONMENT),
            collector_id=(overrides.get("collector_id")
                          or env.get("RELIUM_COLLECTOR_ID") or "relium-collector"),
            timeout_seconds=float(overrides.get("timeout_seconds")
                                  or env.get("RELIUM_COLLECTOR_TIMEOUT_SECONDS")
                                  or DEFAULT_TIMEOUT_SECONDS),
        )
