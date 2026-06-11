import json
import sqlite3
from pathlib import Path
from datetime import datetime

STORE_PATH = Path(__file__).resolve().parent.parent / "relium_data"
STORE_PATH.mkdir(exist_ok=True)
METADATA_HISTORY_DB = Path(__file__).resolve().parent.parent / "metadata_history.db"


def _init_table_metrics_history(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS table_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            project_name TEXT,
            table_name TEXT,
            row_count INTEGER,
            duplicate_rows INTEGER,
            metrics_json TEXT
        )
    """)
    conn.commit()


def get_db(project_name: str) -> sqlite3.Connection:
    """
    Each project gets its own SQLite metrics database.
    Stored locally — zero data leaves infrastructure.
    """
    db_path = STORE_PATH / f"{project_name}.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    """Create all tables if they don't exist."""
    conn.executescript("""
        -- Table quality metrics over time
        CREATE TABLE IF NOT EXISTS table_metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            table_name  TEXT NOT NULL,
            row_count   INTEGER,
            null_rates  TEXT,  -- JSON
            distinct_counts TEXT,  -- JSON
            numeric_stats   TEXT,  -- JSON
            duplicate_rows  INTEGER
        );

        -- Schema snapshots over time
        CREATE TABLE IF NOT EXISTS schema_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            table_name  TEXT NOT NULL,
            columns     TEXT NOT NULL  -- JSON [{name, type}]
        );

        -- Schema change events
        CREATE TABLE IF NOT EXISTS schema_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            table_name  TEXT NOT NULL,
            change_type TEXT NOT NULL,
            detail      TEXT NOT NULL,  -- JSON
            severity    TEXT NOT NULL,
            resolved    INTEGER DEFAULT 0
        );

        -- Freshness tracking
        CREATE TABLE IF NOT EXISTS freshness_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at     TEXT NOT NULL,
            table_name      TEXT NOT NULL,
            last_updated    TEXT,
            freshness_col   TEXT,
            hours_since_update REAL,
            status          TEXT  -- ok / stale / critical
        );

        -- dbt test results
        CREATE TABLE IF NOT EXISTS test_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            run_id      TEXT,
            model_name  TEXT NOT NULL,
            test_name   TEXT NOT NULL,
            test_type   TEXT,
            status      TEXT NOT NULL,  -- pass / fail / error
            failure_count INTEGER DEFAULT 0
        );

        -- dbt model execution metrics
        CREATE TABLE IF NOT EXISTS run_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at     TEXT NOT NULL,
            run_id          TEXT,
            model_name      TEXT NOT NULL,
            status          TEXT NOT NULL,
            execution_time  REAL,
            rows_affected   INTEGER
        );

        -- Lineage graph (persisted)
        CREATE TABLE IF NOT EXISTS lineage (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at     TEXT NOT NULL,
            model_name      TEXT NOT NULL,
            depends_on      TEXT NOT NULL  -- JSON list
        );
    """)
    conn.commit()


def record_table_metrics(project_name: str, table_name: str, metrics: dict):
    conn = sqlite3.connect(str(METADATA_HISTORY_DB))
    _init_table_metrics_history(conn)
    conn.execute("""
        INSERT INTO table_metrics
        (project_name, table_name, row_count, duplicate_rows, metrics_json)
        VALUES (?, ?, ?, ?, ?)
    """, (
        project_name,
        table_name,
        metrics.get("row_count"),
        metrics.get("duplicate_rows"),
        json.dumps(metrics)
    ))
    conn.commit()
    conn.close()

    project_conn = get_db(project_name)
    project_conn.execute("""
        INSERT INTO table_metrics
        (recorded_at, table_name, row_count, null_rates,
         distinct_counts, numeric_stats, duplicate_rows)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        table_name,
        metrics.get("row_count"),
        json.dumps(metrics.get("null_rates", {})),
        json.dumps(metrics.get("distinct_counts", {})),
        json.dumps(metrics.get("numeric_stats", {})),
        metrics.get("duplicate_rows"),
    ))
    project_conn.commit()
    project_conn.close()


def get_metric_history(project: str, table: str, days: int = 30) -> list:
    """Returns last N days of metrics for a table."""
    conn = get_db(project)
    rows = conn.execute("""
        SELECT * FROM table_metrics
        WHERE table_name = ?
        AND recorded_at >= datetime('now', ?)
        ORDER BY recorded_at ASC
    """, (table, f'-{days} days')).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_schema_snapshot(project: str, table: str, columns: list):
    conn = get_db(project)
    conn.execute("""
        INSERT INTO schema_history (recorded_at, table_name, columns)
        VALUES (?, ?, ?)
    """, (datetime.utcnow().isoformat(), table, json.dumps(columns)))
    conn.commit()
    conn.close()


def record_schema_change(project: str, table: str, change: dict):
    conn = get_db(project)
    conn.execute("""
        INSERT INTO schema_changes
        (detected_at, table_name, change_type, detail, severity)
        VALUES (?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        table,
        change.get("type"),
        json.dumps(change),
        change.get("severity", "high")
    ))
    conn.commit()
    conn.close()


