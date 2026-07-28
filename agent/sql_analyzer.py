import re
import json
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"


def _extract_table_aliases(sql: str) -> dict:
    """Return mapping of aliases to table names found in JOIN clauses."""
    aliases = {}
    # matches: JOIN table_name [AS] alias ON
    for m in re.finditer(r"join\s+([\w\.\"]+)(?:\s+(?:as\s+)?([\w_]+))?\s+on", sql, re.I):
        table = m.group(1)
        alias = m.group(2) or table.split(".")[-1].strip('"')
        aliases[alias] = table
    return aliases


def _find_select_star(sql: str) -> bool:
    return bool(re.search(r"\bselect\s+\*", sql, re.I))


def _find_missing_join_condition(sql: str) -> bool:
    # naive: JOIN without ON nearby
    return bool(re.search(r"\bjoin\b(?![\s\S]{0,120}?on)", sql, re.I))


def _find_left_join_nullified(sql: str) -> list:
    findings = []
    # find left joins with alias and then WHERE that references alias columns
    for m in re.finditer(r"left\s+join\s+([\w\.\"]+)(?:\s+(?:as\s+)?([\w_]+))?\s+on\s+([\s\S]{0,200}?)\b", sql, re.I):
        alias = m.group(2) or m.group(1).split(".")[-1].strip('"')
        # look for WHERE ... alias.column ... = ...
        where_match = re.search(r"where\s+([\s\S]+)$", sql, re.I)
        if where_match and re.search(rf"\b{re.escape(alias)}\.[\w_]+\b", where_match.group(1)):
            findings.append({"alias": alias})
    return findings


def _find_risky_count_after_join(sql: str) -> bool:
    return bool(re.search(r"count\s*\(\s*\*\s*\)" , sql, re.I) and re.search(r"\bjoin\b", sql, re.I))


def _extract_selected_columns(sql: str) -> list:
    m = re.search(r"select\s+([\s\S]+?)\s+from\b", sql, re.I)
    if not m:
        return []
    cols = m.group(1)
    # split on commas not inside parentheses
    parts = re.split(r",\s*(?![^()]*\))", cols)
    cleaned = [p.strip() for p in parts if p.strip()]
    return cleaned


def _columns_from_context(context: str) -> set:
    # context may be a simple newline list or JSON-like. Try simple heuristics.
    cols = set()
    try:
        data = json.loads(context)
        if isinstance(data, dict):
            for k in data.keys():
                cols.add(k)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    cols.add(item)
                elif isinstance(item, dict) and "name" in item:
                    cols.add(item["name"])
    except Exception:
        # fall back to regex: find word tokens with possible dot notation
        for c in re.findall(r"\b[\w_]+\b", context):
            cols.add(c)
    return cols


