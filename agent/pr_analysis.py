import copy
from pathlib import Path
from typing import Any

from agent.ast_analyzer import run_ast_analysis
from agent.ast_analyzer import to_signal as ast_to_signal
from agent.blast_radius import calculate_blast_radius
from agent.blast_radius import to_signal as blast_radius_to_signal
from agent.business_metrics import calculate_operational_metrics
from agent.business_metrics import evaluate_metric_reliability
from agent.business_metrics import to_signal as business_metrics_to_signal
from agent.decision_assembly import assemble_decision_incident
from agent.deployment_snapshot import create_deployment_snapshot
from agent.historical_reliability import evaluate_history
from agent.historical_reliability import to_signal as historical_reliability_to_signal
from agent.incident import Incident
from agent.metadata_checks import run_metadata_checks
from agent.metadata_checks import to_signal as metadata_checks_to_signal
from agent.metadata_drift import compare_last_run
from agent.metadata_drift import to_signal as metadata_drift_to_signal
from agent.semantic_context import build_semantic_context
from agent.semantic_contract_validation import to_signal as semantic_contract_to_signal
from agent.semantic_diff import compare_semantic_snapshots
from agent.semantic_diff import to_signal as semantic_diff_to_signal
from agent.semantic_kpi_inference import to_signal as kpi_impact_to_signal
from agent.signals import Signal


def analyze_pr_with_history(
    *,
    changed_models,
    project_context,
    history_store=None,
    deployment_id=None,
    metadata=None,
    events=None,
    **existing_options,
) -> Incident:
    history_enabled = history_store is not None
    previous_snapshot = None

    if history_store is not None:
        loaded_snapshot = history_store.load_latest_snapshot()
        if loaded_snapshot is not None:
            previous_snapshot = copy.deepcopy(loaded_snapshot)

    model_specs = _model_specs_with_pr_context(
        changed_models,
        project_context=project_context,
        events=events,
    )
    incident = analyze_changed_models(
        model_specs,
        previous_snapshot=previous_snapshot,
        deployment_id=deployment_id,
    )

    incident.metadata["history_enabled"] = history_enabled
    incident.metadata["previous_snapshot_loaded"] = previous_snapshot is not None
    if previous_snapshot is not None:
        incident.metadata["previous_snapshot_id"] = _snapshot_id(previous_snapshot)
    if metadata is not None:
        incident.metadata["request_metadata"] = copy.deepcopy(metadata)
    if existing_options:
        incident.metadata["analysis_options"] = copy.deepcopy(existing_options)
    return incident


def analyze_changed_models(
    changed_models,
    *,
    previous_snapshot=None,
    deployment_id=None,
) -> Incident:
    model_specs = list(changed_models)
    signals = []
    affected_models = []

    for model_spec in model_specs:
        model_name = _model_name(model_spec)
        affected_models.append(model_name)

        model_signals = [
            _ast_signal(model_spec, model_name),
            _metadata_signal(model_spec, model_name),
            _drift_signal(model_spec, model_name),
            _blast_radius_signal(model_spec, model_name),
            _historical_reliability_signal(model_spec),
        ]
        business_metric_signal = _business_metric_signal(model_spec)
        if business_metric_signal is not None:
            model_signals.append(business_metric_signal)
        signals.extend(
            _with_model_attribution(signal, model_name)
            for signal in model_signals
        )

    metadata = {
        "model_count": len(model_specs),
        "models": list(affected_models),
    }
    semantic_context = _semantic_context(model_specs, affected_models, metadata)
    semantic_signals = _semantic_signals(semantic_context)
    current_snapshot = None
    semantic_diff = None
    if previous_snapshot is not None:
        current_snapshot = _current_snapshot(
            deployment_id,
            affected_models,
            semantic_context,
            metadata,
        )
        semantic_diff = compare_semantic_snapshots(previous_snapshot, current_snapshot)
        semantic_signals.append(_source_signal(semantic_diff_to_signal(semantic_diff)))

    signals.extend(semantic_signals)
    metadata["signal_count"] = len(signals)
    _add_semantic_metadata(
        metadata,
        semantic_context,
        semantic_signals,
        current_snapshot=current_snapshot,
        semantic_diff=semantic_diff,
    )

    return assemble_decision_incident(
        signals,
        affected_models=affected_models,
        metadata=metadata,
    )


