import click
from agent.diagnose import diagnose_failure
from agent.simulator import run_simulation


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
        print(f"  Source: ⚡ deterministic AST (zero false positives)")
        print(f"  Safe:   {'✅ Yes' if report.get('safe_to_run') else '❌ NO'}")
        print(f"{'━' * 55}")
        print(f"\n📋 {report.get('summary')}")

        if not bugs:
            print("\n✅ Clean.\n")
            continue

        print(f"\n🐛 {len(bugs)} issue(s):\n")
        for i, bug in enumerate(bugs, 1):
            sev = bug.get("severity", "low")
            emoji = {"critical":"🔴","high":"🟠","medium":"🟡","low":"🟢"}.get(sev,"⚪")
            conf = bug.get("confidence", "")
            conf_str = "✓ high confidence" if conf == "high" else "~ medium confidence"

            print(f"  {i}. {emoji} [{sev.upper()}] {bug.get('category')}")
            print(f"     Confidence: {conf_str}")
            print(f"     SQL:    {bug.get('line_reference')}")
            print(f"     Bug:    {bug.get('description')}")
            print(f"     Impact: {bug.get('impact')}")
            print(f"     Fix:    {bug.get('fix')}")
            print()

        print(f"  Data loss risk: {'⚠️  YES' if report.get('data_loss_risk') else '✅ No'}")
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
    print(f"  Detection method: ⚡ deterministic AST — no LLM, no false positives")
    print(f"{'━'*55}\n")

@cli.command(name="sql_metadata")
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--dialect', default='sqlite', help='SQL dialect: sqlite, snowflake, bigquery, duckdb')
def sql_metadata(project, dialect):
    """Extract SQL metadata from dbt models and save it to SQLite."""
    from agent.sql_metadata_extractor import extract_sql_metadata

    reports = extract_sql_metadata(project, dialect)
    if not reports:
        print("⚠️  No SQL models found or metadata extraction produced no output.")
        return

    print(f"\n📦 SQL metadata extracted for {len(reports)} model(s). Stored in metadata_history.db")
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
@click.pass_context
def pr_guard(ctx, project, changed_files, fail_on, output):
    """Run static SQL/dbt PR guard checks and write a Markdown report."""
    from agent.pr_guard import run_pr_guard, terminal_summary

    selected_files = list(changed_files) + list(ctx.args)
    report = run_pr_guard(
        project,
        changed_files=selected_files or None,
        fail_on=fail_on,
        output=output,
    )
    click.echo(terminal_summary(report))
    raise click.exceptions.Exit(report["exit_code"])

@cli.command()
@click.option('--project', required=True, help='Path to your dbt project folder')
@click.option('--table', required=True, help='The upstream table that changed')
@click.option('--columns', default=None, help='Comma-separated list of changed columns (optional)')
def blast(project, table, columns):
    """Show which models break when an upstream table changes"""
    from agent.blast_radius import run_blast_radius
    run_blast_radius(project, table, columns)

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
    print(f"\n📊 Relium — {project} summary\n")
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
