import json
import os
from pathlib import Path


class StorageError(ValueError):
    """Raised when repository-scoped state cannot be stored safely."""


class RepositoryStorage:
    """Small filesystem store isolated by immutable GitHub repository id."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def claim_delivery(self, repository_id: int, delivery_id: str) -> bool:
        """Reserve a delivery. Call complete_delivery only after publication."""
        directory = self._repository_directory(repository_id) / "deliveries"
        directory.mkdir(parents=True, exist_ok=True)
        key = _safe_key(delivery_id, "delivery id")
        path = directory / key
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("in_progress\n")
        return True

    def complete_delivery(self, repository_id: int, delivery_id: str) -> None:
        path = self._delivery_path(repository_id, delivery_id)
        if not path.exists():
            raise StorageError("Delivery must be claimed before completion.")
        path.write_text("complete\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def release_delivery(self, repository_id: int, delivery_id: str) -> None:
        path = self._delivery_path(repository_id, delivery_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def get(self, repository_id: int, key: str):
        path = self._state_path(repository_id, key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, repository_id: int, key: str, value) -> None:
        path = self._state_path(repository_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _state_path(self, repository_id: int, key: str) -> Path:
        return self._repository_directory(repository_id) / "state" / (_safe_key(key, "key") + ".json")

    def _delivery_path(self, repository_id: int, delivery_id: str) -> Path:
        return self._repository_directory(repository_id) / "deliveries" / _safe_key(
            delivery_id, "delivery id"
        )

    def _repository_directory(self, repository_id: int) -> Path:
        if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id <= 0:
            raise StorageError("Repository id must be a positive integer.")
        return self.root / str(repository_id)


def _safe_key(value, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise StorageError(f"{label.capitalize()} must be a non-empty string.")
    if not all(character.isalnum() or character in "-_." for character in value):
        raise StorageError(f"Unsafe {label}.")
    if value in {".", ".."}:
        raise StorageError(f"Unsafe {label}.")
    return value
