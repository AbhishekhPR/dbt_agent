import os
import sqlite3
from pathlib import Path

from agent.ast_analyzer import run_ast_analysis
from agent.ast_analyzer import to_signal as ast_to_signal
from agent.decision_assembly import (
    assemble_pipeline_incident,
    summarize_pipeline_incident,
)
from agent.metadata_checks import run_metadata_checks
from agent.metadata_checks import to_signal as metadata_checks_to_signal
from agent.metadata_drift import to_signal as metadata_drift_to_signal
from agent.metadata_store import (
    MetadataStoreError,
    ModelMetricRecord,
    ScanRunRecord,
    fetch_recent_model_metrics,
    insert_model_metrics,
    insert_scan_run,
)
from agent.pipeline_validation_report import write_pipeline_validation_report


PROJECT_NAME = "relium_demo"
MODEL_NAME = "fct_customer_lifetime_value"
DEFAULT_SCENARIO = "normal"
DEMO_ARTIFACT_NAMES = {
    "warehouse_db_path": "demo_pipeline.db",
    "metadata_db_path": "relium_metadata.db",
    "markdown_report_path": "pipeline_validation_report.md",
    "json_report_path": "pipeline_validation_report.json",
}
SCENARIO_FIXTURES = {
    "normal": {
        "customers": [
            (1, "Ada", 0, "2026-06-20T10:00:00"),
            (2, "Ben", 1, "2026-06-21T10:00:00"),
            (3, "Cleo", 0, "2026-06-22T10:00:00"),
        ],
        "orders": [
            (101, 1, 120.0, "2026-06-20T11:00:00", "2026-06-20T12:00:00"),
            (102, 1, 80.0, "2026-06-21T11:00:00", "2026-06-21T12:00:00"),
            (103, 2, 60.0, "2026-06-22T11:00:00", "2026-06-22T12:00:00"),
            (104, 99, 45.0, "2026-06-23T11:00:00", "2026-06-23T12:00:00"),
        ],
    },
    "row-drop": {
        "customers": [
            (1, "Ada", 0, "2026-06-20T10:00:00"),
            (2, "Ben", 1, "2026-06-21T10:00:00"),
            (3, "Cleo", 0, "2026-06-22T10:00:00"),
        ],
        "orders": [
            (101, 1, 120.0, "2026-06-20T11:00:00", "2026-06-20T12:00:00"),
            (103, 2, 60.0, "2026-06-22T11:00:00", "2026-06-22T12:00:00"),
            (104, 99, 45.0, "2026-06-23T11:00:00", "2026-06-23T12:00:00"),
        ],
    },
    "duplicate-spike": {
        "customers": [
            (1, "Ada", 0, "2026-06-20T10:00:00"),
            (2, "Ben", 1, "2026-06-21T10:00:00"),
            (3, "Cleo", 0, "2026-06-22T10:00:00"),
        ],
        "orders": [
            (101, 1, 120.0, "2026-06-20T11:00:00", "2026-06-20T12:00:00"),
            (102, 1, 80.0, "2026-06-21T11:00:00", "2026-06-21T12:00:00"),
            (105, 1, 40.0, "2026-06-22T11:00:00", "2026-06-22T12:00:00"),
            (106, 1, 70.0, "2026-06-23T11:00:00", "2026-06-23T12:00:00"),
            (107, 1, 65.0, "2026-06-24T10:00:00", "2026-06-24T11:00:00"),
            (108, 1, 95.0, "2026-06-24T11:30:00", "2026-06-24T12:00:00"),
            (103, 2, 60.0, "2026-06-22T11:00:00", "2026-06-22T12:00:00"),
            (104, 99, 45.0, "2026-06-23T11:00:00", "2026-06-23T12:00:00"),
        ],
    },
    "freshness-regression": {
        "customers": [
            (1, "Ada", 0, "2026-06-18T10:00:00"),
            (2, "Ben", 1, "2026-06-18T11:00:00"),
            (3, "Cleo", 0, "2026-06-18T12:00:00"),
        ],
        "orders": [
            (101, 1, 120.0, "2026-06-18T11:00:00", "2026-06-18T12:00:00"),
            (102, 1, 80.0, "2026-06-19T11:00:00", "2026-06-19T12:00:00"),
            (103, 2, 60.0, "2026-06-19T11:30:00", "2026-06-19T11:45:00"),
            (104, 99, 45.0, "2026-06-19T11:50:00", "2026-06-19T11:55:00"),
        ],
    },
}
RISKY_MODEL_SQL = """
SELECT
    o.order_id,
    o.customer_id,
    c.customer_name,
    o.order_total,
    o.created_at,
    o.updated_at
FROM raw_orders o
LEFT JOIN raw_customers c
    ON o.customer_id = c.customer_id
WHERE c.is_deleted = 0
"""


