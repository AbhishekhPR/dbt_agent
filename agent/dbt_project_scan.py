"""Local-first scanning of compiled dbt model artifacts."""

import json
from collections import deque
from pathlib import Path

from agent.ast_analyzer import RULE_RECOMMENDATIONS, run_ast_analysis


SEVERITY_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def scan_dbt_project(
    project_path: str,
    changed_model: str | None = None,
    diff_base: str = "origin/main",
) -> dict:
    """Scan compiled dbt model SQL and optionally calculate downstream impact."""
    project = Path(project_path)
    _validate_project(project)
    manifest = _load_manifest(project)
    project_name = _project_name(manifest, project)
    artifacts = _model_artifacts(project, manifest, project_name)

    reports = []
    for model_name, sql_path in artifacts:
        report = run_ast_analysis(sql_path.read_text(encoding="utf-8"), model_name)
        report["compiled_sql_path"] = str(sql_path)
        reports.append(report)
    risks_found = sum(len(report.get("bugs", [])) for report in reports)
    highest_severity = _highest_severity(reports)
    if changed_model is not None:
        resolved_changed_models = [_resolve_changed_model(manifest, changed_model)]
    else:
        from agent.changed_models import detect_changed_models

        resolved_changed_models = [
            _resolve_changed_model(manifest, model)
            for model in detect_changed_models(project, diff_base)
        ]

    return {
        "project_name": project_name,
        "models_scanned": len(reports),
        "risks_found": risks_found,
        "highest_severity": highest_severity,
        "changed_model": resolved_changed_models[0] if len(resolved_changed_models) == 1 else None,
        "changed_models": resolved_changed_models,
        "affected_models": _affected_models(manifest, resolved_changed_models),
        "safe_to_merge": highest_severity not in {"HIGH", "CRITICAL"},
        "model_reports": reports,
    }


def format_scan_report(report: dict) -> str:
    """Return the compact terminal report for a completed project scan."""
    affected_models = ", ".join(report["affected_models"])
    changed_models = report.get("changed_models", [])
    changed_model = ", ".join(changed_models) or "not provided"
    changed_label = "Changed model" if len(changed_models) <= 1 else "Changed models"
    safe_to_merge = "YES" if report["safe_to_merge"] else "NO"

    return "\n".join(
        [
            "Relium Scan Report",
            f"Project: {report['project_name']}",
            f"Models scanned: {report['models_scanned']}",
            f"Risks found: {report['risks_found']}",
            f"Highest severity: {report['highest_severity']}",
            f"{changed_label}: {changed_model}",
            f"Affected downstream models: [{affected_models}]",
            f"Safe to merge: {safe_to_merge}",
        ]
    )


def format_markdown_scan_report(report: dict) -> str:
    """Render a decision-first Markdown summary for pull-request review."""
    changed_models = report.get("changed_models", [])
    changed_model = ", ".join(changed_models) or "not provided"
    changed_label = "Changed model" if len(changed_models) <= 1 else "Changed models"
    affected_models = ", ".join(report["affected_models"]) or "[]"
    safe_to_merge = "YES" if report["safe_to_merge"] else "NO"
    rows = [
        ("Project", report["project_name"]),
        ("Models scanned", report["models_scanned"]),
        ("Risks found", report["risks_found"]),
        ("Highest severity", report["highest_severity"]),
        (changed_label, changed_model),
        ("Affected downstream models", affected_models),
        ("Safe to merge", safe_to_merge),
    ]
    if report["safe_to_merge"]:
        recommendation = [
            "🟢 **SAFE TO MERGE**",
            "",
            "No high or critical logic risks detected.",
        ]
    else:
        recommendation = [
            "🔴 **DO NOT MERGE YET**",
            "",
            "High or critical logic risks require review.",
        ]

    impact = [f"{changed_label}:", f"`{changed_model}`", "", "Affected downstream models:"]
    if report["affected_models"]:
        impact.append(", ".join(f"`{model}`" for model in report["affected_models"]))
    else:
        impact.append("None")

    findings = _format_grouped_markdown_findings(report["model_reports"])

    return "\n".join(
        [
            "## Relium PR Risk Summary",
            "",
            "### Merge Recommendation",
            "",
            *recommendation,
            "",
            "### Changed Model Impact",
            "",
            *impact,
            "",
            "### Findings",
            "",
            *findings,
            "",
            "### Scan Details",
            "",
            "| Field | Value |",
            "|---|---|",
            *[f"| {field} | {value} |" for field, value in rows],
        ]
    )


def _format_grouped_markdown_findings(model_reports: list[dict]) -> list[str]:
    """Group repeated AST findings into one reviewer-focused section per rule."""
    grouped: dict[str, dict] = {}
    for model_report in model_reports:
        for bug in model_report.get("bugs", []):
            rule = bug.get("rule", "UNCLASSIFIED")
            group = grouped.setdefault(
                rule,
                {
                    "models": [],
                    "bugs": [],
                },
            )
            if model_report["model_name"] not in group["models"]:
                group["models"].append(model_report["model_name"])
            group["bugs"].append(bug)

    if not grouped:
        return ["No SQL risks found in the compiled models."]

    lines = []
    for rule, group in grouped.items():
        representative = group["bugs"][0]
        severity = max(
            (bug.get("severity", "low").upper() for bug in group["bugs"]),
            key=lambda value: SEVERITY_RANK.get(value, 0),
        )
        lines.extend(
            [
                f"#### {rule}",
                "",
                f"Severity: {severity}",
                "",
                "Affected models:",
                *[f"- {model_name}" for model_name in group["models"]],
                "",
                "Why it matters:",
                representative.get("description", "No description provided."),
                "",
                "Recommendation:",
                representative.get(
                    "recommendation",
                    RULE_RECOMMENDATIONS.get(rule, "Review the affected model."),
                ),
                "",
            ]
        )
    return lines


