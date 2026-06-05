# Relium Incident Report

## Incident Summary

Project: test_project  
Table: raw_orders  
Anomaly Type: duplicate_explosion  
Severity: critical  
Data Loss Risk: yes  
Generated At: 2026-06-05 11:06:26  

## Executive Summary

Relium detected a critical duplicate explosion in raw_orders.

The anomaly in raw_orders may affect downstream analytics models that depend on this table.

Primary hypothesis: duplicate ingestion.

3 downstream model(s) may be affected.

## Metric Evidence

Expected rows: N/A  
Observed rows: N/A  
Change: N/A  

Anomaly message:
Duplicate rows increased from 0 to 10

Detail:
Duplicate rate is 76.92% using key: order_id

Impact:
Duplicate records may inflate COUNT, SUM, revenue, and downstream metrics.

## Root Cause Analysis

Primary hypothesis:
Duplicate ingestion.

Confidence:
0.99

Status:
Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.

Reason:
Duplicate rows increased from 0 to 10.

## Alternative Hypotheses

1. Bad join.  
   Confidence: 0.80  
   Reason: Duplicate rows increased from 0 to 10.

2. Missing deduplication.  
   Confidence: 0.71  
   Reason: Duplicate rows increased from 0 to 10.

3. Retry/replay of source load.  
   Confidence: 0.65  
   Reason: Duplicate rows increased from 0 to 10.

## Blast Radius

Total affected models: 3

Affected models:

- fct_customer_lifetime_value
- fct_revenue
- fct_customer_summary

Interpretation:
These models either directly or indirectly depend on raw_orders. If raw_orders is incomplete, these downstream models may produce incorrect metrics.

## Recommended Investigation Steps

1. Check the upstream ingestion job for raw_orders.
2. Compare the latest raw_orders row count with the previous successful run.
3. Verify whether the source table was partially loaded or truncated.
4. Review recent WHERE clause or filter changes.
5. Inspect downstream joins only if raw_orders appears healthy.

## Suggested Owner Action

First action: Verify whether the upstream ingestion job for raw_orders completed successfully and loaded the expected number of rows.

Investigation priority: Start at raw_orders before debugging downstream models, because the affected models appear to inherit the anomaly from the raw table layer.

## Compliance Note

This report was generated using metadata only.

Relium did not access customer records, raw table data, query results, emails, names, or PII.

Only the following metadata was used:

- row counts
- anomaly details
- dependency graph
- SQL structure metadata
- blast radius metadata
