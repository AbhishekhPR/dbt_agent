import copy
import re
from pathlib import Path
from typing import Any

from agent.assumption_verification import to_signal as assumption_verification_to_signal
from agent.ast_analyzer import run_ast_analysis
from agent.ast_analyzer import to_signal as ast_to_signal
from agent.blast_radius import calculate_blast_radius
from agent.blast_radius import to_signal as blast_radius_to_signal
from agent.business_metrics import calculate_operational_metrics
from agent.business_metrics import evaluate_metric_reliability
from agent.business_metrics import to_signal as business_metrics_to_signal
from agent.decision_assembly import assemble_decision_incident
from agent.deployment_outcomes import analyze_outcome_history
from agent.deployment_outcomes import outcome_history_to_signal
from agent.deployment_snapshot import create_deployment_snapshot
from agent.historical_reliability import evaluate_history
from agent.historical_reliability import to_signal as historical_reliability_to_signal
from agent.incident import Incident
from agent.metadata_checks import run_metadata_checks
from agent.metadata_checks import to_signal as metadata_checks_to_signal
from agent.metadata_drift import DriftComparisonUnavailable, compare_last_run
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
    outcomes=None,
    metadata_db_path=None,
    previous_snapshot=None,
    manifest_comparison=None,
    **existing_options,
) -> Incident:
    history_enabled = history_store is not None
    previous_snapshot = copy.deepcopy(previous_snapshot)

    if previous_snapshot is None and history_store is not None:
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
        outcomes=copy.deepcopy(outcomes) if outcomes is not None else None,
        metadata_db_path=metadata_db_path,
        manifest_comparison=manifest_comparison,
    )

    incident.metadata["history_enabled"] = history_enabled
    incident.metadata["previous_snapshot_loaded"] = previous_snapshot is not None
    if previous_snapshot is not None:
        incident.metadata["previous_snapshot_id"] = _snapshot_id(previous_snapshot)
    if metadata is not None:
        incident.metadata["request_metadata"] = copy.deepcopy(metadata)
        incident.metadata.update(copy.deepcopy(metadata))
    if existing_options:
        incident.metadata["analysis_options"] = copy.deepcopy(existing_options)
    return incident


def analyze_changed_models(
    changed_models,
    *,
    previous_snapshot=None,
    deployment_id=None,
    outcomes=None,
    metadata_db_path=None,
    manifest_comparison=None,
) -> Incident:
    model_specs = list(changed_models)
    signals = []
    affected_models = []
    metadata_drift_statuses = []

    for model_spec in model_specs:
        model_name = _model_name(model_spec)
        affected_models.append(model_name)
        drift_signal = _drift_signal(
            model_spec,
            model_name,
            metadata_db_path=metadata_db_path,
        )
        metadata_drift_statuses.append(
            {
                "model_name": model_name,
                "comparison_status": str(
                    drift_signal.metadata.get("comparison_status") or "evaluated"
                ),
            }
        )

        model_signals = [
            _ast_signal(model_spec, model_name),
            _metadata_signal(model_spec, model_name),
            drift_signal,
            _blast_radius_signal(model_spec, model_name),
            _historical_reliability_signal(model_spec),
        ]
        business_metric_signal = _business_metric_signal(model_spec)
        if business_metric_signal is not None:
            model_signals.append(business_metric_signal)
        signals.extend(
            _with_model_attribution(signal, model_name)
            for signal in model_signals
            if signal is not None
        )

    metadata = {
        "model_count": len(model_specs),
        "models": list(affected_models),
        "metadata_drift": metadata_drift_statuses,
    }
    sql_sources = [
        _sql_source_metadata(model_spec)
        for model_spec in model_specs
    ]
    changed_columns_by_model = _changed_columns_by_model_from_specs(model_specs)
    if changed_columns_by_model:
        metadata["changed_columns_by_model"] = changed_columns_by_model

    outcome_signal = _deployment_outcome_signal(
        outcomes,
        deployment_id=deployment_id,
        changed_models=affected_models,
    )
    if outcome_signal is not None:
        signals.append(outcome_signal)
        metadata["outcome_memory"] = copy.deepcopy(outcome_signal.metadata)

    semantic_context = _semantic_context(model_specs, affected_models, metadata)
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
        derived_changed_columns = (
            (semantic_diff.metadata or {}).get("changed_columns_by_model") or {}
        )
        if derived_changed_columns:
            metadata["changed_columns_by_model"] = _merge_changed_columns(
                changed_columns_by_model,
                derived_changed_columns,
            )
            metadata["column_dependency_changes"] = list(
                (semantic_diff.metadata or {}).get("column_dependency_changes") or []
            )
            semantic_context = _semantic_context(model_specs, affected_models, metadata)
            current_snapshot = _current_snapshot(
                deployment_id,
                affected_models,
                semantic_context,
                metadata,
            )

    semantic_signals = _semantic_signals(semantic_context)
    manifest_signal = _manifest_comparison_signal(manifest_comparison)
    if semantic_diff is not None:
        semantic_signals.append(_source_signal(semantic_diff_to_signal(semantic_diff)))
    if manifest_signal is not None and not _declared_semantics_available(manifest_comparison):
        semantic_signals.append(manifest_signal)
    semantic_signals = [
        _with_semantic_audit_metadata(
            signal,
            manifest_comparison=manifest_comparison,
        )
        for signal in semantic_signals
    ]

    signals.extend(semantic_signals)
    metadata["signal_count"] = len(signals)
    _add_semantic_metadata(
        metadata,
        semantic_context,
        semantic_signals,
        current_snapshot=current_snapshot,
        semantic_diff=semantic_diff,
    )
    metadata["sql_sources"] = sql_sources
    if manifest_comparison is not None:
        metadata["manifest_comparison"] = copy.deepcopy(manifest_comparison)

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


