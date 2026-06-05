import json
from pathlib import Path
from dotenv import load_dotenv
from agent.groq_client import call_llm_json

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

SYSTEM_PROMPT = """
You are an expert analytics engineer reviewing production SQL pipelines.
You have deep knowledge of how SQL behaves in Snowflake, BigQuery, and Redshift.

Your job is to find bugs that are SPECIFIC, VERIFIABLE, and IMPACTFUL.
You only flag bugs you are highly confident about.
You never flag patterns that are simply uncommon — only patterns that produce incorrect results.
You always respond with valid JSON only. No explanation outside JSON.
"""


def analyze_sql_logic(model_name: str, sql: str, context: str = "") -> dict:
    """
    Sends SQL to the AI and asks it to find logic errors,
    not just structural ones. Returns a full analysis report.
    """

    prompt = f"""
You are an expert analytics engineer reviewing production SQL.
Your job is to find bugs that are SPECIFIC, VERIFIABLE, and IMPACTFUL.

RULES:
- Only flag bugs you are highly confident about
- Every bug must have a specific SQL clause as evidence
- Never flag a pattern as buggy unless you can explain exactly what wrong data it produces
- Do not flag patterns that are simply uncommon — only flag patterns that produce incorrect results
- If you are not sure, do not flag it
- False positives destroy trust in observability tools — when in doubt, leave it out

## SQL MODEL: {model_name}
{sql}

## ADDITIONAL CONTEXT
{context if context else "No additional context provided."}

Check specifically for these HIGH-VALUE bugs:

1. LEFT JOIN NULLIFIED BY WHERE CLAUSE
   Pattern: LEFT JOIN table t ON ... followed by WHERE t.column = value
   Why dangerous: The WHERE on the right table silently converts LEFT JOIN
   to INNER JOIN. Unmatched rows are dropped silently. No error thrown.
   Example bad output: "LEFT JOIN nullified by WHERE c.is_deleted = 0 —
   customers with no match in raw_customers will be silently excluded."

2. WINDOW FUNCTION OVER AGGREGATED ROWS
   Pattern: LAG/LEAD/ROW_NUMBER over a query that already GROUP BY'd
   Why dangerous: If each partition has only one row after grouping,
   LAG returns NULL for every row. No error. Silent wrong results.
   Example: LAG(SUM(revenue)) OVER (PARTITION BY customer_id ORDER BY MAX(date))
   — each customer has one grouped row, so LAG always returns NULL.

3. NULL EXCLUSION VIA != OPERATOR
   Pattern: WHERE col != 'value'
   Why dangerous: SQL treats NULL != 'value' as UNKNOWN not TRUE.
   NULL rows are silently excluded. Never shows in error logs.

4. INTEGER DIVISION TRUNCATION
   Pattern: integer_col / integer_col without explicit FLOAT cast
   Why dangerous: 10/3 = 3 not 3.33. Revenue averages silently wrong.
   Fix: CAST one side to FLOAT or multiply numerator by 1.0

5. HARDCODED DATE FILTERS ON LIFETIME METRICS
   Pattern: WHERE date_col >= '2024-01-01' in a model named "lifetime" or "all_time"
   Why dangerous: A lifetime value model filtered to a static date is not
   measuring lifetime. Silent scope reduction that grows worse over time.

6. FAN-OUT FROM NON-UNIQUE JOIN KEY
   Pattern: JOIN on a column that may not be unique in the right table
   Why dangerous: One row matches multiple rows, duplicating metrics.
   SUM(revenue) inflates silently. Classic warehouse bug.

7. DIVIDE BY ZERO WITHOUT NULLIF
   Pattern: SUM(x) / COUNT(y) or any division without NULLIF protection
   Why dangerous: Returns NULL silently or crashes depending on warehouse.
   Fix: NULLIF(denominator, 0) to handle zero safely.

8. MISLEADING METRIC DENOMINATORS
   Pattern: total_value / COUNT(DISTINCT days) where days can equal 1
   Why dangerous: If customer ordered on only 1 day, denominator = 1
   and avg_daily_revenue equals total lifetime value. Completely misleading.
   Only flag if you can see the denominator can realistically be 1.

9. AGGREGATE ON POTENTIALLY VARCHAR COLUMN
   Pattern: SUM() or AVG() on columns whose type is ambiguous
   Why dangerous: Returns NULL silently in most warehouses. No type error.
   Only flag if there is real evidence the column could be non-numeric.

10. SLOWLY CHANGING DIMENSION WITHOUT VERSION FILTER
    Pattern: JOIN to a dimension table without filtering to current/active version
    Why dangerous: Historical records multiply current facts.
    Revenue appears inflated. Classic SCD bug.

For each bug you are confident about return exactly:
{{
  "category": "exact category name from above list",
  "severity": "critical | high | medium | low",
  "confidence": "high | medium",
  "line_reference": "the exact SQL clause proving this bug exists",
  "description": "specific and precise — what exact wrong data does this produce",
  "impact": "concrete business impact — what number is wrong and by how much",
  "fix": "exact SQL fix, not vague advice"
}}

CRITICAL RULE: 
- Only include bugs where confidence is high or medium
- If confidence would be low, skip the bug entirely
- Maximum 6 bugs per model — prioritize the most impactful ones
- Do not include duplicate findings for the same root cause

Return JSON:
{{
  "model_name": "{model_name}",
  "overall_risk": "critical | high | medium | low | clean",
  "summary": "one precise sentence describing the most dangerous issue found",
  "bugs": [...],
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

        # Confidence tag
        confidence = bug.get("confidence", "")
        confidence_display = {
            "high":   "✓ high confidence",
            "medium": "~ medium confidence",
        }.get(confidence, "")

        print(f"  {i}. {severity_emoji} [{bug.get('severity', '').upper()}] {bug.get('category', '')}")
        if confidence_display:
            print(f"     Confidence: {confidence_display}")
        print(f"     SQL:    {bug.get('line_reference', 'N/A')}")
        print(f"     Bug:    {bug.get('description', 'N/A')}")
        print(f"     Impact: {bug.get('impact', 'N/A')}")
        print(f"     Fix:    {bug.get('fix', 'N/A')}")
        print()

    print(f"  Data loss risk:       {'⚠️  YES' if report.get('data_loss_risk') else '✅ No'}")
    print(f"  Rows affected:        {report.get('estimated_rows_affected', 'unknown')}")
    print()