# Relium PR Guard Report

## Summary

* Project: business_demo
* Files scanned: 5
* Risks found: 1
* Highest severity: HIGH
* Safe to merge: NO

## Risks

### [HIGH] fct_customer_lifetime_value

File: models/fct_customer_lifetime_value.sql
Risk: LEFT JOIN may behave like INNER JOIN because WHERE filters the right-side table.
Evidence: WHERE c.is_deleted = [NUMBER_LITERAL]
Why it matters: A LEFT JOIN should preserve rows from the left table. Filtering the right-side table in the WHERE clause can remove unmatched rows and silently change the business meaning of the model.
Recommendation: Move the right-table filter into the JOIN condition or explicitly allow NULLs.
Suggested fix:

```sql
LEFT JOIN raw_customers c
    ON o.customer_id = c.customer_id
   AND c.is_deleted = 0
```

Affected downstream models:
- fct_daily_kpis
- dashboard_executive_metrics
