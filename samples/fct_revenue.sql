SELECT
    customer_id,
    SUM(order_total) as revenue
FROM stg_orders
WHERE customer_id = 'C1042'