class DemoPipelineError(ValueError):
    """Raised when an isolated demo workspace cannot be used safely."""


def prepare_demo_workspace(workspace_path: str | Path) -> dict[str, Path]:
    supplied = Path(workspace_path).expanduser()
    absolute = Path(os.path.abspath(supplied))
    if absolute.exists() and not absolute.is_dir():
        raise DemoPipelineError(f"Demo workspace is not a directory: {supplied}")
    if _has_redirecting_component(absolute):
        raise DemoPipelineError(
            f"Demo workspace may not use symbolic links or junctions: {supplied}"
        )
    try:
        absolute.mkdir(parents=True, exist_ok=True)
        workspace = absolute.resolve(strict=True)
    except OSError as error:
        raise DemoPipelineError(
            f"Could not create demo workspace {supplied}: {error}"
        ) from error

    paths = {
        key: workspace / filename
        for key, filename in DEMO_ARTIFACT_NAMES.items()
    }
    for path in paths.values():
        if path.parent != workspace or path.name not in DEMO_ARTIFACT_NAMES.values():
            raise DemoPipelineError(f"Demo artifact escapes workspace: {path}")
        if _is_redirecting_path(path):
            raise DemoPipelineError(
                f"Demo artifact may not be a symbolic link or junction: {path}"
            )
    paths["workspace_path"] = workspace
    return paths


