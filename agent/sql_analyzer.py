import json
from pathlib import Path
from dotenv import load_dotenv
from agent.groq_client import call_llm_json

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

SYSTEM_PROMPT = """
You are a senior data engineer with 15 years of experience debugging 
production SQL pipelines. You have seen every possible way SQL can be 
technically valid but logically wrong.

Your job is to read SQL models and find bugs that:
- Won't cause crashes (dbt won't catch them)
- Will silently corrupt data or return wrong results
- Would take a human engineer hours to find

You are paranoid, thorough, and you assume nothing is correct until proven.
You always respond with valid JSON only.
"""

def analyze_sql_logic(model_name: str, sql: str, context: str = "") -> dict:
    """
    Sends SQL to the AI and asks it to find logic errors,
    not just structural ones. Returns a full analysis report.
    """

    prompt = f"""
Analyze this dbt SQL model for logic errors, silent failures, and data 
quality risks. This model is called '{model_name}'.

## SQL MODEL
{sql}

## ADDITIONAL CONTEXT
{context if context else "No additional context provided."}

Check for ALL of the following categories of bugs:

1. SILENT DATA LOSS
   - JOINs that silently drop rows (wrong join type, missing nulls)
   - Filters that exclude valid data (wrong operators, off-by-one)
   - DISTINCT that hides duplicates instead of fixing the root cause

2. WRONG AGGREGATIONS  
   - SUM/AVG on columns that might be varchar (returns null silently)
   - GROUP BY missing columns (non-deterministic results)
   - COUNT(*) vs COUNT(column) confusion (nulls handled differently)
   - Integer division truncating decimals

3. NULL HANDLING BUGS
   - WHERE col != 'value' silently excludes NULLs
   - COALESCE used incorrectly
   - NULL comparisons using = instead of IS NULL

4. DATE AND TIME BUGS
   - Off-by-one date ranges (BETWEEN is inclusive on both ends)
   - Timezone issues (comparing timestamps across timezones)
   - Hardcoded dates that will become stale
   - Date truncation errors (truncating to month vs day)

5. WINDOW FUNCTION BUGS
   - Wrong PARTITION BY (too broad or too narrow)
   - Missing ORDER BY in window functions that need it
   - ROW_NUMBER vs RANK vs DENSE_RANK used incorrectly

6. JOIN LOGIC BUGS
   - Cartesian products from missing JOIN conditions
   - Fan-out from one-to-many joins inflating metrics
   - Wrong join key (joining on non-unique columns)
   - Self-joins that duplicate data

7. BUSINESS LOGIC ERRORS
   - Revenue calculations that miss edge cases
   - Status filters that exclude valid states
   - Percentage calculations that don't handle zero denominators

Return a JSON object with exactly this structure:
{{
  "model_name": "{model_name}",
  "overall_risk": "critical | high | medium | low | clean",
  "summary": "one sentence overall assessment",
  "bugs": [
    {{
      "category": "category name from above",
      "severity": "critical | high | medium | low",
      "line_reference": "the specific SQL clause or line with the bug",
      "description": "what the bug is",
      "impact": "what wrong data this produces in plain English",
      "fix": "exact SQL fix"
    }}
  ],
  "data_loss_risk": true or false,
  "estimated_rows_affected": "none | some | significant | all",
  "safe_to_run": true or false
}}

If no bugs found, return bugs as empty array and overall_risk as "clean".
"""

    return call_llm_json(prompt=prompt, system=SYSTEM_PROMPT)


def analyze_all_models(project_path: str) -> list:
    """
    Scans all SQL models in a dbt project and analyzes each one.
    Returns list of analysis reports.
    """
    models_path = Path(project_path) / "models"

    if not models_path.exists():
        print(f"⚠️  No models folder found at {models_path}")
        return []

    sql_files = list(models_path.glob("**/*.sql"))

    if not sql_files:
        print("⚠️  No SQL models found.")
        return []

    print(f"\n🔬 Analyzing {len(sql_files)} SQL model(s) for logic errors...\n")

    reports = []
    for sql_file in sql_files:
        model_name = sql_file.stem
        print(f"  → Checking {model_name}...")

        with open(sql_file) as f:
            sql = f.read()

        report = analyze_sql_logic(model_name, sql)
        reports.append(report)

    return reports


def print_analysis_report(report: dict):
    """Pretty prints a single model analysis report"""

    risk_emoji = {
        "critical": "🔴",
        "high":     "🟠",
        "medium":   "🟡",
        "low":      "🟢",
        "clean":    "✅"
    }.get(report.get("overall_risk", "low"), "⚪")

    print(f"\n{'━' * 55}")
    print(f"  Model: {report.get('model_name', 'unknown')}")
    print(f"  Risk:  {risk_emoji} {report.get('overall_risk', '').upper()}")
    print(f"  Safe to run: {'✅ Yes' if report.get('safe_to_run') else '❌ NO — fix before running'}")
    print(f"{'━' * 55}")

    print(f"\n📋 Summary\n  {report.get('summary', 'N/A')}")

    bugs = report.get("bugs", [])
    if not bugs:
        print("\n✅ No logic errors detected.\n")
        return

    print(f"\n🐛 Found {len(bugs)} issue(s):\n")
    for i, bug in enumerate(bugs, 1):
        severity_emoji = {
            "critical": "🔴",
            "high":     "🟠",
            "medium":   "🟡",
            "low":      "🟢"
        }.get(bug.get("severity", "low"), "⚪")

        print(f"  {i}. {severity_emoji} [{bug.get('severity', '').upper()}] {bug.get('category', '')}")
        print(f"     SQL:    {bug.get('line_reference', 'N/A')}")
        print(f"     Bug:    {bug.get('description', 'N/A')}")
        print(f"     Impact: {bug.get('impact', 'N/A')}")
        print(f"     Fix:    {bug.get('fix', 'N/A')}")
        print()

    print(f"  Data loss risk:       {'⚠️  YES' if report.get('data_loss_risk') else '✅ No'}")
    print(f"  Rows affected:        {report.get('estimated_rows_affected', 'unknown')}")
    print()