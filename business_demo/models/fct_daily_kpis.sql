WITH customer_ltv AS (
    SELECT
        COUNT(customer_id) AS active_customers,
        AVG(lifetime_value) AS average_ltv
    FROM fct_customer_lifetime_value
)

SELECT
    r.order_date,
    r.net_revenue AS daily_revenue,
    (SELECT active_customers FROM customer_ltv) AS active_customers,
    (SELECT average_ltv FROM customer_ltv) AS average_ltv,
    r.completed_orders AS completed_order_count
FROM fct_revenue r
