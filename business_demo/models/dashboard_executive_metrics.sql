WITH top_products AS (
    SELECT
        category,
        SUM(product_revenue) AS category_revenue,
        SUM(order_count) AS category_orders
    FROM fct_product_performance
    GROUP BY category
),

daily_summary AS (
    SELECT
        SUM(daily_revenue) AS total_revenue,
        AVG(daily_revenue) AS average_daily_revenue,
        SUM(completed_order_count) AS completed_orders,
        AVG(active_customers) AS average_active_customers,
        AVG(average_ltv) AS average_ltv
    FROM fct_daily_kpis
)

SELECT
    ds.total_revenue,
    ds.average_daily_revenue,
    ds.completed_orders,
    ds.average_active_customers,
    ds.average_ltv,
    tp.category AS top_category,
    tp.category_revenue AS top_category_revenue
FROM daily_summary ds
LEFT JOIN top_products tp
    ON tp.category_revenue = (
        SELECT MAX(category_revenue)
        FROM top_products
    )
