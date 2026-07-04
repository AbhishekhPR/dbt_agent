import click


def run_simulation(*args, **kwargs):
    """Lazy simulator entry point, kept patchable for CLI tests."""
    from agent.simulator import run_simulation as _run_simulation

    return _run_simulation(*args, **kwargs)


def print_diagnosis(result: dict):
    """Reusable formatted diagnosis printer"""
    severity_color = {
        'critical': 'red',
        'high': 'yellow',
        'medium': 'cyan',
        'low': 'green'
    }.get(result.get('severity', 'low'), 'white')

    click.secho(f"  SEVERITY: {result['severity'].upper()}", fg=severity_color, bold=True)
    click.echo("━" * 50)

    click.secho("\nRoot Cause", bold=True)
    click.echo(f"  {result['root_cause']}")

    click.secho("\nAffected File", bold=True)
    click.echo(f"  {result['affected_file']}: {result['affected_line']}")

    click.secho("\nExplanation", bold=True)
    click.echo(f"  {result['explanation']}")

    click.secho("\nSuggested Fix", bold=True)
    click.echo(f"  {result['suggested_fix']}")

    click.secho("\n⚠️  Data Loss Risk", bold=True)
    risk = result['data_loss_risk']

    click.secho(
        f"  {'YES — act immediately' if risk else 'No immediate data loss risk'}",
        fg='red' if risk else 'green'
    )

    click.echo("\n" + "━" * 50 + "\n")


@click.group()
def cli():
    """dbt-agent — AI-powered dbt pipeline diagnostics"""
    pass


@cli.command()
@click.option('--log', required=True, help='Path to dbt error log file')
@click.option('--model', required=True, help='Path to the failing .sql model file')
@click.option('--schema', required=True, help='Path to upstream schema file')
def diagnose(log, model, schema):
    """Diagnose a failed dbt pipeline run"""
    from agent.diagnose import diagnose_failure

    click.echo("\nAnalyzing pipeline failure...\n")

    with open(log) as f:
        error_log = f.read()

    with open(model) as f:
        model_sql = f.read()

    with open(schema) as f:
        upstream_schema = f.read()

    result = diagnose_failure(
        error_log,
        model_sql,
        upstream_schema
    )

    print_diagnosis(result)


@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
def watch(project):
    """Auto-diagnose failures from last dbt run"""

    from agent.hooks import run_post_hook

    run_post_hook(project)


@cli.command()
@click.option(
    '--project',
    required=True,
    help='dbt project name (used for snapshot file)'
)
@click.option(
    '--db',
    required=True,
    help='Path to your SQLite database file'
)
def diff(project, db):
    """Detect upstream schema changes before running dbt"""

    from agent.schema_diff import run_schema_diff

    run_schema_diff(project, db)


@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
def analyze(project):
    """Deep SQL logic analysis — catches silent bugs dbt never will"""
    from agent.sql_analyzer import analyze_all_models, print_analysis_report
    from agent.slack import send_slack_alert

    reports = analyze_all_models(project)

    if not reports:
        return

    critical_count = 0
    for report in reports:
        print_analysis_report(report)

        # Alert on Slack for anything high or critical
        if report.get("overall_risk") in ("critical", "high"):
            critical_count += 1
            for bug in report.get("bugs", []):
                if bug.get("severity") in ("critical", "high"):
                    diagnosis = {
                        "root_cause": bug.get("description"),
                        "affected_file": report.get("model_name"),
                        "affected_line": bug.get("line_reference"),
                        "explanation": bug.get("impact"),
                        "suggested_fix": bug.get("fix"),
                        "severity": bug.get("severity"),
                        "data_loss_risk": report.get("data_loss_risk", False)
                    }
                    send_slack_alert(
                        f"LOGIC BUG — {report.get('model_name')}",
                        diagnosis
                    )

    if critical_count > 0:
        print(f"{critical_count} model(s) have critical/high logic bugs — Slack alerted.\n")
    else:
        print("All models passed logic analysis.\n")


