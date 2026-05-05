SELECT
    order_id,
    customer_id,
    order_total,
    order_status
FROM stg_orders
WHERE order_status = 'completed'