SELECT
    o.customer_id,
    c.customer_segment,
    c.acquisition_channel,
    COUNT(o.order_id) AS total_orders,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END) AS lifetime_value,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END)
        / NULLIF(COUNT(CASE WHEN o.order_status = 'completed' THEN o.order_id END), 0) AS average_order_value
FROM raw_orders o
LEFT JOIN raw_customers c ON o.customer_id = c.customer_id
WHERE c.is_deleted = 0
GROUP BY
    o.customer_id,
    c.customer_segment,
    c.acquisition_channel

-- test Relium PR Guard

-- retrigger Relium PR Guard
