-- hosted Relium webhook test
SELECT
    customer_id,
    SUM(order_total) as total_revenue,
    COUNT(*) as order_count,
    SUM(order_total) / COUNT(*) as avg_order_value,
    order_status
FROM raw_orders
WHERE order_status != 'cancelled'
GROUP BY customer_id, order_status
