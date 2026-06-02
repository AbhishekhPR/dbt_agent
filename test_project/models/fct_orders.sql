-- Auto-fixed by dbt-agent
-- Replace 'order_status' with 'status' in the WHERE clause, resulting in: SELECT order_id, customer_id, status AS order_status FROM raw_orders WHERE status = 'completed'

SELECT
    order_id,
    customer_id,
    status
FROM raw_orders
WHERE status = 'completed'