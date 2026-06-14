<!-- relium-pr-guard -->

## Relium PR Guard

Safe to merge: NO

Merge decision:
Blocked because HIGH risk transformation logic was detected.

Project: business_demo
Files scanned: 1
Risks found: 1
Highest severity: HIGH

### High risk transformation logic found

#### fct_customer_lifetime_value

Risk:
LEFT JOIN may behave like INNER JOIN because WHERE filters the right-side table.

Confidence:
95%

Impact Level:
HIGH

Blast Radius Score:
9/10

Evidence:
`WHERE c.is_deleted = [NUMBER_LITERAL]`

Why it matters:
A LEFT JOIN should preserve rows from the left table. Filtering the right-side table in the WHERE clause can remove unmatched rows and silently change the business meaning of the model.

Business impact:
This change may silently remove valid rows from the left-side table. Metrics such as customer lifetime value, revenue, order counts, daily KPIs, and dashboard totals may become undercounted.

Recommended Action:
Fix before merge. This risky transformation may silently remove records and affect downstream business models.

Suggested fix:

```sql
LEFT JOIN raw_customers c
    ON o.customer_id = c.customer_id
   AND c.is_deleted = 0
```

Affected downstream models:

* fct_daily_kpis
* dashboard_executive_metrics
