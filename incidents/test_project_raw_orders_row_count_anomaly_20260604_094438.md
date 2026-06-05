# Relium Incident Report

## Summary

Project: test_project  
Table: raw_orders  
Anomaly: row_count_anomaly  
Severity: critical  
Data Loss Risk: yes  

## Metric Evidence

Message: Row count dropped by 96.0%  
Detail: Expected ~200 rows, got 8  
Impact: Possible data loss or duplication in pipeline  

## Root Cause Analysis

Primary hypothesis: upstream ingestion failure  
Confidence: 0.95  

Reason:
row count dropped by 96%

## Alternative Hypotheses

1. accidental filter introduction  
   Confidence: 0.85  
   Reason: a restrictive filter can remove records before downstream models run

2. source table truncation  
   Confidence: 0.80  
   Reason: large row count drops can indicate partial loads or truncation

3. join removing records  
   Confidence: 0.70  
   Reason: downstream joins may remove unmatched rows

## Blast Radius

Affected models:

- fct_customer_lifetime_value
- fct_revenue
- fct_customer_summary

Total affected models: 3

## Recommended Actions

1. Check upstream ingestion job for raw_orders
2. Compare latest row count with previous successful run
3. Review recent WHERE clause/filter changes
4. Check whether joins are removing unmatched records
5. Check whether source table was truncated or partially loaded

## Compliance Note

This report was generated using metadata only.

No customer records, query results, raw table data, emails, names, or PII were accessed.
