import json
import os
import base64
import tempfile
import threading
import uuid
from pathlib import Path


class StorageError(ValueError):
    """Raised when repository-scoped state cannot be stored safely."""


class RepositoryStorage:
    """Small filesystem store isolated by immutable GitHub repository id."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.recovery_issues = []

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

    def persist_verified_job(self, repository_id: int, job) -> bool:
        """Durably reserve a verified webhook before it is acknowledged."""
        from agent.github_app.jobs import WebhookJob

        if not isinstance(job, WebhookJob):
            raise StorageError("Verified webhook job is invalid.")
        directory = self._repository_directory(repository_id) / "jobs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (_safe_key(job.delivery_id, "delivery id") + ".json")
        payload = {
            "repository_id": int(repository_id),
            "delivery_id": job.delivery_id,
            "event_name": job.event_name,
            "raw_body": base64.b64encode(job.raw_body).decode("ascii"),
            "received_at": job.received_at,
            "attempt": job.attempt,
            "state": "verified_pending",
            "owner": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "retry_at": None,
            "last_error": None,
        }
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            with self._write_lock:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    _write_json_stream(stream, payload)
                _sync_directory(directory)
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return True

    def recover_jobs(self, repository_id: int, *, now: float) -> list:
        from agent.github_app.jobs import WebhookJob

        directory = self._repository_directory(repository_id) / "jobs"
        if not directory.exists():
            return []
        recovered = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self._quarantine_corrupt(path, exc)
                continue
            state = payload.get("state")
            retry_at = payload.get("retry_at")
            lease_expired = (
                state in {"claimed", "processing"}
                and isinstance(payload.get("lease_expires_at"), (int, float))
                and payload["lease_expires_at"] <= now
            )
            retryable = state == "verified_pending" or (
                state == "retry_at"
                and (retry_at is None or retry_at <= now)
            )
            if lease_expired:
                payload.update(
                    {
                        "state": "verified_pending",
                        "owner": None,
                        "claimed_at": None,
                        "lease_expires_at": None,
                    }
                )
                _atomic_json_write(path, payload)
                retryable = True
            if not retryable:
                continue
            recovered.append(_job_from_payload(payload))
        return recovered

    def recover_all_jobs(self, *, now: float) -> list:
        recovered = []
        if not self.root.exists():
            return recovered
        for directory in sorted(self.root.iterdir()):
            if directory.is_dir() and directory.name.isdigit():
                recovered.extend(self.recover_jobs(int(directory.name), now=now))
        return recovered

    def claim_job(self, repository_id: int, delivery_id: str, *, owner: str, now: float, lease_seconds: float) -> bool:
        path = self._job_path(repository_id, delivery_id)
        payload = self._read_json(path)
        if payload is None or payload.get("state") in {"complete", "dead_letter"}:
            return False
        if payload.get("state") == "claimed":
            lease = payload.get("lease_expires_at")
            if not isinstance(lease, (int, float)) or lease > now:
                return False
        payload.update(
            {
                "state": "claimed",
                "owner": str(owner),
                "claimed_at": float(now),
                "lease_expires_at": float(now + lease_seconds),
            }
        )
        _atomic_json_write(path, payload)
        return True

    def mark_processing(self, repository_id: int, delivery_id: str, *, owner: str) -> None:
        self._update_job(repository_id, delivery_id, state="processing", owner=str(owner))

    def complete_job(self, repository_id: int, delivery_id: str) -> None:
        self._update_job(
            repository_id,
            delivery_id,
            state="complete",
            owner=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def fail_job(self, repository_id: int, delivery_id: str, *, attempt: int, last_error: str, retry_at: float | None = None, dead_letter: bool = False) -> None:
        self._update_job(
            repository_id,
            delivery_id,
            state="dead_letter" if dead_letter else "retry_at",
            attempt=int(attempt),
            last_error=str(last_error)[:500],
            retry_at=retry_at,
            owner=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def record_publication_step(self, repository_id: int, publication_id: str, step: str, value) -> None:
        _validate_publication_step(step)
        path = self._publication_path(repository_id, publication_id)
        with self._write_lock:
            payload = self._read_json(path) or {}
            payload[step] = value
            _atomic_json_write(path, payload)

    def claim_publication_step(self, repository_id: int, publication_id: str, step: str, value) -> bool:
        """Atomically store a publication step only when it has no prior state."""
        _validate_publication_step(step)
        path = self._publication_path(repository_id, publication_id)
        with self._write_lock:
            payload = self._read_json(path) or {}
            if step in payload:
                return False
            payload[step] = value
            _atomic_json_write(path, payload)
            return True

    def transition_publication_step(
        self,
        repository_id: int,
        publication_id: str,
        step: str,
        *,
        expected_state: str,
        value,
    ):
        """Replace a step only while its persisted state matches expectation."""
        _validate_publication_step(step)
        path = self._publication_path(repository_id, publication_id)
        with self._write_lock:
            payload = self._read_json(path) or {}
            current = payload.get(step)
            if not isinstance(current, dict) or current.get("state") != expected_state:
                return current
            payload[step] = value
            _atomic_json_write(path, payload)
            return value

    def get_publication_journal(self, repository_id: int, publication_id: str) -> dict:
        return self._read_json(self._publication_path(repository_id, publication_id)) or {}

    def get_job(self, repository_id: int, delivery_id: str) -> dict | None:
        return self._read_json(self._job_path(repository_id, delivery_id))

    def _update_job(self, repository_id: int, delivery_id: str, **updates) -> None:
        path = self._job_path(repository_id, delivery_id)
        payload = self._read_json(path)
        if payload is None:
            raise StorageError("Webhook job is not persisted.")
        payload.update(updates)
        _atomic_json_write(path, payload)

    def _read_json(self, path: Path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError("Persisted webhook state is invalid.") from exc

    def _job_path(self, repository_id: int, delivery_id: str) -> Path:
        return self._repository_directory(repository_id) / "jobs" / (_safe_key(delivery_id, "delivery id") + ".json")

    def _publication_path(self, repository_id: int, publication_id: str) -> Path:
        return self._repository_directory(repository_id) / "publications" / (_safe_key(publication_id, "publication id") + ".json")

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

    def _quarantine_corrupt(self, path: Path, error: Exception) -> None:
        quarantine = path.parent / "corrupt"
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / f"{path.name}.{uuid.uuid4().hex}"
        try:
            os.replace(path, target)
        except OSError as exc:
            raise StorageError("Corrupt persisted webhook state could not be quarantined.") from exc
        self.recovery_issues.append(
            {"path": str(path), "quarantined_to": str(target), "error": type(error).__name__}
        )


def _safe_key(value, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise StorageError(f"{label.capitalize()} must be a non-empty string.")
    if not all(character.isalnum() or character in "-_." for character in value):
        raise StorageError(f"Unsafe {label}.")
    if value in {".", ".."}:
        raise StorageError(f"Unsafe {label}.")
    return value


def _validate_publication_step(step: str) -> None:
    if step not in {"comment", "check", "slack"}:
        raise StorageError("Unknown publication step.")


def _write_json_stream(stream, payload) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    stream.flush()
    os.fsync(stream.fileno())


def _atomic_json_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    with _ATOMIC_WRITE_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                _write_json_stream(stream, payload)
            os.replace(temporary, path)
            _sync_directory(path.parent)
        except Exception:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            raise


_ATOMIC_WRITE_LOCK = threading.RLock()


def _sync_directory(directory: Path) -> None:
    """Best-effort directory durability.

    POSIX filesystems permit fsync on the parent directory after rename. Some
    Windows filesystems reject directory handles; there the file flush plus
    atomic replace is the strongest supported guarantee and this step is
    intentionally skipped.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _job_from_payload(payload):
    from agent.github_app.jobs import WebhookJob

    return WebhookJob(
        payload["delivery_id"],
        payload["event_name"],
        base64.b64decode(payload["raw_body"]),
        payload["received_at"],
        attempt=payload.get("attempt", 0),
        state=payload.get("state", "verified_pending"),
        owner=payload.get("owner"),
        claimed_at=payload.get("claimed_at"),
        lease_expires_at=payload.get("lease_expires_at"),
        retry_at=payload.get("retry_at"),
        last_error=payload.get("last_error"),
        repository_id=payload.get("repository_id"),
    )