def _model_specs_with_pr_context(
    changed_models,
    *,
    project_context,
    events=None,
) -> list[Any]:
    specs = []
    for model_spec in list(changed_models or []):
        spec = _copy_model_spec(model_spec)
        _set_model_spec_value(
            spec,
            "project_context",
            _merge_contexts(_value(spec, "project_context"), project_context),
        )
        if events is not None and _value(spec, "business_events") is None and _value(spec, "operational_events") is None:
            _set_model_spec_value(spec, "business_events", copy.deepcopy(events))
        specs.append(spec)
    return specs


def _copy_model_spec(model_spec: Any):
    if isinstance(model_spec, dict):
        return copy.deepcopy(model_spec)
    if isinstance(model_spec, str):
        return {
            "model_name": Path(model_spec).stem,
            "sql": "",
        }
    return copy.deepcopy(model_spec)


def _set_model_spec_value(model_spec: Any, key: str, value: Any) -> None:
    if isinstance(model_spec, dict):
        model_spec[key] = value
        return
    setattr(model_spec, key, value)


def _merge_contexts(existing, incoming) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for context in [existing, incoming]:
        if not isinstance(context, dict):
            continue
        for key, value in context.items():
            if isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(_copy_list_items(value))
            elif isinstance(value, dict):
                merged.setdefault(key, {})
                merged[key].update(copy.deepcopy(value))
            else:
                merged[key] = copy.deepcopy(value)
    return merged


def _snapshot_id(snapshot) -> str | None:
    if isinstance(snapshot, dict):
        value = snapshot.get("snapshot_id")
    else:
        value = getattr(snapshot, "snapshot_id", None)
    return str(value) if value else None


def _ast_signal(model_spec: Any, model_name: str) -> Signal:
    ast_result = run_ast_analysis(_sql(model_spec), model_name)
    return ast_to_signal(ast_result)


def _metadata_signal(model_spec: Any, model_name: str) -> Signal:
    metadata_result = run_metadata_checks(
        _value(model_spec, "conn"),
        model_name,
        list(_value(model_spec, "key_columns", []) or []),
    )
    return metadata_checks_to_signal(metadata_result)


def _drift_signal(model_spec: Any, model_name: str) -> Signal:
    drift_result = compare_last_run(
        _value(model_spec, "metadata_db_path"),
        _value(model_spec, "project_name"),
        model_name,
    )
    return metadata_drift_to_signal(drift_result)


def _blast_radius_signal(model_spec: Any, model_name: str) -> Signal:
    blast_radius_result = calculate_blast_radius(
        _value(model_spec, "project_path"),
        model_name,
        changed_columns=list(_value(model_spec, "changed_columns", []) or []),
    )
    return blast_radius_to_signal(blast_radius_result)


def _historical_reliability_signal(model_spec: Any) -> Signal:
    history = dict(_value(model_spec, "history", {}) or {})
    reliability_result = evaluate_history(history)
    return historical_reliability_to_signal(reliability_result)


def _business_metric_signal(model_spec: Any) -> Signal | None:
    events = _value(model_spec, "business_events")
    if events is None:
        events = _value(model_spec, "operational_events")
    if events is None:
        return None
    metrics = calculate_operational_metrics(list(events))
    baseline = _value(model_spec, "business_metric_baseline")
    if baseline is None:
        baseline = _value(model_spec, "operational_metric_baseline")
    result = evaluate_metric_reliability(
        metrics,
        dict(baseline) if baseline is not None else None,
    )
    return business_metrics_to_signal(result)


def _semantic_context(model_specs: list[Any], affected_models: list[str], metadata: dict[str, Any]):
    project_context = _project_context(model_specs)
    if project_context is None:
        return None

    return build_semantic_context(
        project_context=project_context,
        changed_models=list(affected_models),
        metadata=dict(metadata),
    )


def _semantic_signals(semantic_context) -> list[Signal]:
    if semantic_context is None:
        return []

    signals = []
    if semantic_context.kpi_impact_report is not None:
        signals.append(_source_signal(kpi_impact_to_signal(semantic_context.kpi_impact_report)))
    if semantic_context.contract_validation_result is not None:
        signals.append(_source_signal(semantic_contract_to_signal(semantic_context.contract_validation_result)))
    return signals


def _current_snapshot(
    deployment_id,
    affected_models: list[str],
    semantic_context,
    metadata: dict[str, Any],
):
    return create_deployment_snapshot(
        deployment_id=deployment_id if deployment_id is not None else "pr-analysis-current",
        changed_models=list(affected_models),
        semantic_context=semantic_context,
        metadata={
            "source": "pr_analysis",
            "model_count": metadata.get("model_count", 0),
            "models": list(metadata.get("models", [])),
        },
    )


