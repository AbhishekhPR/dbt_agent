import click
from pathlib import Path
from agent.logging_config import configure_logging


def _validate_file_exists(file_path: str, description: str) -> Path:
    """Validate a file exists and return a Path object.

    Raises click.ClickException if file does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise click.ClickException(f"{description} not found: {file_path}")
    if not path.is_file():
        raise click.ClickException(f"{description} is not a file: {file_path}")
    return path


def _validate_directory_exists(dir_path: str, description: str) -> Path:
    """Validate a directory exists and return a Path object.

    Raises click.ClickException if directory does not exist.
    """
    path = Path(dir_path)
    if not path.exists():
        raise click.ClickException(f"{description} not found: {dir_path}")
    if not path.is_dir():
        raise click.ClickException(f"{description} is not a directory: {dir_path}")
    return path


@click.group()
def cli():
    """dbt-agent — AI-powered dbt pipeline diagnostics"""
    configure_logging()


@cli.command()
@click.option('--log', required=True, help='Path to dbt error log file')
@click.option('--model', required=True, help='Path to the failing .sql model file')
@click.option('--schema', required=True, help='Path to upstream schema file')
def diagnose(log, model, schema):
    """Diagnose a failed dbt pipeline run"""
    from agent.diagnose import diagnose_failure
    from agent.presenters.diagnosis import render_diagnosis

    click.echo("\nAnalyzing pipeline failure...\n")

    try:
        log_path = _validate_file_exists(log, "Error log file")
        model_path = _validate_file_exists(model, "Model file")
        schema_path = _validate_file_exists(schema, "Schema file")

        error_log = log_path.read_text(encoding='utf-8')
        model_sql = model_path.read_text(encoding='utf-8')
        upstream_schema = schema_path.read_text(encoding='utf-8')

        result = diagnose_failure(
            error_log,
            model_sql,
            upstream_schema
        )

        click.echo(render_diagnosis(result))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Failed to diagnose: {e}") from e


@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
def watch(project):
    """Diagnose failures from the last dbt run without making changes."""

    from agent.hooks import RunResultsError, run_post_hook
    from agent.presenters.diagnosis import render_diagnosis

    try:
        project_path = _validate_directory_exists(project, "dbt project")
        report = run_post_hook(project_path)
    except click.ClickException:
        raise
    except RunResultsError as error:
        raise click.ClickException(str(error)) from error

    if not report.diagnoses:
        click.echo("No failed models found in the last dbt run.")
    else:
        click.echo(
            f"Found {len(report.diagnoses)} failed model(s). "
            "Running read-only diagnostics."
        )
        for diagnosis_result in report.diagnoses:
            click.echo()
            click.echo(render_diagnosis(diagnosis_result))

    if report.malformed_entries:
        click.echo(
            f"Ignored {report.malformed_entries} malformed result "
            "entry or entries."
        )


@cli.command()
@click.option(
    '--project',
    required=True,
    help='Stable project identity; snapshot filename is <PROJECT>.json.'
)
@click.option(
    '--db',
    required=True,
    help='Path to your SQLite database file'
)
@click.option(
    '--snapshot-dir',
    required=True,
    type=click.Path(path_type=Path, file_okay=False),
    help='Directory containing persisted schema snapshots.'
)
@click.option(
    '--update-snapshot',
    is_flag=True,
    default=False,
    help='Creates or replaces the persisted schema snapshot.'
)
def diff(project, db, snapshot_dir, update_snapshot):
    """Compare schemas locally and read-only by default.

    An existing SQLite database and snapshot are required for comparison.
    The database is opened read-only, and no notifications are sent.
    """

    from agent.schema_diff import SchemaDiffError, run_schema_diff

    try:
        run_schema_diff(
            project,
            db,
            snapshot_dir,
            update_snapshot=update_snapshot,
        )
    except SchemaDiffError as error:
        raise click.ClickException(str(error)) from error


@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
def analyze(project):
    """Run local, read-only SQL logic analysis. No notifications are sent."""
    from agent.sql_analyzer import analyze_all_models, print_analysis_report

    reports = analyze_all_models(project)

    if not reports:
        return

    critical_count = 0
    for report in reports:
        print_analysis_report(report)

        if report.get("overall_risk") in ("critical", "high"):
            critical_count += 1

    if critical_count > 0:
        print(f"{critical_count} model(s) have critical/high logic bugs.\n")
    else:
        print("All models passed logic analysis.\n")


@cli.command()
@click.option('--project', required=True, help='dbt project name')
@click.option('--db', required=True, help='Path to SQLite database')
def quality(project, db):
    """Run data quality checks — catches row drops, null explosions, duplicates"""
    from agent.quality_checker import run_quality_check

    try:
        _validate_directory_exists(project, "dbt project")
        db_path = Path(db)
        if not db_path.exists():
            raise click.ClickException(f"SQLite database not found: {db}")
        run_quality_check(project, db)
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Quality check failed: {e}") from e


@cli.command()
@click.option('--project', required=True, help='Path to a compiled dbt project')
@click.option('--changed-model', default=None, help='Changed dbt model name (optional)')
@click.option('--diff-base', default='origin/main', show_default=True, help='Git base ref for automatic changed-model detection')
@click.option('--verbose', is_flag=True, help='Include per-model SQL paths and AST findings')
@click.option('--format', 'output_format', default='text', type=click.Choice(['text', 'markdown']))
@click.option('--output', default=None, help='Write the rendered report to this file')
def scan(project, changed_model, diff_base, verbose, output_format, output):
    """Scan compiled dbt model SQL and report optional downstream impact."""
    from agent.dbt_project_scan import (
        format_scan_report,
        format_markdown_scan_report,
        format_verbose_scan_report,
        scan_dbt_project,
    )

    try:
        _validate_directory_exists(project, "dbt project")
        report = scan_dbt_project(project, changed_model, diff_base)
    except click.ClickException:
        raise
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    if output_format == 'markdown':
        rendered = format_markdown_scan_report(report)
    elif verbose:
        rendered = format_verbose_scan_report(report)
    else:
        rendered = format_scan_report(report)
    if output:
        from pathlib import Path
        Path(output).write_text(rendered, encoding='utf-8')
    click.echo(rendered)


@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--dialect', default='sqlite', help='SQL dialect: sqlite, snowflake, bigquery, duckdb')
def ast(project, dialect):
    """Run local, read-only deterministic AST analysis. No notifications are sent."""
    from agent.ast_analyzer import analyze_all_models_ast, run_ast_analysis

    reports = analyze_all_models_ast(project, dialect)

    if not reports:
        return

    total_bugs = 0
    for report in reports:
        bugs = report.get("bugs", [])
        total_bugs += len(bugs)

        risk_emoji = {
            "critical": "🔴", "high": "🟠",
            "medium": "🟡", "low": "🟢", "clean": "✅"
        }.get(report.get("overall_risk", "low"), "⚪")

        print(f"\n{'━' * 55}")
        print(f"  Model:  {report.get('model_name')}")
        print(f"  Risk:   {risk_emoji} {report.get('overall_risk', '').upper()}")
        print(f"  Source: deterministic AST (zero false positives)")
        print(f"  Safe:   {'Yes' if report.get('safe_to_run') else 'NO'}")
        print(f"{'━' * 55}")
        print(f"\nSummary\n  {report.get('summary')}")

        if not bugs:
            print("\nClean.\n")
            continue

        print(f"\nFound {len(bugs)} issue(s):\n")
        for i, bug in enumerate(bugs, 1):
            sev = bug.get("severity", "low")
            conf = bug.get("confidence", "")
            conf_str = "high confidence" if conf == "high" else "medium confidence"

            print(f"  {i}. [{sev.upper()}] {bug.get('category')}")
            print(f"     Confidence: {conf_str}")
            print(f"     SQL:    {bug.get('line_reference')}")
            print(f"     Bug:    {bug.get('description')}")
            print(f"     Impact: {bug.get('impact')}")
            print(f"     Fix:    {bug.get('fix')}")
            print()

        print(f"  Data loss risk: {'YES' if report.get('data_loss_risk') else 'No'}")
        print()

    print(f"\n{'━'*55}")
    print(f"  Total bugs found: {total_bugs}")
    print(f"  Detection method: deterministic AST — no LLM, no false positives")
    print(f"{'━'*55}\n")

@cli.command(
    name="pr_guard",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option(
    '--changed-files',
    multiple=True,
    help='SQL model file(s) changed in this PR. May also be followed by multiple paths.'
)
@click.option(
    '--fail-on',
    default='high',
    type=click.Choice(['info', 'low', 'medium', 'high', 'critical'], case_sensitive=False),
    help='Minimum severity that should fail the command.'
)
@click.option('--output', default='.relium/pr_guard_report.md', help='Markdown report output path')
@click.option('--github-comment', is_flag=True, help='Write and post/update a GitHub PR comment when possible')
@click.option('--comment-output', default='.relium/pr_guard_comment.md', help='GitHub PR comment Markdown output path')
@click.pass_context
def pr_guard(ctx, project, changed_files, fail_on, output, github_comment, comment_output):
    """Run static SQL/dbt PR guard checks and write a Markdown report."""
    from agent.pr_guard import PrGuardError, run_pr_guard, terminal_summary

    _validate_directory_exists(project, "dbt project")
    selected_files = list(changed_files) + list(ctx.args)
    try:
        report = run_pr_guard(
            project,
            changed_files=selected_files or None,
            fail_on=fail_on,
            output=output,
            github_comment=github_comment,
            comment_output=comment_output,
        )
    except PrGuardError as e:
        raise click.ClickException(str(e)) from e
    click.echo(terminal_summary(report))
    if github_comment:
        status = report.get("github_comment_status", {})
        if status.get("reason") == "missing_environment":
            click.echo("GitHub environment not detected. Comment markdown written locally.")
        elif status.get("posted"):
            click.echo("GitHub PR comment posted/updated.")
        else:
            reason = status.get("reason", "unknown error")
            click.echo(f"GitHub PR comment was not posted: {reason}. Comment markdown written locally.")
    raise click.exceptions.Exit(report["exit_code"])

@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--table', required=True, help='The upstream table that changed')
@click.option('--columns', default=None, help='Comma-separated list of changed columns (optional)')
def blast(project, table, columns):
    """Show which models break when an upstream table changes"""
    from agent.blast_radius import run_blast_radius
    run_blast_radius(project, table, columns)


@cli.command(name="demo-pipeline")
@click.option(
    '--scenario',
    default='normal',
    show_default=True,
    type=click.Choice(
        ['normal', 'row-drop', 'duplicate-spike', 'freshness-regression'],
        case_sensitive=False,
    ),
    help='Deterministic demo scenario to run.',
)
@click.option('--decision', is_flag=True, help='Show the internal Decision View.')
def demo_pipeline(scenario, decision):
    """Run Relium's local validation demo pipeline end to end."""
    from agent.demo_pipeline import run_demo_pipeline
    from agent.pipeline_validation_report import write_pipeline_validation_report
    from agent.presentation import render_cli

    result = run_demo_pipeline(scenario=scenario)
    write_pipeline_validation_report(result)
    click.echo(f"Slack alert sent: {'YES' if result['slack_sent'] else 'NO'}")
    click.echo(result["report_text"])
    if decision:
        click.echo()
        click.echo(render_cli(result["incident"]))