def record_freshness(project: str, table: str, result: dict):
    conn = get_db(project)
    conn.execute("""
        INSERT INTO freshness_history
        (recorded_at, table_name, last_updated,
         freshness_col, hours_since_update, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        table,
        result.get("last_updated"),
        result.get("freshness_col"),
        result.get("hours_since_update"),
        result.get("status")
    ))
    conn.commit()
    conn.close()


def record_test_results(project: str, run_id: str, results: list):
    conn = get_db(project)
    now = datetime.utcnow().isoformat()
    for r in results:
        conn.execute("""
            INSERT INTO test_results
            (recorded_at, run_id, model_name, test_name,
             test_type, status, failure_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            now, run_id,
            r.get("model_name"),
            r.get("test_name"),
            r.get("test_type"),
            r.get("status"),
            r.get("failure_count", 0)
        ))
    conn.commit()
    conn.close()


def record_run_metrics(project: str, run_id: str, results: list):
    conn = get_db(project)
    now = datetime.utcnow().isoformat()
    for r in results:
        conn.execute("""
            INSERT INTO run_metrics
            (recorded_at, run_id, model_name,
             status, execution_time, rows_affected)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            now, run_id,
            r.get("model_name"),
            r.get("status"),
            r.get("execution_time"),
            r.get("rows_affected")
        ))
    conn.commit()
    conn.close()


def record_lineage(project: str, graph: dict):
    """Store the full dependency graph."""
    conn = get_db(project)
    now = datetime.utcnow().isoformat()
    # Clear old lineage and replace
    conn.execute("DELETE FROM lineage")
    for model, deps in graph.items():
        conn.execute("""
            INSERT INTO lineage (recorded_at, model_name, depends_on)
            VALUES (?, ?, ?)
        """, (now, model, json.dumps(deps)))
    conn.commit()
    conn.close()


def get_schema_change_history(project: str, table: str = None) -> list:
    conn = get_db(project)
    if table:
        rows = conn.execute("""
            SELECT * FROM schema_changes
            WHERE table_name = ?
            ORDER BY detected_at DESC LIMIT 50
        """, (table,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM schema_changes
            ORDER BY detected_at DESC LIMIT 100
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_freshness_history(project: str, table: str, days: int = 7) -> list:
    conn = get_db(project)
    rows = conn.execute("""
        SELECT * FROM freshness_history
        WHERE table_name = ?
        AND recorded_at >= datetime('now', ?)
        ORDER BY recorded_at ASC
    """, (table, f'-{days} days')).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_test_history(project: str, model: str = None, days: int = 30) -> list:
    conn = get_db(project)
    if model:
        rows = conn.execute("""
            SELECT * FROM test_results
            WHERE model_name = ?
            AND recorded_at >= datetime('now', ?)
            ORDER BY recorded_at DESC
        """, (model, f'-{days} days')).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM test_results
            AND recorded_at >= datetime('now', ?)
            ORDER BY recorded_at DESC LIMIT 200
        """, (f'-{days} days',)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_project_summary(project: str) -> dict:
    """
    High-level summary for a project.
    Used for dashboard and reporting.
    """
    conn = get_db(project)

    # Tables tracked
    tables = conn.execute("""
        SELECT DISTINCT table_name FROM table_metrics
    """).fetchall()

    # Recent schema changes
    recent_changes = conn.execute("""
        SELECT COUNT(*) as cnt FROM schema_changes
        WHERE detected_at >= datetime('now', '-7 days')
    """).fetchone()

    # Recent test failures
    recent_failures = conn.execute("""
        SELECT COUNT(*) as cnt FROM test_results
        WHERE status = 'fail'
        AND recorded_at >= datetime('now', '-7 days')
    """).fetchone()

    # Stale tables
    stale = conn.execute("""
        SELECT COUNT(*) as cnt FROM freshness_history
        WHERE status IN ('stale', 'critical')
        AND recorded_at >= datetime('now', '-24 hours')
    """).fetchone()

    conn.close()

    return {
        "tables_tracked": len(tables),
        "schema_changes_7d": recent_changes["cnt"] if recent_changes else 0,
        "test_failures_7d": recent_failures["cnt"] if recent_failures else 0,
        "stale_tables_24h": stale["cnt"] if stale else 0
    }
