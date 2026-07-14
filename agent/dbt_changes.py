import copy
import json
from pathlib import Path


def load_changed_models_from_paths(*, manifest_path: str, changed_files: list[str]) -> list[str]:
    path = Path(manifest_path)
    if not path.is_file():
        raise ValueError(f"Manifest file not found: {manifest_path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid manifest JSON: {manifest_path}: {error}") from error

    return load_changed_models_from_manifest(
        manifest=manifest,
        changed_files=changed_files,
    )


def load_changed_models_from_manifest(*, manifest: dict, changed_files: list[str]) -> list[str]:
    manifest_copy = copy.deepcopy(manifest or {})
    if not isinstance(manifest_copy, dict):
        raise ValueError("Manifest must be an object.")

    model_entries = _model_entries(manifest_copy)
    changed_models = []
    for changed_file in list(changed_files or []):
        changed_path = _normalise_path(changed_file)
        for model_name, candidates in model_entries:
            if _path_matches(changed_path, candidates) and model_name not in changed_models:
                changed_models.append(model_name)
                break
    return changed_models


def _model_entries(manifest: dict) -> list[tuple[str, set[str]]]:
    entries = []
    for node in (manifest.get("nodes") or {}).values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        model_name = str(node.get("name") or "")
        if not model_name:
            continue
        candidates = set()
        for field_name in ("original_file_path", "path", "compiled_path", "build_path"):
            value = node.get(field_name)
            if value:
                candidates.add(_normalise_path(value))
        for candidate in list(candidates):
            if not candidate.startswith("models/"):
                candidates.add(f"models/{candidate}")
        entries.append((model_name, candidates))
    return entries


def _path_matches(changed_path: str, candidates: set[str]) -> bool:
    for candidate in candidates:
        if not candidate:
            continue
        if changed_path == candidate:
            return True
        if changed_path.endswith(f"/{candidate}"):
            return True
        if candidate.endswith(f"/{changed_path}"):
            return True
    return False


def _normalise_path(value: str) -> str:
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/").casefold()