def format_verbose_scan_report(report: dict) -> str:
    """Return the compact report followed by a complete model-level audit."""
    lines = [format_scan_report(report), "", "Scanned models:"]

    for model_report in report["model_reports"]:
        lines.extend(_format_model_audit(model_report))

    if report.get("changed_models", []):
        downstream_models = ", ".join(report["affected_models"])
        lines.extend(["", f"Downstream models: [{downstream_models}]"])

    return "\n".join(lines)


def _format_model_audit(model_report: dict) -> list[str]:
    lines = [
        "",
        f"Model: {model_report['model_name']}",
        f"Compiled SQL: {model_report['compiled_sql_path']}",
    ]
    bugs = model_report.get("bugs", [])
    if not bugs:
        lines.append("No risks found")
        return lines

    for bug in bugs:
        lines.append(
            f"[{bug['severity'].upper()}] {bug['rule']}: {bug['description']}"
        )
    return lines


def _validate_project(project: Path) -> None:
    if not project.is_dir():
        raise ValueError(f"dbt project directory does not exist: {project}")
    if not (project / "dbt_project.yml").is_file():
        raise ValueError(f"dbt_project.yml not found in: {project}")


def _load_manifest(project: Path) -> dict:
    manifest_path = project / "target" / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"target/manifest.json not found in: {project}")
    with manifest_path.open(encoding="utf-8") as manifest_file:
        return json.load(manifest_file)


def _project_name(manifest: dict, project: Path) -> str:
    project_name = manifest.get("metadata", {}).get("project_name")
    if project_name:
        return project_name

    for line in (project / "dbt_project.yml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return project.name


def _model_artifacts(project: Path, manifest: dict, project_name: str) -> list[tuple[str, Path]]:
    compiled_root = project / "target" / "compiled"
    run_root = project / "target" / "run"
    if compiled_root.is_dir():
        artifact_root = compiled_root
    elif run_root.is_dir():
        artifact_root = run_root
    else:
        raise ValueError(
            "No dbt model SQL artifacts found. Run dbt compile or dbt run first "
            "to create target/compiled or target/run."
        )

    artifacts = []
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "model":
            continue
        sql_path = _artifact_path(project, artifact_root, project_name, node)
        if sql_path is not None:
            artifacts.append((node["name"], sql_path))

    if not artifacts:
        raise ValueError(
            f"No compiled model SQL files found under {artifact_root}. "
            "Ensure dbt compile or dbt run completed successfully."
        )
    return artifacts


def _artifact_path(
    project: Path,
    artifact_root: Path,
    project_name: str,
    node: dict,
) -> Path | None:
    candidates = []

    compiled_path = node.get("compiled_path")
    if compiled_path:
        candidates.append(project / _path_value(compiled_path))

    package_name = node.get("package_name") or project_name
    node_path = node.get("path")
    if node_path:
        candidates.append(artifact_root / package_name / _path_value(node_path))

    candidates.append(artifact_root / f"{node['name']}.sql")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    matches = sorted(artifact_root.rglob(f"{node['name']}.sql"))
    return matches[0] if matches else None


def _path_value(path_value: str) -> Path:
    """Interpret dbt artifact paths from manifests created on either OS."""
    return Path(path_value.replace("\\", "/"))


def _resolve_changed_model(manifest: dict, changed_model: str | None) -> str | None:
    if changed_model is None:
        return None

    wanted_name = changed_model.casefold()
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") == "model" and node.get("name", "").casefold() == wanted_name:
            return node["name"]
    raise ValueError(f"Changed dbt model not found in manifest: {changed_model}")


def _downstream_models(manifest: dict, changed_model: str) -> list[str]:
    reverse_dependencies: dict[str, list[str]] = {}
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "model":
            continue
        for dependency in node.get("depends_on", {}).get("nodes", []):
            reverse_dependencies.setdefault(dependency, []).append(node["name"])

    start_node_id = next(
        node_id
        for node_id, node in manifest.get("nodes", {}).items()
        if node.get("resource_type") == "model" and node.get("name") == changed_model
    )
    queue = deque(reverse_dependencies.get(start_node_id, []))
    affected_models = []
    visited = {changed_model.casefold()}

    while queue:
        model_name = queue.popleft()
        model_key = model_name.casefold()
        if model_key in visited:
            continue
        visited.add(model_key)
        affected_models.append(model_name)

        downstream_node_id = next(
            node_id
            for node_id, node in manifest.get("nodes", {}).items()
            if node.get("resource_type") == "model" and node.get("name") == model_name
        )
        queue.extend(reverse_dependencies.get(downstream_node_id, []))

    return affected_models


def _affected_models(manifest: dict, changed_models: list[str]) -> list[str]:
    affected_models = []
    seen = set()
    for changed_model in changed_models:
        for model_name in _downstream_models(manifest, changed_model):
            model_key = model_name.casefold()
            if model_key not in seen:
                seen.add(model_key)
                affected_models.append(model_name)
    return affected_models


def _highest_severity(reports: list[dict]) -> str:
    highest = "NONE"
    for report in reports:
        severity = report.get("overall_risk", "clean").upper()
        if severity == "CLEAN":
            severity = "NONE"
        if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK[highest]:
            highest = severity
    return highest
