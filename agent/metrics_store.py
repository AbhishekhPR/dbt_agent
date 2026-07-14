import copy
from datetime import datetime


def record_table_metrics(project_name: str, table_name: str, metrics: dict) -> None:
    return None


def record_schema_change(project_name: str, table_name: str, change: dict) -> None:
    return None


def record_test_results(project_name: str, test_records: list[dict], run_id: str | None = None) -> None:
    return None


def get_metric_history(project_name: str, table_name: str, days: int = 7) -> list[dict]:
    return []


def get_schema_change_history(project_name: str, table_name: str | None = None, days: int = 7) -> list[dict]:
    return []


def get_freshness_history(project_name: str, table_name: str, days: int = 7) -> list[dict]:
    return []


def get_project_summary(project_name: str) -> dict:
    return {
        "project_name": project_name,
        "generated_at": datetime.utcnow().isoformat(),
        "tables_tracked": 0,
        "schema_changes_7d": 0,
        "test_failures_7d": 0,
        "stale_tables_24h": 0,
    }
