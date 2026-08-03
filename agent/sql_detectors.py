"""Supported SQL risk detectors with explicit evidence and limitations."""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import expressions as exp


_REGISTRY = {
    "B05_CROSS_JOIN": {"owner": "sql-reliability", "severity": "high", "supported_dialects": ["sqlite", "postgres", "snowflake", "bigquery"], "limitations": "Cannot infer business intent for every Cartesian operation; approved contracts suppress known one-row parameter relations."},
    "B08_DUPLICATE_GENERATING_JOIN": {"owner": "sql-reliability", "severity": "high", "supported_dialects": ["sqlite", "postgres", "snowflake", "bigquery"], "limitations": "Requires declared grain, relationship, or uniqueness metadata; static SQL alone cannot prove runtime cardinality."},
    "B09_GRAIN_CHANGING_AGGREGATION": {"owner": "sql-reliability", "severity": "high", "supported_dialects": ["sqlite", "postgres", "snowflake", "bigquery"], "limitations": "Only compares parseable GROUP BY and declared grain; resulting data comparison remains authoritative for observed behavior."},
    "B10_MISSING_DEDUPLICATION": {"owner": "sql-reliability", "severity": "high", "supported_dialects": ["sqlite", "postgres", "snowflake", "bigquery"], "limitations": "Recognizes common window/QUALIFY patterns but does not require one syntax; upstream uniqueness evidence is needed."},
    "B11_UNSAFE_INCREMENTAL_WATERMARK": {"owner": "sql-reliability", "severity": "high", "supported_dialects": ["sqlite", "postgres", "snowflake", "bigquery"], "limitations": "Cannot validate source clock semantics or late-arrival behavior without declared contracts and metadata."},
    "C06_LEFT_TO_INNER_JOIN": {"owner": "sql-reliability", "severity": "high", "supported_dialects": ["sqlite", "postgres", "snowflake", "bigquery"], "limitations": "Structural comparison cannot prove all optimizer-equivalent rewrites; downstream metadata comparison is required."},
}


def detector_registry() -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in _REGISTRY.items()}


def run_sql_detectors(sql: str, *, model_name: str, base_sql: str | None = None, metadata: dict[str, Any] | None = None, dialect: str | None = None, source: str = "raw") -> list[dict[str, Any]]:
    metadata = metadata or {}
    text = str(sql or "")
    findings = []
    finding = _cross_join(text, metadata)
    if finding:
        findings.append(finding)
    finding = _duplicate_join(text, metadata)
    if finding:
        findings.append(finding)
    if base_sql:
        for finder in (_grain_change, _dedup_removed, _watermark_weakened, _left_to_inner):
            finding = finder(str(base_sql), text, metadata)
            if finding:
                findings.append(finding)
    for finding in findings:
        supported = not dialect or str(dialect).lower() in {
            value.lower() for value in _REGISTRY[finding["finding_type"]]["supported_dialects"]
        }
        finding.update({"model_name": model_name, "source": source, "dialect": dialect, "supported": supported, "status": "EVALUATED" if supported else "UNSUPPORTED"})
    return findings


def _finding(kind: str, evidence: str, remediation: str) -> dict[str, Any]:
    spec = _REGISTRY[kind]
    return {"finding_type": kind, "severity": spec["severity"], "evidence": evidence, "remediation": remediation, "owner": spec["owner"], "limitations": spec["limitations"]}


def _relation_names(sql: str) -> list[str]:
    return [name.lower().strip('"') for name in re.findall(r'\b(?:from|join)\s+([\w\.\"]+)', sql, re.I)]


def _cross_join(sql: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    approved = {str(value).lower() for value in metadata.get("approved_cartesian_relations", [])}
    explicit = re.search(r'\bcross\s+join\s+([\w\.\"]+)', sql, re.I)
    implicit = bool(re.search(r'\bfrom\s+[\w\.\"]+(?:\s+\w+)?\s*,\s*[\w\.\"]+', sql, re.I))
    if explicit and explicit.group(1).lower().strip('"') in approved:
        return None
    if explicit or implicit:
        return _finding("B05_CROSS_JOIN", "Cartesian relation without a selective predicate", "Declare the intentional Cartesian contract or add a meaningful join predicate.")
    return None


def _duplicate_join(sql: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    relationships = metadata.get("relationships", {})
    unique_keys = metadata.get("unique_keys", {})
    for table in _relation_names(sql)[1:]:
        relationship = str(relationships.get(table, "")).lower()
        keys = unique_keys.get(table)
        if relationship in {"one-to-many", "many-to-many"} or keys == []:
            if re.search(r"\bjoin\s+" + re.escape(table) + r"\b", sql, re.I):
                return _finding("B08_DUPLICATE_GENERATING_JOIN", f"Join to {table} lacks trusted uniqueness for its join key", "Declare relationship/key uniqueness, aggregate at the intended grain, or deduplicate the joined relation.")
    return None


def _group_keys(sql: str) -> list[str]:
    match = re.search(r"\bgroup\s+by\s+(.+?)(?:\bhaving\b|\border\s+by\b|$)", sql, re.I | re.S)
    return [part.strip().lower() for part in match.group(1).split(",")] if match else []


def _grain_change(base: str, head: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    before, after = _group_keys(base), _group_keys(head)
    declared = {str(value).lower() for value in metadata.get("declared_grain", [])}
    if before and after and before != after and declared and set(after) != declared:
        return _finding("B09_GRAIN_CHANGING_AGGREGATION", f"GROUP BY changed from {before} to {after}; declared grain is {sorted(declared)}", "Update the declared grain and contracts or restore the intended grouping keys.")
    return None


def _has_dedup(sql: str) -> bool:
    return bool(re.search(r"row_number\s*\(\s*\)\s*over|qualify\s+row_number|distinct\s+on", sql, re.I))


def _dedup_removed(base: str, head: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if metadata.get("declared_grain") and _has_dedup(base) and not _has_dedup(head):
        return _finding("B10_MISSING_DEDUPLICATION", "Prior deduplication pattern is absent from the head query", "Restore equivalent deduplication or provide an upstream uniqueness contract.")
    return None


def _watermark_weakened(base: str, head: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if not metadata.get("incremental"):
        return None
    required = metadata.get("required_lookback_days")
    if required is not None and _lookback_days(base) >= int(required) and _lookback_days(head) < int(required):
        return _finding("B11_UNSAFE_INCREMENTAL_WATERMARK", "Head incremental predicate removed the required late-arrival lookback window", "Restore the configured lookback and verify unique-key merge behavior.")
    return None


def _lookback_days(sql: str) -> int:
    match = re.search(r"interval\s+'(\d+)'\s+day", sql, re.I)
    return int(match.group(1)) if match else 0


def _join_kind(sql: str, table: str) -> str | None:
    match = re.search(r"\b(left\s+|inner\s+)?join\s+" + re.escape(table) + r"\b", sql, re.I)
    return (match.group(1) or "inner").strip().lower() if match else None


def _left_to_inner(base: str, head: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    tables = set(_relation_names(base)) & set(_relation_names(head))
    for table in tables:
        if _join_kind(base, table) == "left" and _join_kind(head, table) == "inner":
            return _finding("C06_LEFT_TO_INNER_JOIN", f"Relationship to {table} changed from LEFT JOIN to INNER JOIN", "Preserve the LEFT JOIN or document the intentional null-row removal in the contract.")
    return None
