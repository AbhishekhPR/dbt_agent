# Relium Incident Report

## Incident Summary

Project: test_project  
Table: raw_orders  
Anomaly Type: row_count_anomaly  
Severity: critical  
Data Loss Risk: yes  
Generated At: 2026-06-05 07:19:02  

## Executive Summary

Relium detected a critical row-count anomaly in raw_orders.

raw_orders dropped from ~8 expected rows to 13 observed rows, a +62.5% decrease.

Primary hypothesis: duplicate ingestion.

3 downstream model(s) may be affected.

## Metric Evidence

Expected rows: ~8  
Observed rows: 13  
Change: +62.5%  

Anomaly message:
Row count spiked by 62.5%

Detail:
Expected ~8 rows, got 13

Impact:
Possible data loss or duplication in pipeline

## Root Cause Analysis

Primary hypothesis:
Duplicate ingestion.

Confidence:
0.85

Status:
Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.

Reason:
The raw_orders table experienced a sharp row-count drop compared to the baseline. Since this table is a raw/source-level dependency, downstream models are likely inheriting the issue rather than causing it.

## Alternative Hypotheses

1. Join fan-out.  
   Confidence: 0.75  
   Reason: Many-to-many joins can multiply records.

2. Accidental cross join.  
   Confidence: 0.70  
   Reason: Missing join predicates can create a Cartesian product.

3. Missing deduplication.  
   Confidence: 0.60  
   Reason: Deduplication changes can allow repeated records downstream.

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
