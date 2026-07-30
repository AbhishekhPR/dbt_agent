import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_METADATA_DB_PATH = Path(__file__).resolve().parent.parent / "relium_metadata.db"


class MetadataStoreError(ValueError):
    """Raised when persisted metadata cannot be read safely."""


@dataclass
class ScanRunRecord:
    project_name: str
    model_name: str
    risk_level: str
    safe_to_merge: bool
    affected_models: list[str] = field(default_factory=list)
    report_text: str = ""
    scan_id: str | None = None
    timestamp: str | None = None


@dataclass
class ModelMetricRecord:
    scan_id: str
    project_name: str
    model_name: str
    row_count: int
    null_count: int
    duplicate_count: int
    freshness_timestamp: str | None
    schema_column_count: int
    timestamp: str | None = None


@dataclass
class DriftRecord:
    project_name: str
    model_name: str
    current_scan_id: str
    previous_scan_id: str
    row_count_change_pct: float
    null_count_change_pct: float
    duplicate_count_change_pct: float
    schema_column_count_change: int
    freshness_regressed: bool
    drift_level: str
    report_text: str
    timestamp: str | None = None


def initialize_metadata_db(db_path: str | Path = DEFAULT_METADATA_DB_PATH) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS relium_scan_runs (
                scan_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                project_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                safe_to_merge INTEGER NOT NULL,
                affected_models TEXT NOT NULL,
                report_text TEXT
            );

            CREATE TABLE IF NOT EXISTS relium_model_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                project_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                row_count INTEGER,
                null_count INTEGER,
                duplicate_count INTEGER,
                freshness_timestamp TEXT,
                schema_column_count INTEGER,
                FOREIGN KEY(scan_id) REFERENCES relium_scan_runs(scan_id)
            );

            CREATE TABLE IF NOT EXISTS relium_metric_drifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                project_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                current_scan_id TEXT NOT NULL,
                previous_scan_id TEXT NOT NULL,
                row_count_change_pct REAL NOT NULL,
                null_count_change_pct REAL NOT NULL,
                duplicate_count_change_pct REAL NOT NULL,
                schema_column_count_change INTEGER NOT NULL,
                freshness_regressed INTEGER NOT NULL,
                drift_level TEXT NOT NULL,
                report_text TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def insert_scan_run(
    db_path: str | Path,
    record: ScanRunRecord,
) -> str:
    path = initialize_metadata_db(db_path)
    scan_id = record.scan_id or str(uuid.uuid4())
    timestamp = record.timestamp or _utc_now()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO relium_scan_runs (
                scan_id, timestamp, project_name, model_name,
                risk_level, safe_to_merge, affected_models, report_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                timestamp,
                record.project_name,
                record.model_name,
                record.risk_level,
                int(record.safe_to_merge),
                json.dumps(record.affected_models),
                record.report_text,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return scan_id


def insert_model_metrics(
    db_path: str | Path,
    record: ModelMetricRecord,
) -> None:
    path = initialize_metadata_db(db_path)
    timestamp = record.timestamp or _utc_now()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO relium_model_metrics (
                scan_id, timestamp, project_name, model_name,
                row_count, null_count, duplicate_count,
                freshness_timestamp, schema_column_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.scan_id,
                timestamp,
                record.project_name,
                record.model_name,
                record.row_count,
                record.null_count,
                record.duplicate_count,
                record.freshness_timestamp,
                record.schema_column_count,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_scan_run(db_path: str | Path, scan_id: str) -> dict:
    rows = _read_metadata_rows(
        db_path,
        "SELECT * FROM relium_scan_runs WHERE scan_id = ?",
        (scan_id,),
    )
    return dict(rows[0]) if rows else {}


def fetch_model_metrics(db_path: str | Path, scan_id: str) -> list[dict]:
    rows = _read_metadata_rows(
        db_path,
        """
        SELECT * FROM relium_model_metrics
        WHERE scan_id = ?
        ORDER BY id ASC
        """,
        (scan_id,),
    )
    return [dict(row) for row in rows]


def insert_metric_drift(
    db_path: str | Path,
    record: DriftRecord,
) -> None:
    path = initialize_metadata_db(db_path)
    timestamp = record.timestamp or _utc_now()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO relium_metric_drifts (
                timestamp, project_name, model_name, current_scan_id, previous_scan_id,
                row_count_change_pct, null_count_change_pct, duplicate_count_change_pct,
                schema_column_count_change, freshness_regressed, drift_level, report_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                record.project_name,
                record.model_name,
                record.current_scan_id,
                record.previous_scan_id,
                record.row_count_change_pct,
                record.null_count_change_pct,
                record.duplicate_count_change_pct,
                record.schema_column_count_change,
                int(record.freshness_regressed),
                record.drift_level,
                record.report_text,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_metric_drifts(
    db_path: str | Path,
    project_name: str,
    model_name: str,
) -> list[dict]:
    rows = _read_metadata_rows(
        db_path,
        """
        SELECT * FROM relium_metric_drifts
        WHERE project_name = ? AND model_name = ?
        ORDER BY id ASC
        """,
        (project_name, model_name),
    )
    return [dict(row) for row in rows]


def fetch_recent_model_metrics(
    db_path: str | Path,
    project_name: str | None = None,
    model_name: str | None = None,
    limit: int = 2,
) -> list[dict]:
    sql = """
        SELECT * FROM relium_model_metrics
    """
    params: list[object] = []
    clauses = []
    if project_name:
        clauses.append("project_name = ?")
        params.append(project_name)
    if model_name:
        clauses.append("model_name = ?")
        params.append(model_name)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = _read_metadata_rows(db_path, sql, tuple(params))
    return [dict(row) for row in rows]


def fetch_latest_model_identity(db_path: str | Path) -> tuple[str, str] | None:
    rows = _read_metadata_rows(
        db_path,
        """
        SELECT project_name, model_name
        FROM relium_model_metrics
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
        """,
    )
    if not rows:
        return None
    return rows[0][0], rows[0][1]


def _read_metadata_rows(
    db_path: str | Path,
    query: str,
    parameters: tuple = (),
) -> list[sqlite3.Row]:
    path = _metadata_db_path_for_read(db_path)
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as error:
        raise MetadataStoreError(
            f"Could not open metadata database read-only: {path}: {error}"
        ) from error

    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, parameters).fetchall()
    except sqlite3.Error as error:
        raise MetadataStoreError(
            f"Could not read metadata database: {path}: {error}"
        ) from error
    finally:
        connection.close()


def _metadata_db_path_for_read(db_path: str | Path) -> Path:
    if db_path is None or not str(db_path).strip():
        raise MetadataStoreError("A metadata database path is required.")

    supplied = Path(db_path).expanduser()
    try:
        path = supplied.resolve(strict=True)
    except FileNotFoundError as error:
        raise MetadataStoreError(
            f"Metadata database does not exist: {supplied}"
        ) from error
    except OSError as error:
        raise MetadataStoreError(
            f"Could not access metadata database: {supplied}: {error}"
        ) from error

    if not path.is_file():
        raise MetadataStoreError(f"Metadata database is not a file: {supplied}")
    try:
        if path.stat().st_size == 0:
            raise MetadataStoreError(f"Metadata database is empty: {supplied}")
    except OSError as error:
        raise MetadataStoreError(
            f"Could not access metadata database: {supplied}: {error}"
        ) from error
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
