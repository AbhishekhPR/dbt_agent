import copy
import json
import re
from pathlib import Path
from typing import Any


def extract_project_context_from_manifest(manifest: dict) -> dict:
    manifest_copy = copy.deepcopy(manifest or {})
    if not isinstance(manifest_copy, dict):
        manifest_copy = {}

    id_names, id_types = _manifest_identity_maps(manifest_copy)

    models = _extract_models(manifest_copy, id_names, id_types)
    sources = _extract_sources(manifest_copy)
    metrics = _extract_metrics(manifest_copy, id_names, id_types)
    exposures = _extract_exposures(manifest_copy, id_names)
    semantic_models = _extract_semantic_models(manifest_copy, id_names, id_types)
    refs = _model_ref_edges(models)

    column_names = _unique_sorted(
        column
        for item in [*models, *sources]
        for column in item.get("columns", [])
    )
    file_paths = _unique_sorted(
        model.get("path")
        for model in models
        if model.get("path")
    )
    business_terms = _unique_sorted(_business_terms(
        models=models,
        sources=sources,
        metrics=metrics,
        exposures=exposures,
        semantic_models=semantic_models,
    ))

    return {
        "models": models,
        "model_names": [model["name"] for model in models],
        "column_names": column_names,
        "file_paths": file_paths,
        "sources": sources,
        "metrics": metrics,
        "dbt_metrics": copy.deepcopy(metrics),
        "exposures": exposures,
        "dashboard_names": [
            exposure["name"]
            for exposure in exposures
            if exposure.get("type") == "dashboard"
        ],
        "semantic_models": semantic_models,
        "refs": refs,
        "business_terms": business_terms,
        "metadata": _metadata(manifest_copy, models, sources, metrics, exposures, semantic_models),
    }


def load_manifest_from_path(path: str | Path) -> dict:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise ValueError(f"Manifest file not found: {path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid manifest JSON: {path}: {error}") from error
    except OSError as error:
        raise ValueError(f"Could not read manifest file: {path}: {error}") from error

    if not isinstance(manifest, dict):
        raise ValueError(f"Manifest JSON must be an object: {path}")

    return manifest


def load_project_context_from_manifest_path(path: str) -> dict:
    manifest = load_manifest_from_path(path)

    return extract_project_context_from_manifest(manifest)


def _extract_models(manifest: dict, id_names: dict[str, str], id_types: dict[str, str]) -> list[dict]:
    models = []
    for key, node in _resource_items(manifest.get("nodes"), "model"):
        unique_id = _unique_id(key, node)
        model_name = _name(node, unique_id)
        model = {
            "name": model_name,
            "unique_id": unique_id,
            "path": node.get("original_file_path") or node.get("path"),
            "columns": _columns(node),
            "refs": _model_refs(node, id_names, id_types),
            "sources": _model_sources(node, id_names, id_types),
            "description": str(node.get("description") or ""),
            "tags": _string_list(node.get("tags")),
            "materialized": _materialized(node),
        }
        for field_name in ("raw_code", "compiled_code", "sql", "original_file_path"):
            if node.get(field_name):
                model[field_name] = node.get(field_name)
        models.append(model)
    return sorted(models, key=lambda model: (model["name"], model["unique_id"]))


def _extract_sources(manifest: dict) -> list[dict]:
    sources = []
    for key, source in _resource_items(manifest.get("sources"), "source"):
        sources.append(
            {
                "name": _source_name(source, key),
                "source_name": source.get("source_name"),
                "table_name": source.get("table_name") or source.get("name"),
                "columns": _columns(source),
                "description": str(source.get("description") or ""),
            }
        )
    return sorted(sources, key=lambda source: source["name"])


def _extract_metrics(manifest: dict, id_names: dict[str, str], id_types: dict[str, str]) -> list[dict]:
    metric_items = [
        *_resource_items(manifest.get("metrics"), "metric"),
        *_resource_items(manifest.get("nodes"), "metric"),
    ]
    metrics = []
    seen = set()
    for key, metric in metric_items:
        unique_id = _unique_id(key, metric)
        if unique_id in seen:
            continue
        seen.add(unique_id)
        metrics.append(
            {
                "name": _name(metric, unique_id),
                "label": metric.get("label"),
                "type": metric.get("type"),
                "description": str(metric.get("description") or ""),
                "model": _metric_model(metric, id_names, id_types),
            }
        )
    return sorted(metrics, key=lambda metric: metric["name"])


def _extract_exposures(manifest: dict, id_names: dict[str, str]) -> list[dict]:
    exposure_items = [
        *_resource_items(manifest.get("exposures"), "exposure"),
        *_resource_items(manifest.get("nodes"), "exposure"),
    ]
    exposures = []
    seen = set()
    for key, exposure in exposure_items:
        unique_id = _unique_id(key, exposure)
        if unique_id in seen:
            continue
        seen.add(unique_id)
        exposures.append(
            {
                "name": _name(exposure, unique_id),
                "type": exposure.get("type"),
                "depends_on": _depends_on_names(exposure, id_names),
                "owner": copy.deepcopy(exposure.get("owner") or {}),
                "description": str(exposure.get("description") or ""),
            }
        )
    return sorted(exposures, key=lambda exposure: exposure["name"])


