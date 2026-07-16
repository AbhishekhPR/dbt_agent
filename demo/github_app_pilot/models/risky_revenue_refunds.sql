-- Pilot-only risky change: refund deductions were intentionally removed.
select
    order_date,
    gross_revenue as net_revenue
from {{ ref('fct_orders') }}
