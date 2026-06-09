SELECT
    o.product_id,
    p.category,
    p.brand,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END) AS product_revenue,
    COUNT(o.order_id) AS order_count,
    AVG(CASE WHEN o.order_status = 'completed' THEN o.order_total END) AS average_selling_price,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END)
        / NULLIF(SUM(SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END)) OVER (PARTITION BY p.category), 0) AS category_performance
FROM raw_orders o
JOIN raw_products p ON o.product_id = p.product_id
WHERE p.is_active = 1
GROUP BY
    o.product_id,
    p.category,
    p.brand
