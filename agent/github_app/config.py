import copy
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from agent.evidence_policy import EvidencePolicyVersion, default_policy


DEFAULT_MANIFEST_PATH = "target/manifest.json"
_ALLOWED_KEYS = frozenset(
    {
        "version",
        "enabled",
        "manifest_path",
        "mode",
        "enforcement_mode",
        "evidence_policy",
    }
)


class RepositoryConfigError(ValueError):
    """Raised when repository-owned GitHub App configuration is unsafe or invalid."""


class _ConfigLoader(yaml.SafeLoader):
    pass


_ConfigLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first_character, resolvers in _ConfigLoader.yaml_implicit_resolvers.items():
    _ConfigLoader.yaml_implicit_resolvers[first_character] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
_ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


@dataclass(frozen=True)
class RepositoryConfig:
    version: int = 1
    enabled: bool = True
    manifest_path: str = DEFAULT_MANIFEST_PATH
    # Deprecated compatibility field. It no longer controls GitHub checks;
    # enforcement_mode is authoritative and defaults to shadow.
    mode: str = "warn"
    enforcement_mode: str = "shadow"
    evidence_policy: EvidencePolicyVersion = field(default_factory=default_policy)


def load_repository_config(content) -> RepositoryConfig:
    if content is None:
        return RepositoryConfig()
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryConfigError("Repository config must be UTF-8 text.") from exc
    if not isinstance(content, str):
        raise RepositoryConfigError("Repository config must be text.")
    # Backslashes are never valid in repository paths. Detect them before YAML
    # parsing so a Windows path in a quoted scalar gets a field-specific error
    # instead of being interpreted as YAML escape sequences.
    for line in content.splitlines():
        if re.match(r"^\s*manifest_path\s*:", line) and "\\" in line:
            raise RepositoryConfigError(
                "Repository config manifest_path must be a repository-relative POSIX path."
            )
    try:
        values = yaml.load(content, Loader=_ConfigLoader)
    except yaml.YAMLError as exc:
        raise RepositoryConfigError("Repository config must contain valid YAML.") from exc
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise RepositoryConfigError("Repository config must be an object.")
    unknown = sorted(set(values) - _ALLOWED_KEYS)
    if unknown:
        raise RepositoryConfigError(f"Unknown repository config field: {unknown[0]}")
    version = values.get("version", 1)
    if isinstance(version, bool) or version != 1:
        raise RepositoryConfigError("Repository config version must be 1.")
    enabled = values.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RepositoryConfigError("Repository config enabled must be true or false.")
    manifest_path = validate_repository_relative_path(
        values.get("manifest_path", DEFAULT_MANIFEST_PATH), field_name="manifest_path"
    )
    mode = values.get("mode", "warn")
    if mode not in {"warn", "block"}:
        raise RepositoryConfigError("Repository config mode must be warn or block.")
    enforcement_mode = values.get("enforcement_mode", "shadow")
    if enforcement_mode not in {"shadow", "enforce"}:
        raise RepositoryConfigError(
            "Repository config enforcement_mode must be shadow or enforce."
        )
    try:
        if values.get("evidence_policy") is None:
            evidence_policy = default_policy()
        else:
            policy_payload = values["evidence_policy"]
            parsed_policy = EvidencePolicyVersion.from_mapping(policy_payload)
            requirements = dict(default_policy().requirements)
            requirements.update(parsed_policy.requirements)
            evidence_policy = EvidencePolicyVersion.create(
                parsed_policy.version, requirements
            )
    except (TypeError, ValueError) as exc:
        raise RepositoryConfigError(str(exc)) from exc
    return RepositoryConfig(
        version=version,
        enabled=enabled,
        manifest_path=manifest_path,
        mode=mode,
        enforcement_mode=enforcement_mode,
        evidence_policy=evidence_policy,
    )


def validate_repository_relative_path(value, *, field_name="path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepositoryConfigError(f"Repository config {field_name} must be a path.")
    if "\\" in value or "\x00" in value:
        raise RepositoryConfigError(
            f"Repository config {field_name} must be a repository-relative POSIX path."
        )
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise RepositoryConfigError(
            f"Repository config {field_name} must remain within the repository."
        )
    return path.as_posix()


def resolve_repository_path(repository_root, configured_path: str) -> Path:
    relative_path = validate_repository_relative_path(configured_path, field_name="path")
    root = Path(repository_root).resolve()
    candidate = (root / Path(*PurePosixPath(relative_path).parts)).resolve()
    if not candidate.is_relative_to(root):
        raise RepositoryConfigError("Resolved path must remain within the repository.")
    return candidate