def run_demo_pipeline(
    *,
    metadata_db_path: str | Path,
    warehouse_db_path: str | Path,
    markdown_report_path: str | Path,
    json_report_path: str | Path,
    scenario: str = DEFAULT_SCENARIO,
) -> dict:
    """Run one stateful demo entirely within four validated artifact paths.

    Warehouse and metadata commits precede report replacement. A later report
    failure does not roll back already committed demo database state.
    """
    if scenario not in SCENARIO_FIXTURES:
        supported = ", ".join(sorted(SCENARIO_FIXTURES))
        raise DemoPipelineError(
            f"Unsupported demo scenario '{scenario}'. Expected one of: {supported}"
        )
    paths = _validate_artifact_paths(
        metadata_db_path=metadata_db_path,
        warehouse_db_path=warehouse_db_path,
        markdown_report_path=markdown_report_path,
        json_report_path=json_report_path,
    )
    metadata_db_path = paths["metadata_db_path"]
    warehouse_path = paths["warehouse_db_path"]

    try:
        conn = sqlite3.connect(warehouse_path)
        try:
            raw_row_count = _load_demo_raw_tables(conn, scenario)
            _build_demo_model(conn)
            ast_report = run_ast_analysis(RISKY_MODEL_SQL, MODEL_NAME)
            metadata_result = run_metadata_checks(conn, MODEL_NAME, ["customer_id"])
        finally:
            conn.close()
    except sqlite3.Error as error:
        raise DemoPipelineError(
            f"Could not build demo warehouse {warehouse_path}: {error}"
        ) from error

    severity = ast_report["overall_risk"].upper()
    safe_to_continue = severity not in {"HIGH", "CRITICAL"} and not (
        metadata_result.null_count > 0 or metadata_result.duplicate_count > 0
    )

    validation_report_text = _build_report_text(
        raw_row_count=raw_row_count,
        severity=severity,
        metadata_result=metadata_result,
        safe_to_continue=safe_to_continue,
    )
    try:
        scan_id = insert_scan_run(
            metadata_db_path,
            ScanRunRecord(
                project_name=PROJECT_NAME,
                model_name=MODEL_NAME,
                risk_level=severity,
                safe_to_merge=safe_to_continue,
                affected_models=[],
                report_text=validation_report_text,
            ),
        )
        insert_model_metrics(
            metadata_db_path,
            ModelMetricRecord(
                scan_id=scan_id,
                project_name=PROJECT_NAME,
                model_name=MODEL_NAME,
                row_count=metadata_result.row_count,
                null_count=metadata_result.null_count,
                duplicate_count=metadata_result.duplicate_count,
                freshness_timestamp=metadata_result.freshness_timestamp,
                schema_column_count=metadata_result.schema_column_count,
            ),
        )
    except (OSError, sqlite3.Error) as error:
        raise DemoPipelineError(
            f"Could not record demo metadata in {metadata_db_path}: {error}"
        ) from error

    recommendation = (
        "Review the SQL transformation before deployment. "
        "The current change may alter downstream analytics outputs."
    )
    try:
        drift_result = _build_drift_signal(metadata_db_path)
    except MetadataStoreError as error:
        raise DemoPipelineError(
            f"Could not read demo metadata from {metadata_db_path}: {error}"
        ) from error

    result = {
        "project_name": PROJECT_NAME,
        "scenario": scenario,
        "scan_id": scan_id,
        "raw_row_count": raw_row_count,
        "model_name": MODEL_NAME,
        "changed_model": MODEL_NAME,
        "severity": severity,
        "static_analysis_text": "Potential LEFT JOIN nullification detected.",
        "sql_risks": ast_report.get("bugs", []),
        "affected_models": [],
        "recommendation": recommendation,
        "drift_result": drift_result,
        "row_count": metadata_result.row_count,
        "null_count": metadata_result.null_count,
        "duplicate_count": metadata_result.duplicate_count,
        "freshness_timestamp": metadata_result.freshness_timestamp,
        "schema_column_count": metadata_result.schema_column_count,
        "safe_to_continue": safe_to_continue,
        "metadata_stored": True,
    }
    result["report_text"] = format_demo_pipeline_report(result)
    ast_signal = ast_to_signal(ast_report)
    metadata_signal = metadata_checks_to_signal(
        metadata_result,
        safe_to_continue=safe_to_continue,
    )
    drift_signal = (
        metadata_drift_to_signal(drift_result)
        if drift_result is not None
        else None
    )
    incident = assemble_pipeline_incident(
        ast_signal=ast_signal,
        metadata_signal=metadata_signal,
        drift_signal=drift_signal,
        affected_models=result["affected_models"],
        metadata={
            "project_name": PROJECT_NAME,
            "model_name": MODEL_NAME,
            "scenario": scenario,
            "scan_id": scan_id,
        },
    )
    result["incident"] = incident
    result["incident_summary"] = summarize_pipeline_incident(incident)
    write_pipeline_validation_report(
        result,
        paths["markdown_report_path"],
        paths["json_report_path"],
    )
    return result


def format_demo_pipeline_report(result: dict) -> str:
    return "\n".join(
        [
            "Relium Demo Pipeline",
            "",
            f"Scenario: {result['scenario']}",
            f"Raw rows loaded: {result['raw_row_count']}",
            f"Model built: {result['model_name']}",
            f"AST risk found: {result['severity']}",
            f"Row count: {result['row_count']}",
            f"Null count: {result['null_count']}",
            f"Duplicate customer_id count: {result['duplicate_count']}",
            f"Freshness timestamp: {result['freshness_timestamp']}",
            f"Schema columns: {result['schema_column_count']}",
            f"Safe to continue: {'YES' if result['safe_to_continue'] else 'NO'}",
            f"Metadata stored: {'YES' if result['metadata_stored'] else 'NO'}",
        ]
    )


