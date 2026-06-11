# Relium Incident Report

## Incident Summary

Project: business_demo  
Table: raw_orders  
Anomaly Type: row_count  
Severity: critical  
Data Loss Risk: yes  
Generated At: 2026-06-11 11:51:02  

## Executive Summary

Relium detected a critical row count in raw_orders.

The anomaly in raw_orders may affect downstream analytics models that depend on this table.

Primary hypothesis: No strong RCA evidence.

5 downstream model(s) may be affected.

## Metric Evidence

Expected rows: ~2500000  
Observed rows: 100000  
Change: -96.0%  

Anomaly message:
Row count dropped by 96.0%

Detail:
Expected ~2500000 rows, got 100000

Impact:
Possible data loss or duplication in pipeline

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

Total affected models: 5

Affected models:

- fct_customer_lifetime_value
- fct_product_performance
- fct_revenue
- fct_daily_kpis
- dashboard_executive_metrics

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
