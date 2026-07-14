import re


def diagnose_failure(error_log: str, model_sql: str, upstream_schema: str) -> dict:
  """Deterministic, local diagnosis of common dbt/SQL failure messages.

  Returns a dict with keys: category, likely_cause, evidence, recommended_fix, severity, confidence
  """
  el = (error_log or "").lower()

  def make(category, cause, evidence, fix, severity, confidence):
    return {
      "category": category,
      "likely_cause": cause,
      "evidence": evidence,
      "recommended_fix": fix,
      "severity": severity,
      "confidence": confidence
    }

  # Column not found
  if re.search(r"column\s+\"?[\w_]+\"?\s+does not exist|column\s+[\w_]+\s+not found|unknown column", el):
    m = re.search(r"column\s+\"?([\w_]+)\"?", error_log, re.I)
    col = m.group(1) if m else None
    return make(
      "column_not_found",
      f"Referenced column {col} not found in upstream schema" if col else "Referenced column not found",
      error_log.strip(),
      "Check upstream model/schema for renamed or missing column; update SQL to match",
      "high",
      "high"
    )

  # Table/relation not found
  if re.search(r"relation\s+\"?[\w\.]+\"?\s+does not exist|table\s+[\w_]+\s+does not exist|unknown relation", el):
    return make(
      "table_not_found",
      "Referenced table or relation does not exist",
      error_log.strip(),
      "Verify table names and that upstream models have run; check database/schema",
      "high",
      "high"
    )

  # Syntax error
  if "syntax error" in el or re.search(r"parse error|syntax error at or near", el):
    return make(
      "syntax_error",
      "SQL syntax error in model",
      error_log.strip(),
      "Inspect the SQL around the reported location; run SQL linter or try executing the statement",
      "high",
      "high"
    )

  # Type mismatch / cast errors
  if re.search(r"cannot cast|invalid input syntax for|type mismatch|cannot convert", el):
    return make(
      "type_mismatch",
      "Data type mismatch or invalid cast",
      error_log.strip(),
      "Check column types in upstream schema and casts in SQL; coerce types explicitly",
      "medium",
      "medium"
    )

  # Permission/access
  if re.search(r"permission denied|access denied|insufficient privileges", el):
    return make(
      "permission_error",
      "Insufficient privileges to access resource",
      error_log.strip(),
      "Verify user/role permissions and credentials for the warehouse",
      "high",
      "high"
    )

  # Ambiguous column
  if re.search(r"column reference.*is ambiguous|ambiguous column", el):
    return make(
      "ambiguous_column",
      "Ambiguous column reference due to multiple tables with same column name",
      error_log.strip(),
      "Prefix column references with table/alias or disambiguate using aliases",
      "medium",
      "high"
    )

  # Division by zero
  if re.search(r"division by zero|divide.*zero", el):
    return make(
      "division_by_zero",
      "Division by zero encountered",
      error_log.strip(),
      "Protect divisions with NULLIF(denominator, 0) or conditional logic",
      "high",
      "high"
    )

  # Null constraint / not-null failure
  if re.search(r"null value in column.*violates not-null constraint|cannot be null|not null constraint", el):
    return make(
      "not_null_violation",
      "NOT NULL constraint violated by incoming data",
      error_log.strip(),
      "Ensure upstream data provides non-null values or adjust schema/transformations",
      "high",
      "high"
    )

  # Fallback unknown error
  return make(
    "unknown_error",
    "Unclassified error — inspect logs",
    error_log.strip(),
    "Review error message and SQL; run the model locally to reproduce",
    "medium",
    "low"
  )