def _ast_signal(model_spec: Any, model_name: str) -> Signal | None:
    if _value(model_spec, "sql_available") is False:
        return None

    sql = _sql(model_spec)
    if not isinstance(sql, str) or not sql.strip():
        return None

    ast_result = run_ast_analysis(sql, model_name)
    signal = ast_to_signal(ast_result)
    signal.metadata.update(
        {
            "sql_available": True,
            "sql_source": _value(model_spec, "sql_source", "sql"),
            "ast_status": "evaluated",
        }
    )
    return signal


def _metadata_signal(model_spec: Any, model_name: str) -> Signal:
    if _value(model_spec, "conn") is None:
        return metadata_checks_to_signal(
            {
                "model_name": model_name,
                "evaluation_status": "not_evaluated",
                "anomalies": [],
            }
        )
    metadata_result = run_metadata_checks(
        _value(model_spec, "conn"),
        model_name,
        list(_value(model_spec, "key_columns", []) or []),
    )
    return metadata_checks_to_signal(metadata_result)


def _drift_signal(
    model_spec: Any,
    model_name: str,
    *,
    metadata_db_path=None,
) -> Signal:
    if metadata_db_path is None:
        return metadata_drift_to_signal(_unavailable_drift(model_name))

    try:
        drift_result = compare_last_run(
            metadata_db_path,
            _value(model_spec, "project_name"),
            model_name,
        )
    except DriftComparisonUnavailable:
        drift_result = _unavailable_drift(model_name)
    return metadata_drift_to_signal(drift_result)


def _unavailable_drift(model_name: str) -> dict[str, Any]:
    return {
        "project_name": None,
        "model_name": model_name,
        "comparison_status": "unavailable",
        "drift_level": "LOW",
        "report_text": "Metadata drift was not evaluated.",
    }


def _blast_radius_signal(model_spec: Any, model_name: str) -> Signal:
    if _value(model_spec, "project_path") is None:
        return blast_radius_to_signal(
            {
                "changed_table": model_name,
                "changed_columns": list(_value(model_spec, "changed_columns", []) or []),
                "directly_affected": [],
                "indirectly_affected": [],
                "total_affected": 0,
                "summary": "Blast radius not evaluated.",
            }
        )
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


def _deployment_outcome_signal(outcomes, *, deployment_id, changed_models) -> Signal | None:
    if not outcomes:
        return None
    analysis = analyze_outcome_history(
        copy.deepcopy(list(outcomes or [])),
        deployment_id=deployment_id,
        changed_models=list(changed_models or []),
    )
    return outcome_history_to_signal(analysis)


def _semantic_context(model_specs: list[Any], affected_models: list[str], metadata: dict[str, Any]):
    project_context = _project_context(model_specs)
    if project_context is None:
        return None

    kwargs = {
        "project_context": project_context,
        "changed_models": list(affected_models),
        "metadata": dict(metadata),
    }
    assumption_connection = _assumption_connection(model_specs)
    if assumption_connection is not None:
        kwargs["assumption_connection"] = assumption_connection

    return build_semantic_context(**kwargs)


