import subprocess
from pathlib import Path


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
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return _models_from_paths(completed.stdout.splitlines())


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
