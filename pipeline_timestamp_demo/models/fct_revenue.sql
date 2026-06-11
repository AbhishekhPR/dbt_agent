SELECT
    DATE(event_time) AS revenue_date,
    COUNT(*) AS total_orders,
    COUNT(CASE WHEN order_status = 'completed' THEN 1 END) AS completed_orders,
    ROUND(SUM(order_total), 2) AS gross_revenue,
    ROUND(
        SUM(CASE WHEN order_status = 'completed' THEN order_total ELSE 0 END),
        2
    ) AS completed_revenue,
    MAX(source_max_event_time) AS source_max_event_time,
    MAX(source_max_ingested_at) AS source_max_ingested_at,
    MAX(source_max_updated_at) AS source_max_updated_at,
    datetime('now') AS model_built_at
FROM stg_orders
GROUP BY DATE(event_time)
