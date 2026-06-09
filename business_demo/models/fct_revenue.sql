WITH orders AS (
    SELECT
        DATE(o.created_at) AS order_date,
        o.order_id,
        o.order_status,
        o.order_total
    FROM raw_orders o
),

payments AS (
    SELECT
        p.order_id,
        SUM(CASE WHEN p.payment_status = 'captured' THEN p.amount ELSE 0 END) AS captured_amount,
        SUM(CASE WHEN p.payment_status = 'refunded' THEN p.amount ELSE 0 END) AS refund_amount
    FROM raw_payments p
    GROUP BY p.order_id
)

SELECT
    o.order_date,
    SUM(CASE WHEN o.order_status = 'completed' THEN p.captured_amount ELSE 0 END) AS revenue,
    COUNT(CASE WHEN o.order_status = 'completed' THEN o.order_id END) AS completed_orders,
    SUM(p.refund_amount) AS refund_amount,
    SUM(CASE WHEN o.order_status = 'completed' THEN p.captured_amount ELSE 0 END) - SUM(p.refund_amount) AS net_revenue
FROM orders o
LEFT JOIN payments p ON o.order_id = p.order_id
GROUP BY o.order_date
