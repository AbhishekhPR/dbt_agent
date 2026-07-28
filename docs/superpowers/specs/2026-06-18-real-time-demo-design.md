# Real-Time Demo Design

## Goal

Build a local SQLite demo for Relium that simulates warehouse data arriving continuously, rebuilds SQL/dbt-style models, and verifies that Relium detects metadata quality issues across raw and downstream model tables.

## Scope

Create a new `real_time_demo/` project with its own SQLite database, model SQL files, ingestion loop, monitor loop, and anomaly injector. The demo must not change or break the existing `business_demo`, `pipeline_timestamp_demo`, PR guard, simulation, or quality commands.

## Architecture

The demo will live beside the existing demos inside `dbt-agent/`:

- `real_time_demo/db/realtime.db` stores the SQLite warehouse.
- `real_time_demo/models/*.sql` stores the SQL model definitions.
- `scripts/create_real_time_demo.py` initializes clean data and writes model files.
- `scripts/realtime_ingest_loop.py` appends timed batches to `raw_orders`.
- `scripts/run_real_time_models.py` rebuilds model tables in dependency order.
- `scripts/realtime_monitor_loop.py` runs model rebuilds and Relium quality checks on a timer.
- `scripts/inject_realtime_anomaly.py` applies demo anomalies to `raw_orders`.

A small shared helper module may be added under `scripts/` to keep paths, timestamp formatting, deterministic row generation, and SQL model text consistent.

## Data Flow

`raw_orders` starts with 1,000 clean rows and receives new batches. The model chain is:

1. `stg_orders` selects raw rows and preserves source timestamp watermarks.
2. `fct_revenue_realtime` aggregates orders by minute while carrying source watermarks.
3. `dashboard_realtime_metrics` aggregates the fact model and continues carrying source watermarks.

Each model includes `model_built_at`, but Relium must use source freshness metadata for model freshness. The quality checker already prefers `source_max_updated_at` before `model_built_at`; tests will lock this behavior.

## Quality Behavior

Running:

```powershell
python main.py quality --project real_time_demo --db real_time_demo/db/realtime.db
```

will check the SQLite tables in the database:

- `raw_orders`
- `stg_orders`
- `fct_revenue_realtime`
- `dashboard_realtime_metrics`

Raw freshness candidates are `updated_at`, `ingested_at`, and `event_time`. Model freshness candidates are `source_max_updated_at`, `source_max_ingested_at`, `source_max_event_time`, and `model_built_at`, with `source_max_updated_at` preferred.

## Anomaly Injection

The anomaly injector will support:

- `stale_data`: set raw timestamp metadata to 48 hours ago.
- `null_spike`: set `order_total` to `NULL` for about 30% of recent rows.
- `row_count_drop`: delete about 80% of `raw_orders`.
- `duplicate_ids`: because `order_id` is a primary key, insert a duplicate customer/order pattern with repeated `customer_id`, `order_total`, and same-minute timestamps.

Each anomaly prints the type, affected rows, current raw row count, and max `updated_at`.

## Testing

Tests will be written before implementation and cover:

- Creating the realtime database and model files.
- Preserving clean initial row constraints.
- Inserting bounded fast-test batches.
- Rebuilding models in dependency order.
- Propagating source freshness watermarks downstream.
- Confirming Relium freshness inference prefers source watermarks.
- Applying each anomaly type and observing the expected table-level effect.

Manual verification will also run the requested non-breaking commands for existing demos where feasible.

## Documentation

The root `README.md` will get a `Real-time demo` section with the four-terminal workflow, fast-test mode, anomaly injection examples, and a short explanation that Relium tracks freshness, row counts, nulls, duplicates, schema drift, and downstream model freshness from metadata only.