def _add_semantic_metadata(
    metadata: dict[str, Any],
    semantic_context,
    semantic_signals: list[Signal],
    *,
    current_snapshot=None,
    semantic_diff=None,
) -> None:
    if semantic_context is not None:
        metadata["semantic_context"] = semantic_context.to_dict()
        metadata["impacted_kpis"] = list(_signal_metadata_value(semantic_signals, "impacted_kpis", []))
        metadata["impact_paths"] = [
            list(path)
            for path in _signal_metadata_value(semantic_signals, "impact_paths", [])
        ]
        metadata["contract_validation"] = dict(
            (semantic_context.contract_validation_result or {}).get("metadata", {})
        )

        kpi_impact_signal = next(
            (signal for signal in semantic_signals if signal.component == "kpi_impact"),
            None,
        )
        if kpi_impact_signal is not None:
            metadata["kpi_impact"] = {
                "changed_models": list(kpi_impact_signal.metadata.get("changed_models", [])),
                "impacted_kpis": list(kpi_impact_signal.metadata.get("impacted_kpis", [])),
                "impact_paths": [
                    list(path)
                    for path in kpi_impact_signal.metadata.get("impact_paths", [])
                ],
            }

    if current_snapshot is not None:
        current_snapshot_dict = current_snapshot.to_dict()
        metadata["current_snapshot"] = current_snapshot_dict
        metadata["current_snapshot_id"] = current_snapshot_dict.get("snapshot_id")

    if semantic_diff is not None:
        semantic_diff_dict = semantic_diff.to_dict()
        metadata["semantic_diff"] = semantic_diff_dict
        metadata["previous_snapshot_id"] = semantic_diff.previous_snapshot_id
        metadata["current_snapshot_id"] = semantic_diff.current_snapshot_id
        metadata["changed_kpis"] = list(semantic_diff.changed_kpis or [])
        metadata["dependency_changes"] = dict(semantic_diff_dict.get("dependency_changes", {}))
        metadata["contract_changes"] = dict(semantic_diff_dict.get("contract_changes", {}))


def _source_signal(signal: Signal) -> Signal:
    return Signal(
        component=signal.component,
        severity=signal.severity,
        confidence=signal.confidence,
        score=signal.score,
        reasons=list(signal.reasons),
        metadata={
            **dict(signal.metadata),
            "source_component": signal.component,
        },
    )


def _signal_metadata_value(signals: list[Signal], key: str, default):
    for signal in signals:
        if key in signal.metadata:
            return signal.metadata.get(key, default)
    return default


def _with_model_attribution(signal: Signal, model_name: str) -> Signal:
    metadata = dict(signal.metadata)
    metadata["model_name"] = model_name
    metadata["source_component"] = signal.component
    return Signal(
        component=signal.component,
        severity=signal.severity,
        confidence=signal.confidence,
        score=signal.score,
        reasons=list(signal.reasons),
        metadata=metadata,
    )


def _model_name(model_spec: Any) -> str:
    if isinstance(model_spec, str):
        return Path(model_spec).stem
    for key in ("model_name", "name", "changed_model", "table_name"):
        value = _value(model_spec, key)
        if value:
            return str(value)
    path = _value(model_spec, "path") or _value(model_spec, "sql_path")
    if path:
        return Path(path).stem
    raise ValueError("Each changed model must include a model name.")


def _sql(model_spec: Any) -> str:
    sql = _value(model_spec, "sql")
    if sql is not None:
        return str(sql)

    path = _value(model_spec, "path") or _value(model_spec, "sql_path")
    if path:
        return Path(path).read_text(encoding="utf-8")

    return ""


def _value(model_spec: Any, key: str, default: Any = None) -> Any:
    if isinstance(model_spec, dict):
        return model_spec.get(key, default)
    return getattr(model_spec, key, default)


def _project_context(model_specs: list[Any]) -> dict[str, Any] | None:
    contexts = [
        dict(context)
        for context in (
            _value(model_spec, "project_context")
            for model_spec in model_specs
        )
        if isinstance(context, dict)
    ]
    if not contexts:
        return None

    merged: dict[str, Any] = {}
    for context in contexts:
        for key, value in context.items():
            if isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(_copy_list_items(value))
            elif isinstance(value, dict):
                merged.setdefault(key, {})
                merged[key].update(dict(value))
            else:
                merged[key] = value
    return merged


def _copy_list_items(values: list[Any]) -> list[Any]:
    copied = []
    for value in values:
        if isinstance(value, dict):
            copied.append(dict(value))
        elif isinstance(value, list):
            copied.append(list(value))
        else:
            copied.append(value)
    return copied
