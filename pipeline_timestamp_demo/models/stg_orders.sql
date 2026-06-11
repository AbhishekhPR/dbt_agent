SELECT
    order_id,
    customer_id,
    order_status,
    order_total,
    event_time,
    ingested_at,
    updated_at,
    (SELECT MAX(event_time) FROM raw_orders) AS source_max_event_time,
    (SELECT MAX(ingested_at) FROM raw_orders) AS source_max_ingested_at,
    (SELECT MAX(updated_at) FROM raw_orders) AS source_max_updated_at,
    datetime('now') AS model_built_at
FROM raw_orders
