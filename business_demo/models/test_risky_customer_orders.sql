SELECT
    o.customer_id,
    c.customer_segment,
    COUNT(o.order_id) AS total_orders
FROM raw_orders o
LEFT JOIN raw_customers c
    ON o.customer_id = c.customer_id
WHERE c.is_deleted = 0
GROUP BY
    o.customer_id,
    c.customer_segment
