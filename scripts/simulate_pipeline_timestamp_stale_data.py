import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pipeline_timestamp_common import db_path, fmt, print_timestamp_summary


def simulate_pipeline_timestamp_stale_data(
    base_path: Path | str | None = None,
    stale_hours: int = 48,
) -> None:
    db_file = db_path(base_path)
    stale_timestamp = fmt(datetime.utcnow() - timedelta(hours=stale_hours))

    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            """
            UPDATE raw_orders
            SET
                event_time = ?,
                ingested_at = ?,
                updated_at = ?
            """,
            (stale_timestamp, stale_timestamp, stale_timestamp),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"raw_orders timestamps set stale by {stale_hours} hours.")
    print_timestamp_summary(db_file, "raw_orders")


if __name__ == "__main__":
    simulate_pipeline_timestamp_stale_data()
