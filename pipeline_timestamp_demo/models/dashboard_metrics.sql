SELECT
    revenue_date AS metric_date,
    SUM(total_orders) AS total_orders,
    SUM(completed_orders) AS completed_orders,
    ROUND(SUM(gross_revenue), 2) AS gross_revenue,
    ROUND(SUM(completed_revenue), 2) AS completed_revenue,
    MAX(source_max_event_time) AS source_max_event_time,
    MAX(source_max_ingested_at) AS source_max_ingested_at,
    MAX(source_max_updated_at) AS source_max_updated_at,
    datetime('now') AS model_built_at
FROM fct_revenue
GROUP BY revenue_date
