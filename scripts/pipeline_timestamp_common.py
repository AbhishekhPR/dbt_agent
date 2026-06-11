import sqlite3
from datetime import datetime
from pathlib import Path


DEMO_NAME = "pipeline_timestamp_demo"
DB_RELATIVE_PATH = Path(DEMO_NAME) / "db" / "pipeline_timestamp.db"
MODEL_ORDER = ["stg_orders", "fct_revenue", "dashboard_metrics"]
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

MODEL_SQL = {
    "stg_orders": """
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
""".strip(),
    "fct_revenue": """
SELECT
    DATE(event_time) AS revenue_date,
    COUNT(*) AS total_orders,
    COUNT(CASE WHEN order_status = 'completed' THEN 1 END) AS completed_orders,
    ROUND(SUM(order_total), 2) AS gross_revenue,
    ROUND(
        SUM(CASE WHEN order_status = 'completed' THEN order_total ELSE 0 END),
        2
    ) AS completed_revenue,
    MAX(source_max_event_time) AS source_max_event_time,
    MAX(source_max_ingested_at) AS source_max_ingested_at,
    MAX(source_max_updated_at) AS source_max_updated_at,
    datetime('now') AS model_built_at
FROM stg_orders
GROUP BY DATE(event_time)
""".strip(),
    "dashboard_metrics": """
SELECT
    revenue_date AS metric_date,
    SUM(total_orders) AS total_orders,
    SUM(completed_orders) AS completed_orders,
    ROUND(SUM(gross_revenue), 2) AS gross_revenue,
    ROUND(SUM(completed_revenue), 2) AS completed_revenue,
    MAX(source_max_event_time) AS source_max_event_time,
    MAX(source_max_ingested_at) AS source_max_ingested_at,
    MAX(source_max_updated_at) AS source_max_updated_at,
    datetime('now') AS model_built_at
FROM fct_revenue
GROUP BY revenue_date
""".strip(),
}


def project_root(base_path: Path | str | None = None) -> Path:
    return Path(base_path) if base_path is not None else Path(__file__).resolve().parent.parent


def demo_path(base_path: Path | str | None = None) -> Path:
    return project_root(base_path) / DEMO_NAME


def db_path(base_path: Path | str | None = None) -> Path:
    return project_root(base_path) / DB_RELATIVE_PATH


def models_path(base_path: Path | str | None = None) -> Path:
    return demo_path(base_path) / "models"


def write_model_files(base_path: Path | str | None = None) -> None:
    model_dir = models_path(base_path)
    model_dir.mkdir(parents=True, exist_ok=True)
    for model_name, sql in MODEL_SQL.items():
        (model_dir / f"{model_name}.sql").write_text(sql + "\n", encoding="utf-8")


def fmt(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(TIME_FORMAT)


def print_timestamp_summary(db_file: Path, table: str) -> None:
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*),
                MIN(event_time),
                MAX(event_time),
                MAX(ingested_at),
                MAX(updated_at)
            FROM {table}
            """
        ).fetchone()
    finally:
        conn.close()

    print(f"{table} rows: {row[0]}")
    print(f"min event_time: {row[1]}")
    print(f"max event_time: {row[2]}")
    print(f"max ingested_at: {row[3]}")
    print(f"max updated_at: {row[4]}")
