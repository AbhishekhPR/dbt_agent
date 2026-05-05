import json
from agent.groq_client import call_llm_json

SYSTEM_PROMPT = """
You are an expert dbt (data build tool) data engineer.
Your job is to analyze a failed dbt pipeline and return a precise diagnosis.
You always respond with valid JSON only. No explanation outside the JSON.
"""

def diagnose_failure(error_log: str, model_sql: str, upstream_schema: str) -> dict:
    """
    Takes a dbt error log, the failing SQL model, and the upstream schema.
    Returns a structured diagnosis as a dict.
    """

    prompt = f"""
A dbt pipeline has failed. Analyze the inputs below and return a JSON diagnosis.

## ERROR LOG
{error_log}

## FAILING MODEL SQL
{model_sql}

## UPSTREAM SCHEMA (table columns and types)
{upstream_schema}

Return a JSON object with exactly these keys:
{{
  "root_cause": "one sentence — what actually caused this failure",
  "affected_file": "which model file is broken",
  "affected_line": "the approximate line number or SQL clause causing the issue",
  "explanation": "plain English explanation a non-technical stakeholder can understand",
  "suggested_fix": "the exact SQL or code change needed to fix it",
  "severity": "critical | high | medium | low",
  "data_loss_risk": true or false
}}
"""

    return call_llm_json(prompt=prompt, system=SYSTEM_PROMPT)