@cli.command()
@click.option('--project', required=True, help='dbt project name')
@click.option('--db', required=True, help='Path to SQLite database')
def quality(project, db):
    """Run data quality checks — catches row drops, null explosions, duplicates"""
    from agent.quality_checker import run_quality_check
    run_quality_check(project, db)


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
        report = scan_dbt_project(project, changed_model, diff_base)
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
@click.option('--project', required=True, help='dbt project name')
@click.option('--db', required=True, help='Path to SQLite database')
@click.option('--table', required=True, help='Table to simulate an incident on')
@click.option(
    '--type',
    'anomaly_type',
    required=True,
    type=click.Choice(
        [
            "row_count_drop",
            "row_count_spike",
            "null_explosion",
            "cardinality_explosion",
            "duplicate_explosion",
            "freshness_anomaly",
            "schema_drift_added_column",
            "schema_drift_removed_column",
            "schema_drift_type_change",
        ]
    ),
    help='Simulation type to apply'
)
@click.option('--no-restore', is_flag=True, help='Leave simulated DB and baseline changes in place')
@click.option('--no-sync-baseline', is_flag=True, help='Use existing baseline instead of syncing before simulation')
def simulate(project, db, table, anomaly_type, no_restore, no_sync_baseline):
    """Simulate a local data quality incident and run the quality checker"""
    run_simulation(
        project,
        db,
        table,
        anomaly_type,
        restore_after=not no_restore,
        sync_baseline=not no_sync_baseline,
    )


@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--dialect', default='sqlite', help='SQL dialect: sqlite, snowflake, bigquery, duckdb')
def ast(project, dialect):
    """Deterministic AST analysis — zero false positives, instant results"""
    from agent.ast_analyzer import analyze_all_models_ast, run_ast_analysis
    from agent.cli import print_diagnosis
    from agent.slack import send_slack_alert

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

            if bug.get("severity") in ("critical", "high"):
                diagnosis = {
                    "root_cause": bug.get("description"),
                    "affected_file": report.get("model_name"),
                    "affected_line": bug.get("line_reference"),
                    "explanation": bug.get("impact"),
                    "suggested_fix": bug.get("fix"),
                    "severity": bug.get("severity"),
                    "data_loss_risk": report.get("data_loss_risk", False)
                }
                send_slack_alert(
                    f"AST BUG — {report.get('model_name')}",
                    diagnosis
                )

        print(f"  Data loss risk: {'YES' if report.get('data_loss_risk') else 'No'}")
        print()

        # Slack alert for high/critical
        for bug in bugs:
            if bug.get("severity") in ("critical", "high"):
                diagnosis = {
                    "root_cause": bug.get("description"),
                    "affected_file": report.get("model_name"),
                    "affected_line": bug.get("line_reference"),
                    "explanation": bug.get("impact"),
                    "suggested_fix": bug.get("fix"),
                    "severity": bug.get("severity"),
                    "data_loss_risk": report.get("data_loss_risk", False)
                }
                send_slack_alert(
                    f"AST BUG — {report.get('model_name')}",
                    diagnosis
                )

    print(f"\n{'━'*55}")
    print(f"  Total bugs found: {total_bugs}")
    print(f"  Detection method: deterministic AST — no LLM, no false positives")
    print(f"{'━'*55}\n")

@cli.command(name="sql_metadata")
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--dialect', default='sqlite', help='SQL dialect: sqlite, snowflake, bigquery, duckdb')
def sql_metadata(project, dialect):
    """Extract SQL metadata from dbt models and save it to SQLite."""
    from agent.sql_metadata_extractor import extract_sql_metadata

    reports = extract_sql_metadata(project, dialect)
    if not reports:
        print("No SQL models found or metadata extraction produced no output.")
        return

    print(f"\nSQL metadata extracted for {len(reports)} model(s). Stored in metadata.db")
    for report in reports:
        print(f"  - {report.get('model_name')} ({len(report.get('source_tables', []))} source tables, {len(report.get('joins', []))} joins)")

