SELECT
    order_id,
    SUM(order_total) as revenue,
    SUM(discount_amount) as total_discounts
FROM stg_orders
GROUP BY order_id