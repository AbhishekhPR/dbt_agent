# Relium Incident Report

## Incident Summary

Project: business_demo  
Table: raw_products  
Anomaly Type: freshness_anomaly  
Severity: critical  
Data Loss Risk: yes  
Generated At: 2026-06-07 16:58:17  

## Executive Summary

Relium detected a critical freshness anomaly in raw_products.

The anomaly in raw_products may affect downstream analytics models that depend on this table.

Primary hypothesis: upstream ingestion delay.

2 downstream model(s) may be affected.

## Metric Evidence

Expected rows: N/A  
Observed rows: N/A  
Change: N/A  

Anomaly message:
Table is stale by 3799.8 hours

Detail:
Latest updated_at value is 2025-12-31 09:09:00

Impact:
Downstream models may be using outdated data

## Root Cause Analysis

Primary hypothesis:
Upstream ingestion delay.

Confidence:
0.85

Status:
Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.

Reason:
Table is stale by 3799.8 hours.

## Alternative Hypotheses

1. Failed scheduled load.  
   Confidence: 0.66  
   Reason: Table is stale by 3799.8 hours.

2. Source connector paused.  
   Confidence: 0.57  
   Reason: Table is stale by 3799.8 hours.

3. Warehouse/job orchestration failure.  
   Confidence: 0.51  
   Reason: Table is stale by 3799.8 hours.

## Blast Radius

Total affected models: 2

Affected models:

- fct_product_performance
- dashboard_executive_metrics

Interpretation:
These models either directly or indirectly depend on raw_products. If raw_products is incomplete, these downstream models may produce incorrect metrics.

## Recommended Investigation Steps

1. Check the upstream ingestion job for raw_products.
2. Compare the latest raw_products row count with the previous successful run.
3. Verify whether the source table was partially loaded or truncated.
4. Review recent WHERE clause or filter changes.
5. Inspect downstream joins only if raw_products appears healthy.

## Suggested Owner Action

First action: Verify whether the upstream ingestion job for raw_products completed successfully and loaded the expected number of rows.

Investigation priority: Start at raw_products before debugging downstream models, because the affected models appear to inherit the anomaly from the raw table layer.

## Compliance Note

This report was generated using metadata only.

Relium did not access customer records, raw table data, query results, emails, names, or PII.

Only the following metadata was used:

- row counts
- anomaly details
- dependency graph
- SQL structure metadata
- blast radius metadata