def _extract_semantic_models(manifest: dict, id_names: dict[str, str], id_types: dict[str, str]) -> list[dict]:
    semantic_models = []
    for key, semantic_model in _resource_items(manifest.get("semantic_models"), "semantic_model"):
        unique_id = _unique_id(key, semantic_model)
        models = _semantic_model_parents(semantic_model, id_names, id_types)
        semantic_models.append(
            {
                "name": _name(semantic_model, unique_id),
                "model": models[0] if models else None,
                "models": models,
                "description": str(semantic_model.get("description") or ""),
                "columns": _semantic_model_columns(semantic_model),
            }
        )
    return sorted(semantic_models, key=lambda semantic_model: semantic_model["name"])


def _model_ref_edges(models: list[dict]) -> list[dict]:
    edges = []
    for model in models:
        for parent in model.get("refs", []):
            edges.append(
                {
                    "parent": parent,
                    "child": model["name"],
                    "relationship": "ref",
                }
            )
    return sorted(edges, key=lambda edge: (edge["parent"], edge["child"], edge["relationship"]))


def _manifest_identity_maps(manifest: dict) -> tuple[dict[str, str], dict[str, str]]:
    id_names = {}
    id_types = {}

    for key, node in _resource_items(manifest.get("nodes")):
        unique_id = _unique_id(key, node)
        resource_type = str(node.get("resource_type") or _resource_type_from_id(unique_id))
        id_names[unique_id] = _name(node, unique_id)
        id_types[unique_id] = resource_type

    for key, source in _resource_items(manifest.get("sources")):
        unique_id = _unique_id(key, source)
        id_names[unique_id] = _source_name(source, key)
        id_types[unique_id] = "source"

    for key, metric in _resource_items(manifest.get("metrics")):
        unique_id = _unique_id(key, metric)
        id_names[unique_id] = _name(metric, unique_id)
        id_types[unique_id] = "metric"

    for key, exposure in _resource_items(manifest.get("exposures")):
        unique_id = _unique_id(key, exposure)
        id_names[unique_id] = _name(exposure, unique_id)
        id_types[unique_id] = "exposure"

    for key, semantic_model in _resource_items(manifest.get("semantic_models")):
        unique_id = _unique_id(key, semantic_model)
        id_names[unique_id] = _name(semantic_model, unique_id)
        id_types[unique_id] = "semantic_model"

    return id_names, id_types


def _resource_items(raw: Any, resource_type: str | None = None) -> list[tuple[str, dict]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = [
            (
                str(index),
                item,
            )
            for index, item in enumerate(raw if isinstance(raw, list) else [])
        ]

    resources = []
    for key, value in items:
        if not isinstance(value, dict):
            continue
        inferred_type = value.get("resource_type") or _resource_type_from_id(str(key))
        if resource_type is not None and inferred_type != resource_type:
            continue
        resources.append((str(key), value))
    return resources


def _unique_id(key: str, item: dict) -> str:
    return str(item.get("unique_id") or key)


def _name(item: dict, fallback: str) -> str:
    return str(item.get("name") or fallback.split(".")[-1])


def _source_name(source: dict, key: str) -> str:
    source_name = source.get("source_name")
    table_name = source.get("table_name") or source.get("name")
    if source_name and table_name:
        return f"{source_name}.{table_name}"
    return _name(source, key)


def _resource_type_from_id(unique_id: str) -> str | None:
    return str(unique_id).split(".", 1)[0] if "." in str(unique_id) else None


def _columns(item: dict) -> list[str]:
    raw_columns = item.get("columns") or {}
    if isinstance(raw_columns, dict):
        names = [
            str(value.get("name") or key)
            for key, value in raw_columns.items()
            if isinstance(value, dict)
        ]
        names.extend(
            str(key)
            for key, value in raw_columns.items()
            if not isinstance(value, dict)
        )
        return _unique_sorted(names)
    if isinstance(raw_columns, list):
        names = [
            str(column.get("name"))
            if isinstance(column, dict) and column.get("name")
            else str(column)
            for column in raw_columns
        ]
        return _unique_sorted(names)
    return []


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple, set)):
        return _unique_sorted(str(value) for value in raw)
    return [str(raw)]


def _materialized(node: dict) -> str | None:
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    return node.get("materialized") or config.get("materialized")


def _model_refs(node: dict, id_names: dict[str, str], id_types: dict[str, str]) -> list[str]:
    refs = []
    for dependency in _depends_on_nodes(node):
        if id_types.get(dependency) == "model":
            refs.append(id_names.get(dependency, dependency))
    refs.extend(_raw_refs(node.get("refs")))
    return _unique_sorted(refs)


