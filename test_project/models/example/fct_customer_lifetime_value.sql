SELECT
    o.customer_id,
    c.customer_segment,
    c.acquisition_channel,
    COUNT(o.order_id) as total_orders,
    SUM(o.order_total) as lifetime_value,
    SUM(o.order_total) / COUNT(o.order_id) as avg_order_value,
    MIN(o.created_at) as first_order_date,
    MAX(o.created_at) as last_order_date,
    MAX(o.created_at) - MIN(o.created_at) as customer_tenure_days,
    SUM(o.order_total) / COUNT(DISTINCT DATE(o.created_at)) as avg_daily_revenue,
    CASE 
        WHEN SUM(o.order_total) > 1000 THEN 'high_value'
        WHEN SUM(o.order_total) > 500 THEN 'mid_value'
        ELSE 'low_value'
    END as value_segment,
    ROUND(
        100.0 * COUNT(CASE WHEN o.order_status = 'completed' THEN 1 END) 
        / COUNT(o.order_id), 
        2
    ) as completion_rate,
    SUM(CASE WHEN o.order_status = 'refunded' THEN o.order_total ELSE 0 END) as total_refunds,
    SUM(o.order_total) - SUM(CASE WHEN o.order_status = 'refunded' 
        THEN o.order_total ELSE 0 END) as net_revenue,
    LAG(SUM(o.order_total)) OVER (
        PARTITION BY o.customer_id 
        ORDER BY MAX(o.created_at)
    ) as prev_period_revenue,
    ROW_NUMBER() OVER (
        PARTITION BY c.customer_segment 
        ORDER BY SUM(o.order_total)
    ) as rank_in_segment
FROM raw_orders o
LEFT JOIN raw_customers c ON o.customer_id = c.id
WHERE o.created_at >= '2024-01-01'
    AND o.order_status != 'cancelled'
    AND c.is_deleted = 0
GROUP BY 
    o.customer_id,
    c.customer_segment,
    c.acquisition_channel