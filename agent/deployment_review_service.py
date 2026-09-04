import copy
from pathlib import Path
from typing import Any

from agent.dbt_changes import load_changed_models_from_manifest
from agent.dbt_context import extract_project_context_from_manifest
from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_lifecycle import review_deployment
from agent.deployment_outcomes import DeploymentOutcomeStore
from agent.presentation import render_cli, render_json, render_markdown
from agent.deployment_snapshot import create_deployment_snapshot
from agent.pr_analysis import compare_manifest_sql
from agent.semantic_context import build_semantic_context


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
    metadata_db_path=None,
    previous_manifest=None,
    manifest_source=None,
    base_sha=None,
    head_sha=None,
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

    analysis_metadata = {
        "manifest_source": copy.deepcopy(manifest_source or {"head": "unknown"}),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "semantic_comparison_evaluated": previous_manifest is not None,
        "semantic_comparison_status": "evaluated" if previous_manifest is not None else "unavailable",
    }
    previous_snapshot = _manifest_snapshot(
        previous_manifest,
        resolved_models,
        deployment_id,
        analysis_metadata,
    )
    manifest_comparison = compare_manifest_sql(
        previous_manifest,
        manifest_copy,
        resolved_models,
    )
    return _review_project_context_change(
        project_context=project_context,
        changed_files=list(changed_files or []),
        changed_models=resolved_models,
        deployment_id=deployment_id,
        history_path=history_path,
        outcomes_path=outcomes_path,
        auto_record=auto_record,
        allow_blocked_recording=allow_blocked_recording,
        metadata_db_path=metadata_db_path,
        metadata=analysis_metadata,
        previous_snapshot=previous_snapshot,
        manifest_comparison=manifest_comparison,
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
    metadata_db_path=None,
    metadata=None,
    previous_snapshot=None,
    manifest_comparison=None,
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
        metadata_db_path=metadata_db_path,
        metadata=metadata,
        previous_snapshot=previous_snapshot,
        manifest_comparison=manifest_comparison,
        **options,
    )

    lifecycle = _deployment_lifecycle_metadata(review)
    material_findings = _material_ast_findings(review.incident)
    health_explanation = _health_explanation(review.incident)
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
        "material_findings": material_findings,
        "health_explanation": health_explanation,
        "sql_sources": sql_sources,
        "semantic_comparison": {
            "evaluated": bool((metadata or {}).get("semantic_comparison_evaluated")),
            "status": (metadata or {}).get("semantic_comparison_status", "unknown"),
            "base_sha": (metadata or {}).get("base_sha"),
            "head_sha": (metadata or {}).get("head_sha"),
            "manifest_source": copy.deepcopy((metadata or {}).get("manifest_source", {})),
        },
        "deployment_lifecycle": lifecycle,
        "rendered": {
            "cli": f"{cli_text}\n\nDeployment History\n" + "\n".join(cli_status),
            "markdown": f"{markdown}\n\n## Deployment History\n" + "\n".join(markdown_status),
        },
    }


def _health_explanation(incident) -> dict:
    deductions = []
    for signal in incident.signals:
        if signal.score >= 0:
            continue
        reason = next((str(value).strip() for value in signal.reasons or []
                       if str(value).strip()), None)
        if not reason:
            reason = f"{signal.component} reduced code review health"
        deductions.append({
            "component": str(signal.component),
            "points": abs(int(signal.score)),
            "reason": reason,
        })
    return {
        "score": int(incident.health),
        "label": "Code review health",
        "basis": "static_code_and_manifest_analysis",
        "deductions": deductions,
    }


def _manifest_snapshot(manifest, changed_models, deployment_id, metadata):
    if manifest is None:
        return None
    context = extract_project_context_from_manifest(copy.deepcopy(manifest))
    semantic_context = build_semantic_context(
        project_context=context,
        changed_models=list(changed_models or []),
    )
    return create_deployment_snapshot(
        deployment_id=f"{deployment_id}:base",
        changed_models=list(changed_models or []),
        semantic_context=semantic_context,
        metadata={"source": "trusted_manifest", **copy.deepcopy(metadata or {})},
    ).to_dict()


def _material_ast_findings(incident, limit: int = 3) -> list[dict[str, str]]:
    findings = []
    seen = set()
    for signal in incident.signals:
        if signal.component != "ast" or signal.score >= 0:
            continue
        metadata = dict(signal.metadata or {})
        if metadata.get("ast_status") != "evaluated":
            continue
        model_name = str(metadata.get("model_name") or "unknown model")
        for bug in metadata.get("bugs") or []:
            if not isinstance(bug, dict):
                continue
            title = str(
                bug.get("category")
                or bug.get("description")
                or "SQL risk detected"
            )
            key = (model_name, str(bug.get("rule") or ""), title)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "rule": str(bug.get("rule") or ""),
                    "severity": str(bug.get("severity") or "low").lower(),
                    "title": title,
                    "impact": str(
                        bug.get("impact")
                        or bug.get("description")
                        or "This SQL pattern may produce incorrect results."
                    ),
                    "affected_model": model_name,
                    "recommended_fix": str(
                        bug.get("recommendation")
                        or bug.get("fix")
                        or "Review and correct the affected SQL."
                    ),
                }
            )
            if len(findings) >= limit:
                return findings
    return findings


def semantic_evidence_from_incident(incident) -> dict | None:
    """Return the SQL comparison already produced for this review.

    ``None`` means no comparison ran. An evaluated document with zero changes
    is deliberately preserved as a different state.
    """
    metadata = incident.get("metadata") if isinstance(incident, dict) else None
    comparison = (metadata or {}).get("manifest_comparison") or {}
    evidence = comparison.get("sql_semantic_comparison")
    if not isinstance(evidence, dict) or not evidence.get("models"):
        return None
    return evidence


def lifecycle_code_findings(result) -> list[dict]:
    """Project reviewed SQL risks into the durable lifecycle vocabulary."""
    severity_for = {
        "critical": "block",
        "high": "block",
        "medium": "warn",
        "low": "info",
    }
    findings = []
    for item in (result or {}).get("material_findings") or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "SQL_RISK")
        source_severity = str(item.get("severity") or "medium").lower()
        findings.append({
            "code": rule,
            "severity": severity_for.get(source_severity, "warn"),
            "category": "code",
            "message": str(item.get("impact") or item.get("title") or rule),
            "relation": item.get("affected_model"),
            "detail": {
                "title": str(item.get("title") or rule),
                "recommended_fix": str(item.get("recommended_fix") or ""),
                "source_severity": source_severity,
            },
        })
    return findings


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