def _semantic_signals(semantic_context) -> list[Signal]:
    if semantic_context is None:
        return []

    signals = []
    if semantic_context.kpi_impact_report is not None:
        signals.append(_source_signal(kpi_impact_to_signal(semantic_context.kpi_impact_report)))
    if semantic_context.contract_validation_result is not None:
        signals.append(_source_signal(semantic_contract_to_signal(semantic_context.contract_validation_result)))
    assumption_signal = assumption_verification_to_signal(
        getattr(semantic_context, "assumption_verification", None)
    )
    if assumption_signal is not None:
        signals.append(_source_signal(assumption_signal))
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
        semantic_context_dict = semantic_context.to_dict()
        metadata["semantic_context"] = semantic_context_dict
        if semantic_context_dict.get("assumption_verification"):
            metadata["assumption_verification"] = dict(
                semantic_context_dict["assumption_verification"]
            )
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
        metadata["changed_columns_by_model"] = dict(
            semantic_diff_dict.get("metadata", {}).get(
                "changed_columns_by_model",
                metadata.get("changed_columns_by_model", {}),
            )
        )
        metadata["column_dependency_changes"] = list(
            semantic_diff_dict.get("metadata", {}).get(
                "column_dependency_changes",
                metadata.get("column_dependency_changes", []),
            )
        )

    metadata["semantic_findings"] = [
        _semantic_finding_metadata(signal)
        for signal in semantic_signals
        if signal.score < 0 and _semantic_finding_owner(signal)
    ]


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


def _declared_semantics_available(manifest_comparison) -> bool:
    return bool(
        isinstance(manifest_comparison, dict)
        and manifest_comparison.get("declared_semantic_evidence_available")
    )


def _with_semantic_audit_metadata(
    signal: Signal,
    *,
    manifest_comparison,
) -> Signal:
    comparison = manifest_comparison if isinstance(manifest_comparison, dict) else {}
    metadata = dict(signal.metadata or {})
    owner = _semantic_finding_owner(signal)
    fallback_used = owner == "semantic_refund_fallback"
    metadata.update(
        {
            "finding_owner": owner,
            "finding_type": _semantic_finding_type(signal, owner),
            "evidence_source": _semantic_evidence_source(owner),
            "base_manifest_available": bool(
                comparison.get("base_manifest_available")
            ),
            "head_manifest_available": bool(
                comparison.get("head_manifest_available")
            ),
            "semantic_comparison_evaluated": bool(
                comparison.get("semantic_comparison_evaluated")
            ),
            "fallback_used": fallback_used,
        }
    )
    if fallback_used:
        metadata["fallback_scope"] = (
            "refund_adjustment_subtraction_from_net_or_gross_business_expression"
        )
    return Signal(
        component=signal.component,
        severity=signal.severity,
        confidence=signal.confidence,
        score=signal.score,
        reasons=list(signal.reasons),
        metadata=metadata,
    )


def _semantic_finding_owner(signal: Signal) -> str | None:
    metadata = dict(signal.metadata or {})
    explicit = metadata.get("finding_owner")
    if explicit in {
        "semantic_contract",
        "semantic_diff",
        "kpi_definition",
        "semantic_refund_fallback",
    }:
        return str(explicit)
    return {
        "semantic_contract": "semantic_contract",
        "semantic_diff": "semantic_diff",
        "kpi_impact": "kpi_definition",
        "assumption_verification": "semantic_contract",
    }.get(signal.component)


def _semantic_finding_type(signal: Signal, owner: str | None) -> str:
    explicit = (signal.metadata or {}).get("finding_type")
    if explicit:
        return str(explicit)
    if owner == "semantic_refund_fallback":
        return "refund_adjustment_subtraction_removed"
    if signal.component == "semantic_diff":
        if (signal.metadata or {}).get("added_kpis") or (signal.metadata or {}).get("removed_kpis"):
            return "kpi_definition_change"
        if (signal.metadata or {}).get("contract_changes"):
            return "declared_contract_or_invariant_change"
        return "semantic_snapshot_change"
    return {
        "semantic_contract": "semantic_contract_violation",
        "kpi_impact": "kpi_definition_impact",
        "assumption_verification": "semantic_contract_assumption",
    }.get(signal.component, "semantic_finding")


