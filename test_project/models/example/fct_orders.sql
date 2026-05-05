SELECT
    order_id,
    customer_id,
    order_status
FROM raw_orders_v2
WHERE order_status = 'completed'