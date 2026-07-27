"""Static SQL/dbt PR guard checks, built on the current AST-analysis architecture.

Scans changed (or all) dbt model SQL with agent.ast_analyzer, renders a
Markdown report, and gates on a configurable severity threshold. Does not
make network calls, does not call an LLM, and does not read or expose
secrets.
"""

from pathlib import Path
from typing import Any

from agent.ast_analyzer import run_ast_analysis
from agent.blast_radius import calculate_blast_radius
from agent.logging_config import get_logger


logger = get_logger(__name__)


SEVERITY_ORDER = {
    "clean": 0,
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class PrGuardError(ValueError):
    """Raised when pr_guard is given invalid inputs."""


def run_pr_guard(
    project_path: str,
    changed_files: list[str] | None = None,
    fail_on: str = "high",
    output: str = ".relium/pr_guard_report.md",
    github_comment: bool = False,
    comment_output: str = ".relium/pr_guard_comment.md",
) -> dict[str, Any]:
    """Run static SQL checks over changed (or all) models and write a report."""
    project = Path(project_path)
    if not project.exists() or not project.is_dir():
        raise PrGuardError(f"dbt project not found: {project_path}")

    sql_files = _resolve_sql_files(project, changed_files)
    model_reports = [
        _model_report(project, model_name, sql_path)
        for model_name, sql_path in sql_files
    ]

    highest_severity = _highest_severity(model_reports)
    exit_code = _exit_code(highest_severity, fail_on)

    result: dict[str, Any] = {
        "project_path": str(project),
        "models_scanned": len(model_reports),
        "changed_files_provided": bool(changed_files),
        "highest_severity": highest_severity,
        "fail_on": fail_on.lower(),
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "model_reports": model_reports,
        "report_path": None,
        "comment_path": None,
        "github_comment_status": None,
    }

    report_path = Path(output)
    _write_text(report_path, render_markdown_report(result))
    result["report_path"] = str(report_path)

    if github_comment:
        comment_path = Path(comment_output)
        _write_text(comment_path, render_pr_comment(result))
        result["comment_path"] = str(comment_path)
        result["github_comment_status"] = _github_comment_status()

    return result


def terminal_summary(report: dict[str, Any]) -> str:
    """Short human-readable summary for CLI stdout."""
    status = "PASSED" if report.get("passed") else "FAILED"
    lines = [
        f"PR Guard: {status}",
        f"  Models scanned: {report.get('models_scanned', 0)}",
        f"  Highest severity: {report.get('highest_severity', 'clean')}",
        f"  Fail-on threshold: {report.get('fail_on', 'high')}",
    ]
    if report.get("report_path"):
        lines.append(f"  Report written to: {report['report_path']}")
    if report.get("comment_path"):
        lines.append(f"  Comment markdown written to: {report['comment_path']}")
    return "\n".join(lines)


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "## Relium PR Guard",
        "",
        f"**Result:** {'PASSED' if report.get('passed') else 'FAILED'}",
        f"**Models scanned:** {report.get('models_scanned', 0)}",
        f"**Highest severity:** {report.get('highest_severity', 'clean')}",
        f"**Fail-on threshold:** {report.get('fail_on', 'high')}",
        "",
    ]
    model_reports = report.get("model_reports") or []
    if not model_reports:
        lines.append("No SQL models were scanned.")
        return "\n".join(lines)

    for model_report in model_reports:
        lines.extend(_model_section_lines(model_report))
    return "\n".join(lines)


def render_pr_comment(report: dict[str, Any]) -> str:
    status = "PASSED" if report.get("passed") else "FAILED"
    lines = [
        f"### Relium PR Guard — {status}",
        "",
        f"Highest severity: **{report.get('highest_severity', 'clean')}** "
        f"(fail-on: {report.get('fail_on', 'high')})",
        "",
    ]
    flagged = [
        model_report
        for model_report in (report.get("model_reports") or [])
        if model_report.get("bugs")
    ]
    if not flagged:
        lines.append("No issues found.")
        return "\n".join(lines)

    for model_report in flagged:
        lines.append(f"- **{model_report.get('model_name')}**: {model_report.get('summary')}")
    return "\n".join(lines)


def _model_section_lines(model_report: dict[str, Any]) -> list[str]:
    lines = [
        f"### {model_report.get('model_name')}",
        f"- Risk: {model_report.get('overall_risk', 'clean')}",
        f"- {model_report.get('summary', 'Found 0 potential issue(s)')}",
    ]
    blast_radius = model_report.get("blast_radius")
    if blast_radius and blast_radius.get("total_affected"):
        lines.append(f"- Blast radius: {blast_radius.get('summary')}")
    bugs = model_report.get("bugs") or []
    for bug in bugs:
        lines.append(
            f"  - [{bug.get('severity', '').upper()}] {bug.get('category')}: "
            f"{bug.get('description')} — {bug.get('recommendation')}"
        )
    lines.append("")
    return lines


def _model_report(project: Path, model_name: str, sql_path: Path) -> dict[str, Any]:
    sql_text = sql_path.read_text(encoding="utf-8")
    report = run_ast_analysis(sql_text, model_name)
    report["sql_path"] = str(sql_path)
    report["blast_radius"] = _safe_blast_radius(project, model_name)
    return report


def _safe_blast_radius(project: Path, model_name: str) -> dict[str, Any] | None:
    try:
        return calculate_blast_radius(str(project), model_name)
    except Exception as error:
        logger.debug(f"Could not compute blast radius for {model_name}: {error}")
        return None


def _resolve_sql_files(project: Path, changed_files: list[str] | None) -> list[tuple[str, Path]]:
    if changed_files:
        resolved: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for raw_path in changed_files:
            if not str(raw_path).endswith(".sql"):
                continue
            candidate = Path(raw_path)
            if not candidate.exists():
                candidate = project / raw_path
            if not candidate.exists() or not candidate.is_file():
                continue
            key = str(candidate.resolve())
            if key in seen:
                continue
            seen.add(key)
            resolved.append((candidate.stem, candidate))
        return resolved

    models_path = project / "models"
    if not models_path.exists():
        return []
    return [(sql_file.stem, sql_file) for sql_file in sorted(models_path.glob("**/*.sql"))]


def _highest_severity(model_reports: list[dict[str, Any]]) -> str:
    worst = "clean"
    for model_report in model_reports:
        risk = str(model_report.get("overall_risk", "clean")).lower()
        if SEVERITY_ORDER.get(risk, 0) > SEVERITY_ORDER.get(worst, 0):
            worst = risk
    return worst


def _exit_code(highest_severity: str, fail_on: str) -> int:
    if highest_severity == "clean":
        return 0
    threshold = SEVERITY_ORDER.get(fail_on.lower(), SEVERITY_ORDER["high"])
    found = SEVERITY_ORDER.get(highest_severity, 0)
    return 1 if found >= threshold else 0


def _github_comment_status() -> dict[str, Any]:
    return {
        "posted": False,
        "reason": "missing_environment",
        "detail": (
            "Posting a live PR comment requires an authenticated GitHub App "
            "installation client (owner, repository, pull request number, "
            "and an installation access token), which a local CLI invocation "
            "does not have. Comment markdown was written locally instead. "
            "Use the Relium GitHub App webhook flow (agent.github_app) to "
            "post it automatically, or paste the file contents into the PR "
            "by hand."
        ),
    }


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
