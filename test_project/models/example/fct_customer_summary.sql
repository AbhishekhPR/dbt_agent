SELECT
    customer_segment,
    COUNT(*) as customers,
    AVG(lifetime_value) as avg_lifetime_value
FROM fct_customer_lifetime_value
GROUP BY customer_segment