def _semantic_evidence_source(owner: str | None) -> str:
    return {
        "semantic_contract": "declared_semantic_contract",
        "semantic_diff": "trusted_base_head_semantic_snapshots",
        "kpi_definition": "declared_kpi_definition",
        "semantic_refund_fallback": "trusted_base_head_manifest_sql",
    }.get(owner, "semantic_analysis")


def _semantic_finding_metadata(signal: Signal) -> dict:
    metadata = dict(signal.metadata or {})
    finding = {
        "component": signal.component,
        "severity": signal.severity,
        "finding_owner": metadata["finding_owner"],
        "finding_type": metadata["finding_type"],
        "evidence_source": metadata["evidence_source"],
        "base_manifest_available": bool(metadata["base_manifest_available"]),
        "head_manifest_available": bool(metadata["head_manifest_available"]),
        "semantic_comparison_evaluated": bool(
            metadata["semantic_comparison_evaluated"]
        ),
        "fallback_used": bool(metadata["fallback_used"]),
    }
    if metadata.get("fallback_scope"):
        finding["fallback_scope"] = str(metadata["fallback_scope"])
    return finding


def _manifest_comparison_signal(comparison) -> Signal | None:
    if not comparison or not comparison.get("evaluated"):
        return None
    changes = list(comparison.get("material_sql_changes") or [])
    if not changes:
        return None
    return Signal(
        component="semantic_diff",
        severity="HIGH",
        confidence=95,
        score=-35,
        reasons=[
            f"Trusted base/head manifest SQL changed for {change['model_name']}."
            for change in changes
        ],
        metadata={
            "semantic_comparison": "evaluated",
            "manifest_sql_changes": copy.deepcopy(changes),
            **copy.deepcopy(changes[0]),
        },
    )


def compare_manifest_sql(previous_manifest, current_manifest, changed_models) -> dict:
    """Compare trusted manifest SQL using the secondary refund fallback only.

    Declared contracts, invariants, and KPI definitions take precedence. When
    they are absent, this deliberately narrow fallback detects removal of a
    refund/adjustment subtraction from a net/gross business expression. It is
    not arbitrary SQL semantic equivalence.
    """
    base_available = isinstance(previous_manifest, dict)
    head_available = isinstance(current_manifest, dict)
    evaluated = base_available and head_available
    declared_available = _manifest_declared_semantics_available(
        previous_manifest,
        current_manifest,
        changed_models,
    )
    audit = {
        "evaluated": evaluated,
        "base_manifest_available": base_available,
        "head_manifest_available": head_available,
        "semantic_comparison_evaluated": evaluated,
        "declared_semantic_evidence_available": declared_available,
        "material_sql_changes": [],
    }
    if not evaluated or declared_available:
        return audit
    previous_nodes = _manifest_model_nodes(previous_manifest)
    current_nodes = _manifest_model_nodes(current_manifest)
    changes = []
    for name in list(changed_models or []):
        previous = previous_nodes.get(name)
        current = current_nodes.get(name)
        if not previous or not current:
            continue
        previous_sql = _canonical_sql(previous.get("raw_code") or previous.get("compiled_code") or previous.get("sql"))
        current_sql = _canonical_sql(current.get("raw_code") or current.get("compiled_code") or current.get("sql"))
        if (
            previous_sql
            and current_sql
            and previous_sql != current_sql
            and _material_sql_delta(previous_sql, current_sql)
        ):
            changes.append(
                {
                    "model_name": str(name),
                    "finding_owner": "semantic_refund_fallback",
                    "finding_type": "refund_adjustment_subtraction_removed",
                    "evidence_source": "trusted_base_head_manifest_sql",
                    "base_manifest_available": base_available,
                    "head_manifest_available": head_available,
                    "semantic_comparison_evaluated": evaluated,
                    "fallback_used": True,
                    "fallback_scope": (
                        "refund_adjustment_subtraction_from_net_or_gross_business_expression"
                    ),
                }
            )
    audit["material_sql_changes"] = changes
    return audit