@cli.command(name="pr-review-demo")
@click.option('--output', default=None, help='Write the review Markdown to this file')
def pr_review_demo(output):
    """Render a deterministic GitHub PR Guard review locally."""
    from pathlib import Path

    from agent.decision_engine import DeploymentDecision
    from agent.github_pr_guard import build_pr_review, render_pr_review_markdown
    from agent.incident import Incident
    from agent.signals import Severity, Signal

    incident = Incident(
        incident_id="INC-PR-DEMO",
        health=42,
        decision=DeploymentDecision.BLOCK,
        severity=Severity.CRITICAL,
        confidence=91,
        root_cause="Cross join detected in fct_orders.",
        recommendation="Review the join logic before deployment.",
        affected_models=["fct_orders", "dashboard_revenue"],
        signals=[
            Signal(
                "ast",
                Severity.CRITICAL,
                95,
                -70,
                reasons=["Cross join detected"],
                metadata={"model_name": "fct_orders", "rule": "CROSS_JOIN"},
            ),
            Signal(
                "metadata_checks",
                Severity.HIGH,
                85,
                -30,
                reasons=["Duplicate count increased"],
                metadata={
                    "model_name": "dashboard_revenue",
                    "duplicate_count": 7,
                },
            ),
            Signal(
                "business_metrics",
                Severity.HIGH,
                95,
                -35,
                reasons=["High severity metric spike detected"],
                metadata={
                    "model_name": "fulfillment_operations",
                    "metrics": {
                        "failed_pickups": 17,
                        "mis_sorts": 14,
                        "overflow_avalanches": 7,
                    },
                    "baseline": {
                        "failed_pickups": 5,
                        "mis_sorts": 5,
                        "overflow_avalanches": 4,
                    },
                    "spike_percentages": {
                        "failed_pickups": 240.0,
                        "mis_sorts": 180.0,
                        "overflow_avalanches": 75.0,
                    },
                },
            ),
        ],
        metadata={"source": "pr-review-demo"},
    )
    review = build_pr_review(incident)
    markdown = render_pr_review_markdown(review)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        click.echo(f"PR review written to {output}")
        return
    click.echo(markdown)


