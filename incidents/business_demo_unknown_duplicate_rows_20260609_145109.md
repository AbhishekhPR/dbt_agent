# Relium Incident Report

## Incident Summary

Project: business_demo  
Table: unknown  
Anomaly Type: duplicate_rows  
Severity: high  
Data Loss Risk: no  
Generated At: 2026-06-09 14:51:09  

## Executive Summary

Relium detected a high duplicate rows in unknown.

The anomaly in unknown may affect downstream analytics models that depend on this table.

Primary hypothesis: No strong RCA evidence.

0 downstream model(s) may be affected.

## Metric Evidence

Expected rows: N/A  
Observed rows: N/A  
Change: N/A  

Anomaly message:
Duplicate rows jumped from 0 to 290000

Detail:
Possible fan-out from a bad JOIN upstream

Impact:
Metrics like SUM(revenue) will be inflated

## Root Cause Analysis

Primary hypothesis:
No strong RCA evidence.

Confidence:
N/A

Status:
Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.

Reason:
No deterministic reason available.

## Alternative Hypotheses

No alternative hypotheses identified.

## Blast Radius

Total affected models: 0

Affected models:

- None found

Interpretation:
These models either directly or indirectly depend on unknown. If unknown is incomplete, these downstream models may produce incorrect metrics.

## Recommended Investigation Steps

1. Check the upstream ingestion job for unknown.
2. Compare the latest unknown row count with the previous successful run.
3. Verify whether the source table was partially loaded or truncated.
4. Review recent WHERE clause or filter changes.
5. Inspect downstream joins only if unknown appears healthy.

## Suggested Owner Action

First action: Verify whether the upstream ingestion job for unknown completed successfully and loaded the expected number of rows.

Investigation priority: Start at unknown before debugging downstream models, because the affected models appear to inherit the anomaly from the raw table layer.

## Compliance Note

This report was generated using metadata only.

Relium did not access customer records, raw table data, query results, emails, names, or PII.

Only the following metadata was used:

- row counts
- anomaly details
- dependency graph
- SQL structure metadata
- blast radius metadata
