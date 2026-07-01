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
from agent.historical_reliability import evaluate_history
from agent.historical_reliability import to_signal as historical_reliability_to_signal
from agent.incident import Incident
from agent.metadata_checks import run_metadata_checks
from agent.metadata_checks import to_signal as metadata_checks_to_signal
from agent.metadata_drift import compare_last_run
from agent.metadata_drift import to_signal as metadata_drift_to_signal
from agent.signals import Signal


def analyze_changed_models(changed_models) -> Incident:
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

    return assemble_decision_incident(
        signals,
        affected_models=affected_models,
        metadata={
            "model_count": len(model_specs),
            "signal_count": len(signals),
            "models": list(affected_models),
        },
    )


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
