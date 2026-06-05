# Relium Incident Report

## Incident Summary

Project: test_project  
Table: raw_orders  
Anomaly Type: row_count_anomaly  
Severity: critical  
Data Loss Risk: yes  
Generated At: 2026-06-05 11:33:49  

## Executive Summary

Relium detected a critical row-count anomaly in raw_orders.

raw_orders dropped from ~200 expected rows to 8 observed rows, a 96.0% decrease.

Primary hypothesis: upstream ingestion failure.

3 downstream model(s) may be affected.

## Metric Evidence

Expected rows: ~200  
Observed rows: 8  
Change: -96.0%  

Anomaly message:
Row count dropped by 96.0%

Detail:
Expected ~200 rows, got 8

Impact:
Possible data loss or duplication in pipeline

## Root Cause Analysis

Primary hypothesis:
Upstream ingestion failure.

Confidence:
0.95

Status:
Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.

Reason:
The raw_orders table experienced a sharp row-count drop compared to the baseline. Since this table is a raw/source-level dependency, downstream models are likely inheriting the issue rather than causing it.

## Alternative Hypotheses

1. Accidental filter introduction.  
   Confidence: 0.85  
   Reason: A restrictive filter can remove records before downstream models run.

2. Source table truncation.  
   Confidence: 0.80  
   Reason: Large row count drops can indicate partial loads or truncation.

3. Join removing records.  
   Confidence: 0.70  
   Reason: Downstream joins may remove unmatched rows.

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
