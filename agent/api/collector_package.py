"""Location and immutable names for the production-built collector package."""
from __future__ import annotations

from pathlib import Path

COLLECTOR_VERSION = "0.1.0"
COLLECTOR_WHEEL_FILENAME = f"relium-{COLLECTOR_VERSION}-py3-none-any.whl"
COLLECTOR_BUNDLE_FILENAME = f"relium-collector-{COLLECTOR_VERSION}.zip"
DEFAULT_COLLECTOR_PACKAGE_PATH = (
    Path("/app/artifacts") / COLLECTOR_BUNDLE_FILENAME
)


class CollectorPackageUnavailable(Exception):
    """The running image does not contain the release artifact it must serve."""


def resolve_collector_package(path=None) -> Path:
    """Return the immutable image artifact, or fail closed if it is absent."""
    candidate = Path(path) if path is not None else DEFAULT_COLLECTOR_PACKAGE_PATH
    if not candidate.is_file():
        raise CollectorPackageUnavailable(
            f"collector package is absent from this build: {candidate.name}")
    return candidate