def _model_sources(node: dict, id_names: dict[str, str], id_types: dict[str, str]) -> list[str]:
    sources = []
    for dependency in _depends_on_nodes(node):
        if id_types.get(dependency) == "source":
            sources.append(id_names.get(dependency, dependency))
    sources.extend(_raw_sources(node.get("sources")))
    return _unique_sorted(sources)


def _metric_model(metric: dict, id_names: dict[str, str], id_types: dict[str, str]) -> str | None:
    for dependency in _depends_on_nodes(metric):
        if id_types.get(dependency) == "model":
            return id_names.get(dependency, dependency)

    model = metric.get("model") or metric.get("parent")
    parsed = _parse_ref(model)
    if parsed:
        return parsed
    if isinstance(model, dict):
        return str(model.get("name") or model.get("model")) if (model.get("name") or model.get("model")) else None
    if model:
        return str(model)
    return None


def _semantic_model_parents(semantic_model: dict, id_names: dict[str, str], id_types: dict[str, str]) -> list[str]:
    parents = []
    for dependency in _depends_on_nodes(semantic_model):
        if id_types.get(dependency) == "model":
            parents.append(id_names.get(dependency, dependency))

    parsed = _parse_ref(semantic_model.get("model"))
    if parsed:
        parents.append(parsed)
    parents.extend(_string_list(semantic_model.get("models")))
    return _unique_sorted(parent for parent in parents if parent)


def _semantic_model_columns(semantic_model: dict) -> list[str]:
    names = []
    for field_name in ("dimensions", "entities", "measures"):
        for item in semantic_model.get(field_name) or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif item:
                names.append(str(item))
    return _unique_sorted(names)


def _depends_on_nodes(item: dict) -> list[str]:
    depends_on = item.get("depends_on") or {}
    if isinstance(depends_on, dict):
        return [str(node) for node in depends_on.get("nodes") or []]
    if isinstance(depends_on, list):
        return [str(node) for node in depends_on]
    return []


def _depends_on_names(item: dict, id_names: dict[str, str]) -> list[str]:
    names = []
    for dependency in _depends_on_nodes(item):
        names.append(id_names.get(dependency, dependency))
    return _ordered_unique(name for name in names if name)


def _raw_refs(raw_refs: Any) -> list[str]:
    refs = []
    for raw_ref in raw_refs or []:
        if isinstance(raw_ref, str):
            refs.append(raw_ref)
        elif isinstance(raw_ref, dict):
            value = raw_ref.get("name") or raw_ref.get("model")
            if value:
                refs.append(str(value))
        elif isinstance(raw_ref, (list, tuple)) and raw_ref:
            refs.append(str(raw_ref[-1]))
    return refs


def _raw_sources(raw_sources: Any) -> list[str]:
    sources = []
    for raw_source in raw_sources or []:
        if isinstance(raw_source, str):
            sources.append(raw_source)
        elif isinstance(raw_source, dict):
            source_name = raw_source.get("source_name") or raw_source.get("source")
            table_name = raw_source.get("table_name") or raw_source.get("name")
            if source_name and table_name:
                sources.append(f"{source_name}.{table_name}")
            elif table_name:
                sources.append(str(table_name))
        elif isinstance(raw_source, (list, tuple)):
            parts = [str(part) for part in raw_source if part]
            if len(parts) >= 2:
                sources.append(f"{parts[-2]}.{parts[-1]}")
            elif parts:
                sources.append(parts[0])
    return sources


def _parse_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"\s*ref\(['\"]([^'\"]+)['\"]\)\s*", value)
    if match:
        return match.group(1)
    return value.strip() or None


def _business_terms(**groups) -> list[str]:
    terms = []
    for items in groups.values():
        for item in items:
            description = item.get("description")
            if description:
                terms.append(str(description))
            terms.extend(item.get("tags") or [])
            label = item.get("label")
            if label:
                terms.append(str(label))
    return terms


def _metadata(
    manifest: dict,
    models: list[dict],
    sources: list[dict],
    metrics: list[dict],
    exposures: list[dict],
    semantic_models: list[dict],
) -> dict:
    manifest_metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    return {
        "source": "dbt_manifest",
        "project_name": manifest_metadata.get("project_name"),
        "dbt_version": manifest_metadata.get("dbt_version"),
        "model_count": len(models),
        "source_count": len(sources),
        "metric_count": len(metrics),
        "exposure_count": len(exposures),
        "semantic_model_count": len(semantic_models),
    }


def _unique_sorted(values) -> list[str]:
    return sorted({str(value) for value in values if value is not None and str(value)})


def _ordered_unique(values) -> list[str]:
    unique = []
    for value in values:
        text = str(value)
        if text and text not in unique:
            unique.append(text)
    return unique
