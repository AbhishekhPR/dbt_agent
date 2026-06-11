import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pipeline_timestamp_common import MODEL_ORDER, db_path, models_path


def run_pipeline_timestamp_models(base_path: Path | str | None = None) -> None:
    db_file = db_path(base_path)
    model_dir = models_path(base_path)

    conn = sqlite3.connect(db_file)
    try:
        for model_name in MODEL_ORDER:
            sql_path = model_dir / f"{model_name}.sql"
            model_sql = sql_path.read_text(encoding="utf-8").strip().rstrip(";")
            conn.execute(f"DROP TABLE IF EXISTS {model_name}")
            conn.execute(f"CREATE TABLE {model_name} AS {model_sql}")
            conn.commit()

            row = conn.execute(
                f"""
                SELECT
                    MAX(source_max_updated_at),
                    MAX(model_built_at)
                FROM {model_name}
                """
            ).fetchone()
            print(f"built model: {model_name}")
            print(f"  max source_max_updated_at: {row[0]}")
            print(f"  max model_built_at: {row[1]}")
    finally:
        conn.close()


if __name__ == "__main__":
    run_pipeline_timestamp_models()
