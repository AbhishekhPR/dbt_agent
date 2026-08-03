from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.lifecycle_models import ALLOWED_TRANSITIONS


class DeploymentEventProcessor:
    def __init__(self, store):
        self.store = store

    def process(self, event: dict) -> dict:
        event_id = event.get("event_id")
        if not event_id or not event.get("deployment_id") or not event.get("event_type"):
            return {"status": "REJECTED_PAYLOAD", "event_id": event_id}
        org, repo, env = (event.get("organization_id"), event.get("repository_id"), event.get("environment"))
        receipt = self.store.connection.execute("SELECT response FROM event_receipts WHERE event_id=?", (event_id,)).fetchone()
        if receipt:
            return {**json.loads(receipt["response"]), "duplicate": True}
        try:
            self.store._tenant(org, repo, env)
        except ValueError:
            return self._receipt(event, {"status": "REJECTED_TENANT", "event_id": event_id})
        event_type = str(event["event_type"])
        deployment_id = event["deployment_id"]
        if event_type == "reviewed":
            result = self.store.create_deployment(org, repo, env, {"deployment_id": deployment_id, **(event.get("payload") or {})})
            result = {"status": result["status"], "deployment_id": deployment_id, "event_id": event_id}
            return self._receipt(event, result)
        row = self.store.connection.execute("SELECT status FROM deployments WHERE deployment_id=? AND organization_id=? AND repository_id=? AND environment=?", (deployment_id, org, repo, env)).fetchone()
        if not row or event_type not in ALLOWED_TRANSITIONS.get(row["status"], set()):
            return self._receipt(event, {"status": "REJECTED_OUT_OF_ORDER", "event_id": event_id, "deployment_id": deployment_id})
        self.store.append_transition(org, repo, env, deployment_id, event_type)
        return self._receipt(event, {"status": event_type, "event_id": event_id, "deployment_id": deployment_id})

    def _receipt(self, event, response):
        self.store.connection.execute("INSERT INTO event_receipts VALUES (?, ?, ?, ?, ?, ?, ?)", (event["event_id"], event.get("organization_id"), event.get("repository_id"), event.get("environment"), response["status"], json.dumps(response, sort_keys=True), datetime.now(timezone.utc).isoformat()))
        self.store.connection.commit()
        return response