def analyze_sql_logic(model_name: str, sql: str, context: str = "") -> dict:
    """Deterministic, local SQL checks that do not call any external service.

    Returns a report with a list of findings. Each finding contains:
    - rule_id, severity, title, evidence, why_it_matters, recommendation, confidence
    """
    findings = []

    if _find_select_star(sql):
        findings.append({
            "rule_id": "select_star",
            "severity": "medium",
            "title": "SELECT * used",
            "evidence": "SELECT * present in query",
            "why_it_matters": "SELECT * can return unexpected columns and hides schema changes",
            "recommendation": "Specify explicit columns in SELECT to guarantee schema",
            "confidence": "high"
        })

    if _find_missing_join_condition(sql):
        findings.append({
            "rule_id": "missing_join_on",
            "severity": "critical",
            "title": "JOIN without ON condition",
            "evidence": "JOIN found with no nearby ON clause",
            "why_it_matters": "A JOIN without ON can produce Cartesian products, massively inflating rows",
            "recommendation": "Add an explicit ON clause or use CROSS JOIN intentionally",
            "confidence": "high"
        })

    lj = _find_left_join_nullified(sql)
    if lj:
        findings.append({
            "rule_id": "left_join_nullified_by_where",
            "severity": "high",
            "title": "LEFT JOIN possibly nullified by WHERE",
            "evidence": f"LEFT JOIN on alias(es) {[x['alias'] for x in lj]} with WHERE referencing those aliases",
            "why_it_matters": "A WHERE filtering on right-side columns can convert LEFT JOIN to INNER JOIN, dropping rows",
            "recommendation": "Move filters into the JOIN condition or use IS NOT DISTINCT FROM semantics",
            "confidence": "medium"
        })

    if _find_risky_count_after_join(sql):
        findings.append({
            "rule_id": "count_after_join",
            "severity": "high",
            "title": "COUNT(*) after JOINs may overcount",
            "evidence": "COUNT(*) used in query that includes JOINs",
            "why_it_matters": "Joining non-unique keys can inflate aggregate counts",
            "recommendation": "Consider COUNT(DISTINCT key) or dedup upstream",
            "confidence": "medium"
        })

    # check selected columns against provided schema context for missing/renamed columns
    ctx_cols = _columns_from_context(context) if context else set()
    sel_cols = _extract_selected_columns(sql)
    referenced = set()
    for col in sel_cols:
        # pick simple word occurrences as referenced columns
        for token in re.findall(r"[\w_]+", col):
            referenced.add(token)

    missing = []
    if ctx_cols:
        for r in referenced:
            if r.lower() not in {c.lower() for c in ctx_cols} and r.lower() not in ("select","from","where","count","sum","avg","case"):
                missing.append(r)
        if missing:
            findings.append({
                "rule_id": "missing_or_renamed_columns",
                "severity": "high",
                "title": "Referenced columns missing from schema context",
                "evidence": f"Columns referenced in SELECT not found in provided schema: {missing}",
                "why_it_matters": "Renamed or missing columns cause runtime failures or incorrect results",
                "recommendation": "Verify model upstream schema and update column names or mappings",
                "confidence": "medium"
            })

    overall_risk = "clean"
    if any(f["severity"] == "critical" for f in findings):
        overall_risk = "critical"
    elif any(f["severity"] == "high" for f in findings):
        overall_risk = "high"
    elif findings:
        overall_risk = "medium"

    report = {
        "model_name": model_name,
        "overall_risk": overall_risk,
        "summary": f"Found {len(findings)} potential issue(s)",
        "findings": findings,
        "safe_to_run": overall_risk in ("low", "clean")
    }

    return report


def analyze_all_models(project_path: str) -> list:
    """
    Scans all SQL models in a dbt project and analyzes each one.
    Returns list of analysis reports.
    """
    models_path = Path(project_path) / "models"

    if not models_path.exists():
        print(f"No models folder found at {models_path}")
        return []

    sql_files = list(models_path.glob("**/*.sql"))

    if not sql_files:
        print("No SQL models found.")
        return []

    print(f"\nAnalyzing {len(sql_files)} SQL model(s) for logic errors...\n")

    reports = []
    for sql_file in sql_files:
        model_name = sql_file.stem
        print(f"  Checking {model_name}...")

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
    print(f"  Safe to run: {'Yes' if report.get('safe_to_run') else 'NO — fix before running'}")
    print(f"{'━' * 55}")

    print(f"\nSummary\n  {report.get('summary', 'N/A')}")

    bugs = report.get("bugs", [])
    if not bugs:
        print("\nNo logic errors detected.\n")
        return

    print(f"\nFound {len(bugs)} issue(s):\n")
    for i, bug in enumerate(bugs, 1):
        # Confidence tag
        confidence = bug.get("confidence", "")
        confidence_display = {
            "high":   "high confidence",
            "medium": "medium confidence",
        }.get(confidence, "")

        print(f"  {i}. [{bug.get('severity', '').upper()}] {bug.get('category', '')}")
        if confidence_display:
            print(f"     Confidence: {confidence_display}")
        print(f"     SQL:    {bug.get('line_reference', 'N/A')}")
        print(f"     Bug:    {bug.get('description', 'N/A')}")
        print(f"     Impact: {bug.get('impact', 'N/A')}")
        print(f"     Fix:    {bug.get('fix', 'N/A')}")
        print()

    print(f"  Data loss risk:       {'⚠️ YES' if report.get('data_loss_risk') else 'No'}")
    print(f"  Rows affected:        {report.get('estimated_rows_affected', 'unknown')}")
    print()