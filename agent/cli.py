import click


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

    click.secho("\n📌 Root Cause", bold=True)
    click.echo(f"  {result['root_cause']}")

    click.secho("\n📁 Affected File", bold=True)
    click.echo(f"  {result['affected_file']} → {result['affected_line']}")

    click.secho("\n💬 Explanation", bold=True)
    click.echo(f"  {result['explanation']}")

    click.secho("\n🔧 Suggested Fix", bold=True)
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

    click.echo("\n🔍 Analyzing pipeline failure...\n")

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
        print(f"🚨 {critical_count} model(s) have critical/high logic bugs — Slack alerted.\n")
    else:
        print("✅ All models passed logic analysis.\n")


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
@click.option('--verbose', is_flag=True, help='Include per-model SQL paths and AST findings')
def scan(project, changed_model, verbose):
    """Scan compiled dbt model SQL and report optional downstream impact."""
    from agent.dbt_project_scan import (
        format_scan_report,
        format_verbose_scan_report,
        scan_dbt_project,
    )

    try:
        report = scan_dbt_project(project, changed_model)
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    if verbose:
        click.echo(format_verbose_scan_report(report))
    else:
        click.echo(format_scan_report(report))

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
def demo_pipeline(scenario):
    """Run Relium's local validation demo pipeline end to end."""
    from agent.demo_pipeline import run_demo_pipeline
    from agent.pipeline_validation_report import write_pipeline_validation_report

    result = run_demo_pipeline(scenario=scenario)
    write_pipeline_validation_report(result)
    click.echo(f"Slack alert sent: {'YES' if result['slack_sent'] else 'NO'}")
    click.echo(result["report_text"])

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
