from __future__ import annotations

import json
import re
import sqlite3
import uuid


class DeliveryJournal:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def ensure_schema(self):
        self.connection.executescript("CREATE TABLE IF NOT EXISTS delivery_journal (journal_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, repository_id TEXT NOT NULL, environment TEXT NOT NULL, channel TEXT NOT NULL, event_key TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, UNIQUE (organization_id, repository_id, environment, channel, event_key));")
        self.connection.commit()

    def record(self, organization_id, repository_id, environment, *, channel, event_key, payload, enabled=True, max_attempts=3):
        existing = self.connection.execute("SELECT * FROM delivery_journal WHERE organization_id=? AND repository_id=? AND environment=? AND channel=? AND event_key=?", (organization_id, repository_id, environment, channel, event_key)).fetchone()
        if existing:
            return {**dict(existing), "duplicate": True}
        status = "PENDING" if enabled else "DISABLED"
        journal_id = str(uuid.uuid4())
        safe = _redact(payload)
        self.connection.execute("INSERT INTO delivery_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)", (journal_id, organization_id, repository_id, environment, channel, event_key, json.dumps(safe, sort_keys=True), status))
        self.connection.commit()
        return {"journal_id": journal_id, "channel": channel, "status": status, "duplicate": False, "max_attempts": max_attempts}

    def fail(self, journal_id):
        row = self.get(journal_id)
        if not row or row["status"] in {"DEAD_LETTER", "DISABLED"}:
            return row
        attempts = int(row["attempts"]) + 1
        status = "DEAD_LETTER" if attempts >= 2 else "RETRY"
        self.connection.execute("UPDATE delivery_journal SET attempts=?, status=? WHERE journal_id=?", (attempts, status, journal_id)); self.connection.commit()
        return self.get(journal_id)

    def get(self, journal_id):
        row = self.connection.execute("SELECT * FROM delivery_journal WHERE journal_id=?", (journal_id,)).fetchone()
        return dict(row) if row else None

    def list(self, organization_id, repository_id, environment):
        return [dict(row) for row in self.connection.execute("SELECT * FROM delivery_journal WHERE organization_id=? AND repository_id=? AND environment=?", (organization_id, repository_id, environment))]


def _redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"token|secret|password|api[_-]?key", str(key), re.I) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and ("select " in value.lower() or " from " in value.lower()):
        return "[SQL REDACTED]"
    return value
