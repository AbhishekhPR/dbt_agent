import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.create_pipeline_timestamp_demo import ORDER_STATUSES
from scripts.pipeline_timestamp_common import db_path, fmt, print_timestamp_summary


def insert_pipeline_timestamp_batch(
    base_path: Path | str | None = None,
    row_count: int = 1_000,
) -> None:
    db_file = db_path(base_path)
    now = datetime.utcnow().replace(microsecond=0)

    conn = sqlite3.connect(db_file)
    try:
        max_order_id = conn.execute(
            "SELECT COALESCE(MAX(order_id), 0) FROM raw_orders"
        ).fetchone()[0]
        rows = []
        for offset in range(1, row_count + 1):
            order_id = max_order_id + offset
            event_time = now - timedelta(minutes=offset % 10)
            ingested_at = now - timedelta(minutes=offset % 5)
            updated_at = now - timedelta(minutes=offset % 3)
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

    print(f"inserted rows: {row_count}")
    print_timestamp_summary(db_file, "raw_orders")


if __name__ == "__main__":
    insert_pipeline_timestamp_batch()
