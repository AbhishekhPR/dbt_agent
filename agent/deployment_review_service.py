import copy
from pathlib import Path
from typing import Any

from agent.dbt_changes import load_changed_models_from_manifest
from agent.dbt_context import extract_project_context_from_manifest
from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_lifecycle import review_deployment
from agent.deployment_outcomes import DeploymentOutcomeStore
from agent.presentation import render_cli, render_json, render_markdown


CONTRACT_VERSION = "1"


def review_manifest_change(
    *,
    manifest: dict,
    changed_files: list[str] | tuple[str, ...],
    changed_models: list[str] | tuple[str, ...] | None = None,
    deployment_id: str,
    history_path=None,
    outcomes_path=None,
    auto_record=False,
    allow_blocked_recording=False,
) -> dict[str, Any]:
    manifest_copy = copy.deepcopy(manifest)
    if not isinstance(manifest_copy, dict):
        raise ValueError("Manifest must be an object.")

    project_context = extract_project_context_from_manifest(manifest_copy)
    inferred_models = load_changed_models_from_manifest(
        manifest=manifest_copy,
        changed_files=list(changed_files or []),
    )
    resolved_models = _ordered_unique(
        [*list(changed_models or []), *inferred_models]
    )
    if not resolved_models:
        raise ValueError("At least one changed model is required.")

    return _review_project_context_change(
        project_context=project_context,
        changed_files=list(changed_files or []),
        changed_models=resolved_models,
        deployment_id=deployment_id,
        history_path=history_path,
        outcomes_path=outcomes_path,
        auto_record=auto_record,
        allow_blocked_recording=allow_blocked_recording,
        require_model_match=True,
    )


def _review_project_context_change(
    *,
    project_context: dict,
    changed_files: list[str] | tuple[str, ...],
    changed_models: list[str] | tuple[str, ...],
    deployment_id: str | None,
    history_path=None,
    outcomes_path=None,
    auto_record=False,
    allow_blocked_recording=False,
    require_model_match=False,
) -> dict[str, Any]:
    context_copy = copy.deepcopy(project_context or {})
    model_specs = _hydrate_model_specs(
        context_copy,
        list(changed_models or []),
        require_model_match=require_model_match,
    )
    sql_sources = [_sql_source_metadata(spec) for spec in model_specs]

    history_store = (
        DeploymentHistoryStore(history_path)
        if history_path is not None
        else None
    )
    options = _outcome_options(outcomes_path)
    review = review_deployment(
        changed_models=model_specs,
        project_context=context_copy,
        history_store=history_store,
        deployment_id=deployment_id,
        auto_record=auto_record,
        allow_blocked_recording=allow_blocked_recording,
        **options,
    )

    lifecycle = _deployment_lifecycle_metadata(review)
    incident = render_json(review.incident)
    cli_text = render_cli(review.incident)
    markdown = render_markdown(review.incident)
    cli_status = _deployment_review_status_lines(review, markdown=False)
    markdown_status = _deployment_review_status_lines(review, markdown=True)

    return {
        "version": CONTRACT_VERSION,
        "incident": incident,
        "decision": incident["decision"],
        "changed_files": _ordered_unique(list(changed_files or [])),
        "changed_models": [spec["name"] for spec in model_specs],
        "sql_sources": sql_sources,
        "deployment_lifecycle": lifecycle,
        "rendered": {
            "cli": f"{cli_text}\n\nDeployment History\n" + "\n".join(cli_status),
            "markdown": f"{markdown}\n\n## Deployment History\n" + "\n".join(markdown_status),
        },
    }


def _hydrate_model_specs(
    project_context: dict,
    changed_models: list[str],
    *,
    require_model_match: bool,
) -> list[dict]:
    models = list(project_context.get("models") or [])
    specs = []
    for changed_model in _ordered_unique(changed_models):
        matches = [
            model
            for model in models
            if str(model.get("name")) == changed_model
            or str(model.get("unique_id")) == changed_model
        ]
        if not matches:
            if require_model_match:
                raise ValueError(f"Changed model not found in manifest: {changed_model}")
            specs.append(
                {
                    "name": changed_model,
                    "model_name": changed_model,
                    "sql": None,
                    "sql_available": False,
                    "sql_source": "unavailable",
                }
            )
            continue
        if len(matches) > 1:
            raise ValueError(f"Changed model is ambiguous in manifest: {changed_model}")

        model = copy.deepcopy(matches[0])
        sql, sql_source = _select_sql(model)
        model_name = str(model.get("name") or changed_model)
        model["name"] = model_name
        model["model_name"] = model_name
        model["path"] = model.get("original_file_path") or model.get("path")
        model["sql"] = sql
        model["sql_available"] = sql is not None
        model["sql_source"] = sql_source
        specs.append(model)
    return specs


def _select_sql(model: dict) -> tuple[str | None, str]:
    for field_name in ("raw_code", "sql", "compiled_code"):
        value = model.get(field_name)
        if isinstance(value, str) and value.strip():
            return value, field_name
    return None, "unavailable"


def _sql_source_metadata(model_spec: dict) -> dict[str, Any]:
    available = bool(model_spec.get("sql_available"))
    return {
        "unique_id": model_spec.get("unique_id"),
        "name": model_spec.get("name"),
        "original_file_path": model_spec.get("original_file_path"),
        "path": model_spec.get("path"),
        "sql_available": available,
        "sql_source": model_spec.get("sql_source", "unavailable"),
        "ast_status": "evaluated" if available else "skipped",
    }


def _outcome_options(outcomes_path) -> dict[str, Any]:
    if outcomes_path is None or not Path(outcomes_path).exists():
        return {}
    outcomes = DeploymentOutcomeStore(outcomes_path).list_outcomes()
    return {"outcomes": outcomes} if outcomes else {}


def _deployment_lifecycle_metadata(review) -> dict[str, Any]:
    return {
        "previous_snapshot_loaded": bool(review.previous_snapshot_loaded),
        "previous_snapshot_id": _snapshot_id(review.previous_snapshot),
        "current_snapshot_id": _snapshot_id(review.current_snapshot),
        "saved_snapshot_id": review.saved_snapshot_id,
        "history_enabled": bool((review.metadata or {}).get("history_enabled")),
    }


def _deployment_review_status_lines(review, *, markdown: bool) -> list[str]:
    loaded = "YES" if review.previous_snapshot_loaded else "NO"
    previous_snapshot_id = _snapshot_id(review.previous_snapshot)
    if markdown:
        lines = [f"**Previous Snapshot Loaded:** {loaded}"]
        if review.previous_snapshot_loaded:
            lines.append(f"**Previous Snapshot:** {previous_snapshot_id or 'None'}")
        if review.saved_snapshot_id:
            lines.append(f"**Saved Snapshot:** {review.saved_snapshot_id}")
        return lines

    lines = [f"Previous Snapshot Loaded: {loaded}"]
    if review.previous_snapshot_loaded:
        lines.append(f"Previous Snapshot: {previous_snapshot_id or 'None'}")
    if review.saved_snapshot_id:
        lines.append(f"Saved Snapshot: {review.saved_snapshot_id}")
    return lines


def _snapshot_id(snapshot) -> str | None:
    if not snapshot:
        return None
    value = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else getattr(snapshot, "snapshot_id", None)
    return str(value) if value else None


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    return unique
