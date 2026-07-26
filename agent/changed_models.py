import subprocess
from pathlib import Path
from agent.logging_config import get_logger

logger = get_logger(__name__)


def detect_changed_models(project_path: str | Path, diff_base: str = "origin/main") -> list[str]:
    """Infer changed dbt model names from git diff output.

    This helper is intentionally conservative: if the project is not in a git
    checkout or the diff cannot be computed, callers get an empty list and the
    legacy scan proceeds as "changed model not provided."
    """
    project = Path(project_path)
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{diff_base}...HEAD"],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as e:
        logger.debug(f"git diff failed: git executable not found or not a git repository: {e}")
        return []
    except subprocess.TimeoutExpired:
        logger.warning(f"git diff timed out after 30 seconds on {diff_base}...HEAD — skipping changed model detection")
        return []

    if completed.returncode != 0:
        if completed.stderr:
            logger.debug(f"git diff returned {completed.returncode}: {completed.stderr.strip()[:100]}")
        else:
            logger.debug(f"git diff returned {completed.returncode} (no error message)")
        return []

    models = _models_from_paths(completed.stdout.splitlines())
    if models:
        logger.debug(f"Detected changed models: {', '.join(models)}")
    return models


def _models_from_paths(paths: list[str]) -> list[str]:
    models = []
    for raw_path in paths:
        path = Path(str(raw_path).replace("\\", "/"))
        if path.suffix.casefold() != ".sql":
            continue
        if "models" not in {part.casefold() for part in path.parts}:
            continue
        model_name = path.stem
        if model_name not in models:
            models.append(model_name)
    return models
