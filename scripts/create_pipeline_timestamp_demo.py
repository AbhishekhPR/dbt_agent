import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pipeline_timestamp_common import (
    db_path,
    fmt,
    print_timestamp_summary,
    write_model_files,
)


ORDER_STATUSES = ("completed", "completed", "completed", "pending", "cancelled", "refunded")


def create_pipeline_timestamp_demo(
    base_path: Path | str | None = None,
    row_count: int = 5_000,
) -> Path:
    db_file = db_path(base_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    write_model_files(base_path)

    now = datetime.utcnow().replace(microsecond=0)
    rows = []
    for order_id in range(1, row_count + 1):
        event_time = now - timedelta(hours=1, minutes=order_id % 60)
        ingested_at = now - timedelta(minutes=order_id % 45)
        updated_at = now - timedelta(minutes=order_id % 30)
        order_total = round(25 + (order_id % 250) * 1.17 + (order_id % 5) * 3.25, 2)
        rows.append(
            (
                order_id,
                ((order_id * 17) % 1_000) + 1,
                ORDER_STATUSES[order_id % len(ORDER_STATUSES)],
                order_total,
                fmt(event_time),
                fmt(ingested_at),
                fmt(updated_at),
            )
        )

    conn = sqlite3.connect(db_file)
    try:
        conn.execute("DROP TABLE IF EXISTS dashboard_metrics")
        conn.execute("DROP TABLE IF EXISTS fct_revenue")
        conn.execute("DROP TABLE IF EXISTS stg_orders")
        conn.execute("DROP TABLE IF EXISTS raw_orders")
        conn.execute(
            """
            CREATE TABLE raw_orders (
                order_id INTEGER PRIMARY KEY,
                customer_id INTEGER,
                order_status TEXT,
                order_total REAL,
                event_time TEXT,
                ingested_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO raw_orders
            (order_id, customer_id, order_status, order_total,
             event_time, ingested_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    print("Pipeline timestamp demo database created.")
    print(f"rows inserted: {row_count}")
    print_timestamp_summary(db_file, "raw_orders")
    return db_file


if __name__ == "__main__":
    create_pipeline_timestamp_demo()
