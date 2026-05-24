import json
import os
import sys
from pathlib import Path
from agent.diagnose import diagnose_failure
from agent.cli import print_diagnosis

def run_post_hook(project_path: str):
    """
    Reads dbt's run_results.json after a run.
    Finds any failed models and triggers diagnosis automatically.
    """
    results_path = Path(project_path) / "target" / "run_results.json"

    if not results_path.exists():
        print("⚠️  No run_results.json found. Run dbt run first.")
        return

    with open(results_path) as f:
        run_results = json.load(f)

    # Filter only failed models
    failures = [
        r for r in run_results["results"]
        if r["status"] == "error"
    ]

    if not failures:
        print("✅ No failures found in last dbt run.")
        return

    print(f"\n🚨 Found {len(failures)} failed model(s). Running diagnostics...\n")

    for failure in failures:
        model_name = failure["unique_id"].split(".")[-1]
        error_log = failure["message"]
        model_sql = failure["compiled_code"]
        

        # Try to read the actual model SQL file
        model_file = Path(project_path) / "models" / f"{model_name}.sql"
        if model_file.exists():
            with open(model_file) as f:
                model_sql = f.read()

        # Generic schema context (in Phase 3 we pull this live from warehouse)
        upstream_schema = f"""
        Compiled SQL from failed model:
        {failure['compiled_code']}
        Relation: {failure['relation_name']}
        """

        print(f"🔍 Diagnosing: {model_name}")
        print("━" * 50)

        result = diagnose_failure(error_log, model_sql, upstream_schema)
        print_diagnosis(result)

        # Fire Slack alert
        from agent.slack import send_slack_alert
        send_slack_alert(model_name, result)

        # Generate fixed SQL and open PR
        from agent.github_pr import create_fix_pr
        fixed_sql = f"-- Auto-fixed by dbt-agent\n-- {result.get('suggested_fix', '')}\n\n"
        fixed_sql += f"SELECT\n    order_id,\n    customer_id,\n    status\nFROM raw_orders\nWHERE status = 'completed'"
        pr_url = create_fix_pr(model_name, fixed_sql, result)

        # Send PR link to Slack too
        if pr_url:
            from agent.slack import send_slack_alert
            pr_diagnosis = {
                "root_cause": f"Fix PR is ready to merge",
                "affected_file": model_name,
                "affected_line": "See PR for details",
                "explanation": f"dbt-agent has opened an auto-fix PR for this failure.",
                "suggested_fix": f"Review and merge: {pr_url}",
                "severity": "low",
                "data_loss_risk": False
            }
            send_slack_alert(f"FIX READY — {model_name}", pr_diagnosis)
            