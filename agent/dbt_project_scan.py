"""Local-first scanning of compiled dbt model artifacts."""

import json
from collections import deque
from pathlib import Path

from agent.ast_analyzer import run_ast_analysis


SEVERITY_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def scan_dbt_project(project_path: str, changed_model: str | None = None) -> dict:
    """Scan compiled dbt model SQL and optionally calculate downstream impact."""
    project = Path(project_path)
    _validate_project(project)
    manifest = _load_manifest(project)
    project_name = _project_name(manifest, project)
    artifacts = _model_artifacts(project, manifest, project_name)

    reports = [
        run_ast_analysis(sql_path.read_text(encoding="utf-8"), model_name)
        for model_name, sql_path in artifacts
    ]
    risks_found = sum(len(report.get("bugs", [])) for report in reports)
    highest_severity = _highest_severity(reports)
    resolved_changed_model = _resolve_changed_model(manifest, changed_model)

    return {
        "project_name": project_name,
        "models_scanned": len(reports),
        "risks_found": risks_found,
        "highest_severity": highest_severity,
        "changed_model": resolved_changed_model,
        "affected_models": (
            _downstream_models(manifest, resolved_changed_model)
            if resolved_changed_model
            else []
        ),
        "safe_to_merge": highest_severity not in {"HIGH", "CRITICAL"},
        "model_reports": reports,
    }


def format_scan_report(report: dict) -> str:
    """Return the compact terminal report for a completed project scan."""
    affected_models = ", ".join(report["affected_models"])
    changed_model = report["changed_model"] or "not provided"
    safe_to_merge = "YES" if report["safe_to_merge"] else "NO"

    return "\n".join(
        [
            "Relium Scan Report",
            f"Project: {report['project_name']}",
            f"Models scanned: {report['models_scanned']}",
            f"Risks found: {report['risks_found']}",
            f"Highest severity: {report['highest_severity']}",
            f"Changed model: {changed_model}",
            f"Affected downstream models: [{affected_models}]",
            f"Safe to merge: {safe_to_merge}",
        ]
    )


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


def _highest_severity(reports: list[dict]) -> str:
    highest = "NONE"
    for report in reports:
        severity = report.get("overall_risk", "clean").upper()
        if severity == "CLEAN":
            severity = "NONE"
        if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK[highest]:
            highest = severity
    return highest
