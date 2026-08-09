"""Collector configuration.

Everything sensitive is read from the environment and never rendered. The
dataclass overrides __repr__ so an accidental log, traceback or debugger frame
cannot spill the API token or the warehouse DSN.

Network behaviour is deliberately boring: TLS verification is on, there is no
setting that turns it off, and proxies are taken from the standard
HTTP_PROXY / HTTPS_PROXY / NO_PROXY variables that urllib already honours. A
customer with a corporate CA points RELIUM_API_CA_BUNDLE at it rather than
disabling verification.
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


def _redact_dsn(dsn: str) -> str:
    """host/database only - never user, password or query parameters."""
    text = str(dsn or "")
    if "://" not in text:
        return "<redacted>"
    _, _, remainder = text.partition("://")
    remainder = remainder.rpartition("@")[2]          # drop any credentials
    hostpart, _, database = remainder.partition("/")
    database = database.split("?")[0]
    return f"{hostpart}/{database}" if database else hostpart


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
    ca_bundle: str | None = None

    def __repr__(self) -> str:
        # Never let a repr carry the token or the DSN, in any context.
        return (f"CollectorConfig(api_url={self.api_url!r}, "
                f"environment={self.environment!r}, "
                f"collector_id={self.collector_id!r}, "
                f"warehouse={self.safe_warehouse!r}, "
                f"api_token=<redacted>, warehouse_dsn=<redacted>)")

    __str__ = __repr__

    @property
    def safe_warehouse(self) -> str:
        """Host and database only. Safe to log; a DSN never is."""
        return _redact_dsn(self.warehouse_dsn)

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

        api_url = str(api_url).rstrip("/")
        if api_url.startswith("http://") and not _is_loopback(api_url):
            raise CollectorConfigError(
                "RELIUM_API_URL must use https for a non-loopback host; the "
                "collector will not send a bearer token over plaintext")

        ca_bundle = overrides.get("ca_bundle") or env.get("RELIUM_API_CA_BUNDLE")
        if ca_bundle and not os.path.isfile(ca_bundle):
            raise CollectorConfigError(
                f"RELIUM_API_CA_BUNDLE does not name a readable file: {ca_bundle}")

        return cls(
            api_url=api_url,
            api_token=str(api_token),
            warehouse_dsn=str(dsn),
            environment=(overrides.get("environment")
                         or env.get("RELIUM_ENVIRONMENT") or DEFAULT_ENVIRONMENT),
            collector_id=(overrides.get("collector_id")
                          or env.get("RELIUM_COLLECTOR_ID") or "relium-collector"),
            timeout_seconds=float(overrides.get("timeout_seconds")
                                  or env.get("RELIUM_API_TIMEOUT_SECONDS")
                                  or DEFAULT_TIMEOUT_SECONDS),
            statement_timeout_ms=int(overrides.get("statement_timeout_ms")
                                     or env.get("RELIUM_STATEMENT_TIMEOUT_MS")
                                     or DEFAULT_STATEMENT_TIMEOUT_MS),
            ca_bundle=ca_bundle or None,
        )


def _is_loopback(url: str) -> bool:
    host = url.partition("://")[2].partition("/")[0].partition(":")[0]
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}
