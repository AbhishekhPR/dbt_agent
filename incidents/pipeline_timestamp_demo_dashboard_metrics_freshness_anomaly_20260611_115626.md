# Relium Incident Report

## Incident Summary

Project: pipeline_timestamp_demo  
Table: dashboard_metrics  
Anomaly Type: freshness_anomaly  
Severity: critical  
Data Loss Risk: yes  
Generated At: 2026-06-11 11:56:26  

## Executive Summary

Relium detected a critical freshness anomaly in dashboard_metrics.

The anomaly in dashboard_metrics may affect downstream analytics models that depend on this table.

Primary hypothesis: upstream ingestion delay.

0 downstream model(s) may be affected.

## Metric Evidence

Expected rows: N/A  
Observed rows: N/A  
Change: N/A  

Anomaly message:
Table is stale by 48.0 hours

Detail:
Latest source_max_updated_at value is 2026-06-09 11:56:25

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
Table is stale by 48.0 hours.

## Alternative Hypotheses

1. Failed scheduled load.  
   Confidence: 0.66  
   Reason: Table is stale by 48.0 hours.

2. Source connector paused.  
   Confidence: 0.57  
   Reason: Table is stale by 48.0 hours.

3. Warehouse/job orchestration failure.  
   Confidence: 0.51  
   Reason: Table is stale by 48.0 hours.

## Blast Radius

Total affected models: 0

Affected models:

- None found

Interpretation:
These models either directly or indirectly depend on dashboard_metrics. If dashboard_metrics is stale, downstream models may be using outdated data.

## Recommended Investigation Steps

1. Check whether the scheduled ingestion job for dashboard_metrics ran successfully.
2. Verify the latest source sync timestamp.
3. Check whether the source connector is paused or delayed.
4. Review orchestration logs for failed, skipped, or delayed jobs.
5. Confirm whether the source system is producing new records.
6. Validate the expected freshness SLA for this table.

## Suggested Owner Action

First action: Verify whether the scheduled ingestion job for dashboard_metrics completed successfully and updated the table within the expected freshness window.

Investigation priority: Start with ingestion schedule, source connector status, and orchestration logs before debugging downstream models.

## Compliance Note

This report was generated using metadata only.

Relium did not access customer records, raw table data, query results, emails, names, or PII.

Only the following metadata was used:

- row counts
- anomaly details
- dependency graph
- SQL structure metadata
- blast radius metadata