@cli.command(name="init-baseline")
@click.option(
    "--dbt-manifest",
    required=True,
    help="Path to a dbt manifest.json artifact.",
)
@click.option(
    "--history-path",
    default=".relium/deployment_history.json",
    show_default=True,
    help="Path to Relium deployment history JSON.",
)
@click.option(
    "--deployment-id",
    default="production-baseline",
    show_default=True,
    help="Deployment identifier for the trusted production baseline.",
)
def init_baseline_command(dbt_manifest, history_path, deployment_id):
    """Initialize Relium history from a trusted production manifest."""
    from agent.baseline import initialize_production_baseline

    try:
        result = initialize_production_baseline(
            dbt_manifest_path=dbt_manifest,
            history_path=history_path,
            deployment_id=deployment_id,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    click.echo("Baseline snapshot initialized")
    click.echo(f"Snapshot ID: {result.snapshot_id}")
    click.echo(f"Deployment ID: {result.deployment_id}")
    click.echo(f"Models: {result.model_count}")
    click.echo(f"Discovered KPIs: {result.kpi_count}")
    click.echo(f"History Path: {result.history_path}")


@cli.command(name="review-deployment")
@click.option(
    "--project-context",
    help="Path to a JSON file containing project context.",
)
@click.option(
    "--dbt-manifest",
    default=None,
    help="Path to a dbt manifest.json artifact.",
)
@click.option(
    "--changed-model",
    "changed_models",
    multiple=True,
    help="Changed model name. May be provided more than once.",
)
@click.option(
    "--changed-file",
    "changed_files",
    multiple=True,
    help="Changed file path. May be provided more than once.",
)
@click.option(
    "--history-path",
    default=".relium/deployment_history.json",
    show_default=True,
    help="Path to Relium deployment history JSON.",
)
@click.option(
    "--outcomes-path",
    default=".relium/deployment_outcomes.json",
    show_default=True,
    help="Path to Relium deployment outcomes JSON.",
)
@click.option("--deployment-id", default=None, help="Deployment identifier.")
@click.option("--auto-record", is_flag=True, help="Record accepted snapshots.")
@click.option(
    "--allow-blocked-recording",
    is_flag=True,
    help="Allow BLOCK deployment snapshots to be recorded.",
)
@click.option(
    "--enforcement-mode",
    default="shadow",
    show_default=True,
    type=click.Choice(["shadow", "enforce"]),
    help=(
        "CI behavior: shadow keeps all decisions advisory; enforce exits "
        "nonzero only for BLOCK. WARN remains advisory in both modes."
    ),
)
@click.option("--output", default=None, help="Write rendered review to this file.")
@click.option(
    "--format",
    "output_format",
    default="cli",
    show_default=True,
    type=click.Choice(["cli", "markdown", "json"]),
    help="Rendered output format.",
)
def review_deployment_command(
    project_context,
    dbt_manifest,
    changed_models,
    changed_files,
    history_path,
    outcomes_path,
    deployment_id,
    auto_record,
    allow_blocked_recording,
    enforcement_mode,
    output,
    output_format,
):
    """Run a history-aware Relium deployment review."""
    import json
    from pathlib import Path

    from agent.dbt_context import load_manifest_from_path
    from agent.deployment_review_service import (
        _review_project_context_change,
        review_manifest_change,
    )

    if changed_files and not dbt_manifest:
        raise click.ClickException("--changed-file requires --dbt-manifest.")

    if bool(project_context) == bool(dbt_manifest):
        raise click.ClickException(
            "Exactly one of --project-context or --dbt-manifest is required."
        )

    try:
        if dbt_manifest:
            manifest = load_manifest_from_path(dbt_manifest)
            service_result = review_manifest_change(
                manifest=manifest,
                changed_files=list(changed_files),
                changed_models=list(changed_models),
                deployment_id=deployment_id,
                history_path=history_path,
                outcomes_path=outcomes_path,
                auto_record=auto_record,
                allow_blocked_recording=allow_blocked_recording,
            )
        else:
            context_payload = _load_review_project_context(project_context, None)
            resolved_changed_models = _ordered_unique(list(changed_models or []))
            if not resolved_changed_models:
                raise ValueError("At least one --changed-model is required.")
            service_result = _review_project_context_change(
                project_context=context_payload,
                changed_files=[],
                changed_models=resolved_changed_models,
                deployment_id=deployment_id,
                history_path=history_path,
                outcomes_path=outcomes_path,
                auto_record=auto_record,
                allow_blocked_recording=allow_blocked_recording,
            )
    except ValueError as error:
        message = str(error)
        if message == "At least one changed model is required.":
            if changed_files:
                message = (
                    "At least one changed model is required after applying "
                    "--changed-model and --changed-file."
                )
            else:
                message = "At least one --changed-model is required."
        raise click.ClickException(message) from error

    if output_format == "json":
        payload = dict(service_result["incident"])
        payload["deployment_lifecycle"] = dict(
            service_result["deployment_lifecycle"]
        )
        rendered = json.dumps(payload, indent=2, sort_keys=True)
    else:
        rendered = service_result["rendered"][output_format]

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        click.echo(f"Deployment review written to {output}")
    else:
        click.echo(rendered)

    if enforcement_mode == "enforce" and service_result["decision"] == "BLOCK":
        raise click.exceptions.Exit(1)


@cli.command(name="backtest-deployment")
@click.option(
    "--dbt-manifest",
    required=True,
    help="Path to the historical deployment dbt manifest.json artifact.",
)
@click.option(
    "--baseline-manifest",
    default=None,
    help="Path to the trusted production manifest before the historical deployment.",
)
@click.option(
    "--changed-model",
    "changed_models",
    multiple=True,
    help="Changed model name in the historical deployment. May be provided more than once.",
)
@click.option(
    "--changed-file",
    "changed_files",
    multiple=True,
    help="Changed file path in the historical deployment. May be provided more than once.",
)
@click.option(
    "--history-path",
    default=".relium/deployment_history.json",
    show_default=True,
    help="Path to Relium deployment history JSON when --baseline-manifest is not provided.",
)
@click.option(
    "--deployment-id",
    default="historical-backtest",
    show_default=True,
    help="Historical deployment identifier.",
)
@click.option("--output", default=None, help="Write rendered backtest to this file.")
@click.option(
    "--format",
    "output_format",
    default="cli",
    show_default=True,
    type=click.Choice(["cli", "markdown", "json"]),
    help="Rendered output format.",
)
def backtest_deployment_command(
    dbt_manifest,
    baseline_manifest,
    changed_models,
    changed_files,
    history_path,
    deployment_id,
    output,
    output_format,
):
    """Replay a historical deployment through Relium's review logic."""
    from pathlib import Path

    from agent.backtest import backtest_deployment

    resolved_changed_models = _review_deployment_changed_models(
        changed_models=changed_models,
        changed_files=changed_files,
        dbt_manifest=dbt_manifest,
    )
    if not resolved_changed_models:
        raise click.ClickException(
            "At least one --changed-model or --changed-file is required for backtest."
        )

    try:
        result = backtest_deployment(
            dbt_manifest_path=dbt_manifest,
            baseline_manifest_path=baseline_manifest,
            history_path=history_path,
            changed_models=resolved_changed_models,
            deployment_id=deployment_id,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    rendered = _render_backtest_result(result, output_format)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        click.echo(f"Backtest written to {output}")
        return

    click.echo(rendered)


@cli.command(name="record-outcome")
@click.option("--deployment-id", required=True, help="Reviewed deployment identifier.")
@click.option(
    "--decision",
    required=True,
    type=click.Choice(["ALLOW", "WARN", "BLOCK"], case_sensitive=False),
    help="Deployment decision that Relium made.",
)
@click.option("--outcome", required=True, help="Observed deployment outcome.")
@click.option("--snapshot-id", default=None, help="Related deployment snapshot identifier.")
@click.option("--notes", default=None, help="Optional human notes about the outcome.")
@click.option(
    "--outcomes-path",
    default=".relium/deployment_outcomes.json",
    show_default=True,
    help="Path to Relium deployment outcomes JSON.",
)
@click.option("--metadata", default=None, help="Optional metadata JSON object.")
def record_outcome_command(
    deployment_id,
    decision,
    outcome,
    snapshot_id,
    notes,
    outcomes_path,
    metadata,
):
    """Record what happened after a deployment decision."""
    import uuid
    from datetime import datetime, timezone

    from agent.deployment_outcomes import DeploymentOutcome, DeploymentOutcomeStore

    try:
        metadata_payload = _load_outcome_metadata(metadata)
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    deployment_outcome = DeploymentOutcome(
        outcome_id=f"outcome-{uuid.uuid4().hex}",
        deployment_id=deployment_id,
        snapshot_id=snapshot_id,
        decision=decision.upper(),
        outcome=outcome,
        created_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
        metadata=metadata_payload,
    )

    DeploymentOutcomeStore(outcomes_path).save_outcome(deployment_outcome)
    click.echo("Outcome recorded")
    click.echo(f"Outcome ID: {deployment_outcome.outcome_id}")
    click.echo(f"Deployment ID: {deployment_outcome.deployment_id}")
    click.echo(f"Decision: {deployment_outcome.decision}")
    click.echo(f"Outcome: {deployment_outcome.outcome}")
    click.echo(f"Outcomes Path: {outcomes_path}")


@cli.command(name="outcome-summary")
@click.option(
    "--outcomes-path",
    default=".relium/deployment_outcomes.json",
    show_default=True,
    help="Path to Relium deployment outcomes JSON.",
)
def outcome_summary_command(outcomes_path):
    """Print a summary of recorded deployment outcomes."""
    from agent.deployment_outcomes import DeploymentOutcomeStore

    summary = DeploymentOutcomeStore(outcomes_path).summarize_outcomes()
    click.echo(_format_outcome_summary(summary))


def _review_deployment_changed_models(changed_models, changed_files, dbt_manifest):
    inferred_models = []
    if changed_files:
        from agent.dbt_changes import load_changed_models_from_paths

        try:
            inferred_models = load_changed_models_from_paths(
                manifest_path=dbt_manifest,
                changed_files=list(changed_files),
            )
        except ValueError as error:
            raise click.ClickException(str(error)) from error

    return _ordered_unique([*list(changed_models or []), *inferred_models])


def _ordered_unique(values):
    unique = []
    seen = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _load_review_project_context(project_context, dbt_manifest):
    if bool(project_context) == bool(dbt_manifest):
        raise click.ClickException(
            "Exactly one of --project-context or --dbt-manifest is required."
        )

    if dbt_manifest:
        from agent.dbt_context import load_project_context_from_manifest_path

        try:
            return load_project_context_from_manifest_path(dbt_manifest)
        except ValueError as error:
            raise click.ClickException(str(error)) from error

    import json
    from pathlib import Path

    context_path = Path(project_context)
    if not context_path.exists():
        raise click.ClickException(
            f"Project context file not found: {project_context}"
        )

    try:
        context_payload = json.loads(context_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise click.ClickException(
            f"Invalid project context JSON: {error}"
        ) from error
    except OSError as error:
        raise click.ClickException(
            f"Could not read project context file: {error}"
        ) from error

    if not isinstance(context_payload, dict):
        raise click.ClickException("Project context JSON must be an object.")

    return context_payload


def _load_outcome_metadata(metadata):
    if metadata is None or not str(metadata).strip():
        return {}

    import json

    try:
        payload = json.loads(metadata)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid metadata JSON: {error}") from error

    if not isinstance(payload, dict):
        raise ValueError("Outcome metadata JSON must be an object.")
    return payload


def _format_outcome_summary(summary):
    return "\n".join(
        [
            "Deployment Outcome Summary",
            f"Total outcomes: {summary['total_outcomes']}",
            f"False positives: {summary['false_positives']}",
            f"Incidents after ALLOW: {summary['incidents_after_allow']}",
            f"Incidents after WARN: {summary['incidents_after_warn']}",
            f"Incidents after BLOCK: {summary['incidents_after_block']}",
            f"Accepted risks: {summary['accepted_risks']}",
            f"Reverted deployments: {summary['reverted_deployments']}",
            f"Blocked deployments: {summary['blocked_deployments']}",
            f"Manually approved deployments: {summary['manually_approved_deployments']}",
        ]
    )


def _render_backtest_result(result, output_format):
    import json

    from agent.presentation import render_backtest_cli, render_backtest_markdown, render_json

    if output_format == "json":
        payload = render_json(result.incident)
        payload["deployment_lifecycle"] = _deployment_lifecycle_metadata(result.review)
        payload["backtest"] = _backtest_metadata(result)
        return json.dumps(payload, indent=2, sort_keys=True)

    if output_format == "markdown":
        return render_backtest_markdown(result)

    return render_backtest_cli(result)


def _backtest_metadata(result):
    lifecycle = _deployment_lifecycle_metadata(result.review)
    return {
        "historical_deployment_id": result.historical_deployment_id,
        "would_have_decision": result.would_have_decision,
        "would_have_health": result.would_have_health,
        "baseline_source": result.baseline_source,
        **lifecycle,
    }


def _backtest_decision_label(decision):
    if str(decision) == "BLOCK":
        return "BLOCK DEPLOYMENT"
    return str(decision)


def _deployment_lifecycle_metadata(result):
    return {
        "previous_snapshot_loaded": bool(result.previous_snapshot_loaded),
        "previous_snapshot_id": _snapshot_id(result.previous_snapshot),
        "current_snapshot_id": _snapshot_id(result.current_snapshot),
        "saved_snapshot_id": result.saved_snapshot_id,
        "history_enabled": bool((result.metadata or {}).get("history_enabled")),
    }


def _snapshot_id(snapshot):
    if not snapshot:
        return None
    if isinstance(snapshot, dict):
        value = snapshot.get("snapshot_id")
    else:
        value = getattr(snapshot, "snapshot_id", None)
    return str(value) if value else None


@cli.command(name="compare-last-run")
@click.option('--db', default=None, help='Path to metadata SQLite database')
@click.option('--project', default=None, help='Project name to compare (optional)')
@click.option('--model', default=None, help='Model name to compare (optional)')
def compare_last_run(db, project, model):
    """Compare the latest model metrics against the previous run."""
    from agent.metadata_drift import compare_last_run as compare_last_run_metrics
    from agent.metadata_drift import format_compare_last_run_report

    try:
        result = compare_last_run_metrics(
            db_path=db,
            project_name=project,
            model_name=model,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    click.echo(format_compare_last_run_report(result))

@cli.command(name="root_cause")
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--table', required=True, help='Table with the detected anomaly')
@click.option('--anomaly', required=True, help='Anomaly type from quality_checker.py')
@click.option('--message', default="", help='Optional anomaly message from quality_checker.py')
def root_cause(project, table, anomaly, message):
    """Analyze likely root cause using local metadata only"""
    from agent.root_cause_engine import run_root_cause
    run_root_cause(project, table, anomaly, message)

@cli.command()
@click.option('--project', required=True,
              help='Project name for metrics lookup')
@click.option('--table', default=None,
              help='Specific table (optional)')
@click.option('--days', default=7, type=int)
def history(project, table, days):
    """Show historical metrics for a project or table"""
    from agent.metrics_store import (
        get_metric_history, get_schema_change_history,
        get_freshness_history, get_project_summary
    )

    summary = get_project_summary(project)
    print(f"\nRelium — {project} summary\n")
    print(f"  Tables tracked:        {summary['tables_tracked']}")
    print(f"  Schema changes (7d):   {summary['schema_changes_7d']}")
    print(f"  Test failures (7d):    {summary['test_failures_7d']}")
    print(f"  Stale tables (24h):    {summary['stale_tables_24h']}")
    print()

    if table:
        import json

        metric_history = get_metric_history(project, table, days)
        if metric_history:
            latest_metrics = metric_history[-1]
            print(f"  Latest quality metrics for '{table}':\n")
            print(f"    Recorded at:     {latest_metrics['recorded_at']}")
            print(f"    Row count:       {latest_metrics['row_count']}")
            print(f"    Duplicate rows:  {latest_metrics['duplicate_rows']}")

            null_rates = json.loads(latest_metrics.get("null_rates") or "{}")
            if null_rates:
                print("    Null rates:")
                for column, null_rate in null_rates.items():
                    print(f"      - {column}: {null_rate}%")
            print()
        else:
            print(f"  No quality metric history found for '{table}'.\n")

        freshness = get_freshness_history(project, table, days)
        if freshness:
            latest_freshness = freshness[-1]
            print(f"  Latest freshness for '{table}':\n")
            print(f"    Status:          {latest_freshness['status']}")
            print(f"    Freshness col:   {latest_freshness['freshness_col']}")
            print(f"    Last updated:    {latest_freshness['last_updated']}")
            print(
                f"    Hours since:     "
                f"{latest_freshness['hours_since_update']}"
            )
            print()

        changes = get_schema_change_history(project, table)
        if changes:
            print(f"  Schema changes for '{table}':\n")
            for c in changes[:5]:
                print(
                    f"    {c['detected_at'][:10]}  "
                    f"{c['change_type']}  "
                    f"{c['severity']}"
                )


if __name__ == '__main__':
    cli()