def _load_demo_raw_tables(conn: sqlite3.Connection, scenario: str) -> int:
    fixture = SCENARIO_FIXTURES.get(scenario)
    if fixture is None:
        supported = ", ".join(sorted(SCENARIO_FIXTURES))
        raise ValueError(f"Unsupported demo scenario '{scenario}'. Expected one of: {supported}")
    conn.executescript(
        """
        DROP TABLE IF EXISTS raw_customers;
        DROP TABLE IF EXISTS raw_orders;
        DROP TABLE IF EXISTS fct_customer_lifetime_value;

        CREATE TABLE raw_customers (
            customer_id INTEGER PRIMARY KEY,
            customer_name TEXT,
            is_deleted INTEGER,
            updated_at TEXT
        );

        CREATE TABLE raw_orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            order_total REAL,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    customers = fixture["customers"]
    orders = fixture["orders"]
    conn.executemany("INSERT INTO raw_customers VALUES (?, ?, ?, ?)", customers)
    conn.executemany("INSERT INTO raw_orders VALUES (?, ?, ?, ?, ?)", orders)
    conn.commit()
    return len(customers) + len(orders)


def _build_demo_model(conn: sqlite3.Connection) -> None:
    conn.execute(f"CREATE TABLE {MODEL_NAME} AS {RISKY_MODEL_SQL}")
    conn.commit()


def _build_report_text(
    raw_row_count: int,
    severity: str,
    metadata_result,
    safe_to_continue: bool,
) -> str:
    lines = [
        f"Raw rows loaded: {raw_row_count}",
        f"AST risk found: {severity}",
        f"Row count: {metadata_result.row_count}",
        f"Null count: {metadata_result.null_count}",
        f"Duplicate count: {metadata_result.duplicate_count}",
        f"Freshness timestamp: {metadata_result.freshness_timestamp}",
        f"Schema columns: {metadata_result.schema_column_count}",
        f"Safe to continue: {'YES' if safe_to_continue else 'NO'}",
    ]
    if metadata_result.anomalies:
        lines.append("Anomalies: " + "; ".join(metadata_result.anomalies))
    return "\n".join(lines)


def _build_drift_signal(metadata_db_path: str | Path) -> dict | None:
    from agent.metadata_drift import _drift_level, _freshness_regressed, _pct_change

    recent_metrics = fetch_recent_model_metrics(
        metadata_db_path,
        project_name=PROJECT_NAME,
        model_name=MODEL_NAME,
        limit=2,
    )
    if len(recent_metrics) < 2:
        return None

    current = recent_metrics[0]
    previous = recent_metrics[1]
    row_count_change_pct = _pct_change(previous["row_count"], current["row_count"])
    null_count_change_pct = _pct_change(previous["null_count"], current["null_count"])
    duplicate_count_change_pct = _pct_change(
        previous["duplicate_count"],
        current["duplicate_count"],
    )
    schema_column_count_change = (
        current["schema_column_count"] - previous["schema_column_count"]
    )
    freshness_regressed = _freshness_regressed(
        previous.get("freshness_timestamp"),
        current.get("freshness_timestamp"),
    )
    drift_level = _drift_level(
        row_count_change_pct=row_count_change_pct,
        null_count_change_pct=0.0,
        duplicate_count_change_pct=duplicate_count_change_pct,
        schema_column_count_change=0,
        freshness_regressed=freshness_regressed,
    )
    return {
        "previous_run_timestamp": previous.get("timestamp"),
        "current_run_timestamp": current.get("timestamp"),
        "row_count_change_pct": row_count_change_pct,
        "null_count_change_pct": null_count_change_pct,
        "duplicate_count_change_pct": duplicate_count_change_pct,
        "schema_column_count_change": schema_column_count_change,
        "freshness_regressed": freshness_regressed,
        "drift_level": drift_level,
    }


def _validate_artifact_paths(**artifact_paths) -> dict[str, Path]:
    paths = {
        key: Path(value).expanduser()
        for key, value in artifact_paths.items()
    }
    for key, path in paths.items():
        expected_name = DEMO_ARTIFACT_NAMES[key]
        if path.name != expected_name:
            raise DemoPipelineError(
                f"Demo artifact must be named {expected_name}: {path}"
            )
        if _has_redirecting_component(path.parent) or _is_redirecting_path(path):
            raise DemoPipelineError(
                f"Demo artifact may not use symbolic links or junctions: {path}"
            )
    try:
        parents = {path.parent.resolve(strict=True) for path in paths.values()}
    except OSError as error:
        raise DemoPipelineError(f"Could not resolve demo workspace: {error}") from error
    if len(parents) != 1:
        raise DemoPipelineError("All demo artifacts must share one workspace.")
    workspace = parents.pop()
    for key, path in paths.items():
        expected_name = DEMO_ARTIFACT_NAMES[key]
        resolved_parent = path.parent.resolve(strict=True)
        if resolved_parent != workspace:
            raise DemoPipelineError(
                f"Demo artifact must be {workspace / expected_name}: {path}"
            )
        paths[key] = workspace / expected_name
    return paths


def _has_redirecting_component(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_redirecting_path(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _is_redirecting_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())