@cli.command(name="sql_risks")
@click.option('--project', required=True, help='Path to your dbt project folder')
def sql_risks(project):
    """Scan dbt model SQL for risky transformation logic."""
    from agent.sql_risk_detector import detect_sql_risks

    print("Scanning SQL models for risky transformation logic...\n")
    risks = detect_sql_risks(project)
    print(f"{len(risks)} risk(s) found.\n")
    for risk in risks:
        print(f"[{risk['severity'].upper()}] {risk['model']}")
        print(risk["message"])
        print(f"Evidence: {risk['evidence']}")
        print(f"Recommendation: {risk['recommendation']}")
        print()

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
    from agent.pr_guard import run_pr_guard, terminal_summary

    selected_files = list(changed_files) + list(ctx.args)
    report = run_pr_guard(
        project,
        changed_files=selected_files or None,
        fail_on=fail_on,
        output=output,
        github_comment=github_comment,
        comment_output=comment_output,
    )
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
@click.option("--deployment-id", default=None, help="Deployment identifier.")
@click.option("--auto-record", is_flag=True, help="Record accepted snapshots.")
@click.option(
    "--allow-blocked-recording",
    is_flag=True,
    help="Allow BLOCK deployment snapshots to be recorded.",
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
    deployment_id,
    auto_record,
    allow_blocked_recording,
    output,
    output_format,
):
    """Run a history-aware Relium deployment review."""
    from pathlib import Path

    from agent.deployment_history import DeploymentHistoryStore
    from agent.deployment_lifecycle import review_deployment

    if changed_files and not dbt_manifest:
        raise click.ClickException("--changed-file requires --dbt-manifest.")

    context_payload = _load_review_project_context(project_context, dbt_manifest)
    resolved_changed_models = _review_deployment_changed_models(
        changed_models=changed_models,
        changed_files=changed_files,
        dbt_manifest=dbt_manifest,
    )

    if not resolved_changed_models:
        if changed_files:
            raise click.ClickException(
                "At least one changed model is required after applying "
                "--changed-model and --changed-file."
            )
        raise click.ClickException("At least one --changed-model is required.")

    history_store = DeploymentHistoryStore(history_path)
    result = review_deployment(
        changed_models=[
            {
                "model_name": Path(model).stem,
                "sql": f"select * from {Path(model).stem}",
            }
            for model in resolved_changed_models
        ],
        project_context=context_payload,
        history_store=history_store,
        deployment_id=deployment_id,
        auto_record=auto_record,
        allow_blocked_recording=allow_blocked_recording,
    )
    rendered = _render_deployment_review_result(result, output_format)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        click.echo(f"Deployment review written to {output}")
        return

    click.echo(rendered)


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


def _render_deployment_review_result(result, output_format):
    import json

    from agent.presentation import render_cli, render_json, render_markdown

    if output_format == "json":
        payload = render_json(result.incident)
        payload["deployment_lifecycle"] = _deployment_lifecycle_metadata(result)
        return json.dumps(payload, indent=2, sort_keys=True)

    if output_format == "markdown":
        rendered = render_markdown(result.incident)
        status_lines = _deployment_review_status_lines(result, markdown=True)
        return f"{rendered}\n\n## Deployment History\n" + "\n".join(status_lines)

    rendered = render_cli(result.incident)
    status_lines = _deployment_review_status_lines(result, markdown=False)
    return f"{rendered}\n\nDeployment History\n" + "\n".join(status_lines)


def _deployment_lifecycle_metadata(result):
    return {
        "previous_snapshot_loaded": bool(result.previous_snapshot_loaded),
        "previous_snapshot_id": _snapshot_id(result.previous_snapshot),
        "current_snapshot_id": _snapshot_id(result.current_snapshot),
        "saved_snapshot_id": result.saved_snapshot_id,
        "history_enabled": bool((result.metadata or {}).get("history_enabled")),
    }


def _deployment_review_status_lines(result, *, markdown):
    loaded = "YES" if result.previous_snapshot_loaded else "NO"
    previous_snapshot_id = _snapshot_id(result.previous_snapshot)
    lines = []
    if markdown:
        lines.append(f"**Previous Snapshot Loaded:** {loaded}")
        if result.previous_snapshot_loaded:
            lines.append(f"**Previous Snapshot:** {previous_snapshot_id or 'None'}")
        if result.saved_snapshot_id:
            lines.append(f"**Saved Snapshot:** {result.saved_snapshot_id}")
        return lines

    lines.append(f"Previous Snapshot Loaded: {loaded}")
    if result.previous_snapshot_loaded:
        lines.append(f"Previous Snapshot: {previous_snapshot_id or 'None'}")
    if result.saved_snapshot_id:
        lines.append(f"Saved Snapshot: {result.saved_snapshot_id}")
    return lines


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
@click.option('--project', required=True)
@click.option('--db', required=True)
@click.option('--stale-hours', default=6, type=float,
              help='Hours before table is considered stale')
@click.option('--critical-hours', default=24, type=float,
              help='Hours before table is considered critical')
def freshness(project, db, stale_hours, critical_hours):
    """Check how recently each table was updated"""
    from agent.freshness import run_freshness_check
    thresholds = {
        "stale": stale_hours,
        "critical": critical_hours
    }
    run_freshness_check(project, db, thresholds)


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
