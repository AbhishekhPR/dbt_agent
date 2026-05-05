import click
import json
from agent.diagnose import diagnose_failure

@click.group()
def cli():
    """dbt-agent — AI-powered dbt pipeline diagnostics"""
    pass

@cli.command()
@click.option('--log', required=True, help='Path to dbt error log file')
@click.option('--model', required=True, help='Path to the failing .sql model file')
@click.option('--schema', required=True, help='Path to upstream schema file (.txt or .yaml)')
def diagnose(log, model, schema):
    """Diagnose a failed dbt pipeline run"""

    click.echo("\n🔍 Analyzing pipeline failure...\n")

    # Read input files
    with open(log, 'r') as f:
        error_log = f.read()
    with open(model, 'r') as f:
        model_sql = f.read()
    with open(schema, 'r') as f:
        upstream_schema = f.read()

    result = diagnose_failure(error_log, model_sql, upstream_schema)

    # Pretty print the diagnosis
    severity_color = {
        'critical': 'red',
        'high': 'yellow',
        'medium': 'cyan',
        'low': 'green'
    }.get(result.get('severity', 'low'), 'white')

    click.echo("━" * 50)
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
    click.secho(f"  {'YES — act immediately' if risk else 'No immediate data loss risk'}", 
                fg='red' if risk else 'green')

    click.echo("\n" + "━" * 50 + "\n")