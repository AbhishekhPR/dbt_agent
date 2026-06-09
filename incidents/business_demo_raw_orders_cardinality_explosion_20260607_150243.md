# Relium Incident Report

## Incident Summary

Project: business_demo  
Table: raw_orders  
Anomaly Type: cardinality_explosion  
Severity: medium  
Data Loss Risk: no  
Generated At: 2026-06-07 15:02:43  

## Executive Summary

Relium detected a medium cardinality explosion in raw_orders.

The anomaly in raw_orders may affect downstream analytics models that depend on this table.

Primary hypothesis: new dimension introduced.

5 downstream model(s) may be affected.

## Metric Evidence

Expected rows: N/A  
Observed rows: N/A  
Change: +92800.0%  

Anomaly message:
Distinct values in 'order_total' increased by 92800.0%

Detail:
Was 1 distinct values, now 929

Impact:
GROUP BY queries on this column may return unexpected granularity

## Root Cause Analysis

Primary hypothesis:
New dimension introduced.

Confidence:
0.99

Status:
Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.

Reason:
Distinct values in 'order_total' increased by 92800.0%.

## Alternative Hypotheses

1. Malformed grouping.  
   Confidence: 0.80  
   Reason: Distinct values in 'order_total' increased by 92800.0%.

2. Incorrect join logic.  
   Confidence: 0.71  
   Reason: Distinct values in 'order_total' increased by 92800.0%.

## Blast Radius

Total affected models: 5

Affected models:

- fct_customer_lifetime_value
- fct_daily_kpis
- fct_product_performance
- fct_revenue
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