def _manifest_declared_semantics_available(
    previous_manifest,
    current_manifest,
    changed_models,
) -> bool:
    changed = {str(value) for value in list(changed_models or [])}
    for manifest in (previous_manifest, current_manifest):
        if not isinstance(manifest, dict):
            continue
        metrics = manifest.get("metrics")
        for metric in metrics.values() if isinstance(metrics, dict) else []:
            if not isinstance(metric, dict):
                continue
            config = metric.get("config") if isinstance(metric.get("config"), dict) else {}
            meta = metric.get("meta") if isinstance(metric.get("meta"), dict) else {}
            if not meta and isinstance(config.get("meta"), dict):
                meta = config["meta"]
            relium = meta.get("relium") if isinstance(meta.get("relium"), dict) else {}
            if not relium:
                continue
            dependencies = (metric.get("depends_on") or {}).get("nodes", [])
            dependency_names = {
                str(value).rsplit(".", 1)[-1]
                for value in dependencies
            }
            if not changed or changed.intersection(dependency_names) or changed.intersection(dependencies):
                return True
    return False


def _manifest_model_nodes(manifest) -> dict[str, dict]:
    nodes = (manifest or {}).get("nodes") if isinstance(manifest, dict) else {}
    result = {}
    for key, value in (nodes.items() if isinstance(nodes, dict) else []):
        if not isinstance(value, dict) or value.get("resource_type") not in {None, "model"}:
            continue
        name = str(value.get("name") or str(key).rsplit(".", 1)[-1])
        result[name] = value
        result[str(value.get("unique_id") or key)] = value
    return result


def _canonical_sql(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = re.sub(r"/\*.*?\*/", " ", value, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"\{\{.*?\}\}", "dbt_ref", text, flags=re.S)
    try:
        import sqlglot

        return sqlglot.parse_one(text).sql(dialect="sqlite")
    except Exception:
        return " ".join(text.lower().split())


def _material_sql_delta(previous_sql: str, current_sql: str) -> bool:
    """Detect removal of a refund adjustment from a net/gross KPI expression.

    This is a deliberately narrow, syntax-independent fallback for manifests
    that do not carry a declared contract, invariant, or KPI definition. It
    examines removal of a refund/adjustment subtraction from a net/gross
    business expression, not model names or one exact SQL spelling. It is not arbitrary SQL semantic equivalence.
    Declared contracts, invariants, and KPI
    definitions take precedence.
    """
    return _has_refund_adjustment(previous_sql) and not _has_refund_adjustment(current_sql)


def _has_refund_adjustment(sql: str) -> bool:
    if not isinstance(sql, str):
        return False
    # Parentheses and COALESCE are deliberately accepted; comments and casing
    # have already been normalised by _canonical_sql.
    return bool(
        re.search(
            r"\b(?:gross|net|total|revenue|sales|income|amount)[a-z0-9_]*\s*-\s*"
            r"(?:\(\s*)?(?:coalesce\s*\(\s*)?refund[a-z0-9_.]*",
            sql,
            flags=re.I,
        )
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


def _sql_source_metadata(model_spec: Any) -> dict[str, Any]:
    sql_available = _value(model_spec, "sql_available")
    if sql_available is None:
        sql = _value(model_spec, "sql")
        sql_available = isinstance(sql, str) and bool(sql.strip())
    else:
        sql_available = bool(sql_available)

    return {
        "unique_id": _value(model_spec, "unique_id"),
        "name": _model_name(model_spec),
        "original_file_path": _value(model_spec, "original_file_path"),
        "path": _value(model_spec, "path") or _value(model_spec, "sql_path"),
        "sql_available": sql_available,
        "sql_source": (
            _value(model_spec, "sql_source", "sql")
            if sql_available
            else "unavailable"
        ),
        "ast_status": "evaluated" if sql_available else "skipped",
    }


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


def _assumption_connection(model_specs: list[Any]):
    for model_spec in model_specs:
        connection = _value(model_spec, "conn")
        if connection is not None:
            return connection
    return None


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


def _changed_columns_by_model_from_specs(model_specs: list[Any]) -> dict[str, list[str]]:
    changed: dict[str, list[str]] = {}
    for model_spec in model_specs:
        columns = [
            str(column)
            for column in list(_value(model_spec, "changed_columns", []) or [])
            if column
        ]
        if not columns:
            continue
        changed[_model_name(model_spec)] = _ordered_unique(columns)
    return changed


def _merge_changed_columns(*maps: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for values_by_model in maps:
        for model_name, columns in (values_by_model or {}).items():
            merged.setdefault(str(model_name), [])
            merged[str(model_name)].extend(str(column) for column in columns or [] if column)
            merged[str(model_name)] = _ordered_unique(merged[str(model_name)])
    return {
        model_name: columns
        for model_name, columns in merged.items()
        if columns
    }


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique
