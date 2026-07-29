import json
from dataclasses import dataclass, replace
from pathlib import Path

from agent.diagnose import FailureDiagnosis, diagnose_failure


class RunResultsError(ValueError):
    """Raised when dbt run results cannot be safely interpreted."""


@dataclass(frozen=True)
class WatchReport:
    diagnoses: tuple[FailureDiagnosis, ...]
    malformed_entries: int = 0


def run_post_hook(project_path: str | Path) -> WatchReport:
    """Read dbt artifacts and return deterministic model diagnoses.

    This service is intentionally read-only. It performs no rendering,
    remediation, repository mutation, messaging, or network access.
    """
    results_path = Path(project_path) / "target" / "run_results.json"
    if not results_path.is_file():
        raise RunResultsError(f"run_results.json not found: {results_path}")

    try:
        document = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunResultsError(
            f"Invalid run_results.json: {results_path}"
        ) from error

    if not isinstance(document, dict) or not isinstance(
        document.get("results"),
        list,
    ):
        raise RunResultsError(
            "run_results.json must contain a 'results' list"
        )

    diagnoses: list[FailureDiagnosis] = []
    malformed_entries = 0
    for entry in document["results"]:
        if not isinstance(entry, dict):
            malformed_entries += 1
            continue

        status = entry.get("status")
        if not isinstance(status, str):
            malformed_entries += 1
            continue
        if status.lower() != "error":
            continue

        unique_id = entry.get("unique_id")
        if not isinstance(unique_id, str) or not unique_id.strip():
            malformed_entries += 1
            continue
        if not unique_id.startswith("model."):
            continue

        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            malformed_entries += 1
            continue

        compiled_code = entry.get("compiled_code")
        model_sql = compiled_code if isinstance(compiled_code, str) else ""
        relation_name = entry.get("relation_name")
        metadata = {"unique_id": unique_id}
        if isinstance(relation_name, str) and relation_name.strip():
            metadata["relation_name"] = relation_name

        diagnosis = diagnose_failure(message, model_sql, "")
        diagnoses.append(
            replace(
                diagnosis,
                affected_model=unique_id.rsplit(".", 1)[-1],
                metadata=metadata,
            )
        )

    return WatchReport(
        diagnoses=tuple(diagnoses),
        malformed_entries=malformed_entries,
    )
