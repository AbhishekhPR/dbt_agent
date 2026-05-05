SELECT
    o.order_id,
    c.name
FROM stg_orders o
JOIN stg_customers c ON o.user_id = c.id