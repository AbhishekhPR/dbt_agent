"""Service layer between the HTTP handlers and the PostgreSQL lifecycle store.

Handlers never touch the store or a database object directly; they call this
layer, which enforces tenant scope, idempotency and transition legality, and
returns plain dictionaries safe for serialization.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from agent.api.validation import isoformat

# An observation reported this far behind its arrival is flagged as late rather
# than silently reordered.
_LATE_THRESHOLD = timedelta(minutes=5)

# Idempotent replays are recognised by hashing the caller's canonical payload.
def payload_digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class ConflictError(Exception):
    """A documented state conflict, surfaced as HTTP 409."""


class NotFoundError(Exception):
    """The resource does not exist within the caller's authorized scope.

    Also raised for a resource that exists in another tenant, so an
    out-of-scope resource is indistinguishable from an absent one.
    """


def scoped_integrity_error(exc) -> Exception:
    """Translate a database integrity error into a documented HTTP outcome.

    Composite tenant keys mean a violation here is either a same-tenant
    conflict or a reference the caller is not entitled to. Neither may escape
    as a raw 500, and neither may disclose which of the two it was.
    """
    name = type(exc).__name__
    if name == "ForeignKeyViolation":
        # The referenced resource is absent from the caller's scope.
        return NotFoundError("referenced resource not found in scope")
    if name == "UniqueViolation":
        return ConflictError("resource already exists")
    return ConflictError("request conflicts with existing state")


class LifecycleService:
    """Tenant-scoped operations over the merged PostgreSQL lifecycle store."""

    def __init__(self, store):
        self.store = store

    # -- idempotency ---------------------------------------------------------

    def _claim(self, scope, environment, key, payload, *, resource_kind):
        """Claim an idempotency key, or return the prior effective result.

        Returns ``(receipt, replayed)``. A replay with a conflicting payload
        raises ConflictError rather than silently returning a stale result.
        """
        digest = payload_digest(payload)
        # Receipts are keyed per tenant, so another tenant's use of the same key
        # is simply invisible here rather than something to compare against.
        existing = self.store.get_event_receipt(
            scope.organization_id, scope.repository_id, key
        )
        if existing is not None:
            if existing.get("payload_hash") != digest:
                raise ConflictError(
                    "idempotency key was already used with a different payload"
                )
            return existing, True
        claimed = self.store.record_event_receipt(
            key, scope.organization_id, scope.repository_id, environment,
            status="accepted", response={}, payload_hash=digest,
            resource_kind=resource_kind,
        )
        if claimed is None:
            # Lost a concurrent race for the same key; re-read the winner.
            existing = self.store.get_event_receipt(
                scope.organization_id, scope.repository_id, key
            )
            if existing is None:
                raise ConflictError("idempotency key contention")
            if existing.get("payload_hash") != digest:
                raise ConflictError(
                    "idempotency key was already used with a different payload"
                )
            return existing, True
        return claimed, False

    def _finalize(self, scope, key, response, *, resource_id=None):
        self.store.connection.execute(
            "UPDATE event_receipts SET response=%s, resource_id=%s "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (self.store._Jsonb(response), resource_id,
             scope.organization_id, scope.repository_id, key),
        )

    # -- deployment lifecycle -------------------------------------------------

    def submit_deployment_event(self, scope, *, environment, deployment_id, event_type,
                                idempotency_key, payload):
        environment = scope.require_environment(environment)
        self.store.ensure_tenant(scope.organization_id, scope.repository_id, environment)

        receipt, replayed = self._claim(
            scope, environment, idempotency_key,
            {"deployment_id": deployment_id, "event_type": event_type, "payload": payload},
            resource_kind="deployment_event",
        )
        if replayed:
            return dict(receipt.get("response") or {}, replayed=True), False

        existing = self.store.get_deployment(scope.organization_id, scope.repository_id, deployment_id)
        if existing is None:
            record = self.store.create_deployment(
                scope.organization_id, scope.repository_id, environment,
                {"deployment_id": deployment_id, **payload},
            )
            status = record["status"]
        else:
            if existing["environment"] != environment:
                raise ConflictError("deployment belongs to a different environment")
            status = existing["status"]

        transition_applied = False
        deferred = None
        if event_type != "created":
            status_before = status
            try:
                self.store.append_transition(
                    scope.organization_id, scope.repository_id, environment,
                    deployment_id, event_type,
                )
            except ValueError as exc:
                # Illegal or out-of-order: rejected whole, never partially applied.
                deferred = str(exc)
            current = self.store.get_deployment(
                scope.organization_id, scope.repository_id, deployment_id
            )
            if current is None:
                # The deployment is not present in the caller's scope. Report
                # the same absence as any other out-of-scope resource rather
                # than dereferencing nothing and surfacing a 500.
                raise NotFoundError("unknown deployment")
            status = current["status"]
            # Only claim a transition when the state actually moved. Replaying
            # the current status is an accepted no-op, not an applied change.
            transition_applied = deferred is None and status != status_before
            if deferred is None and not transition_applied:
                deferred = "event matches the current status; no transition recorded"

        self.store.append_audit(
            scope.organization_id, scope.repository_id,
            actor=f"token:{scope.token_id}", event_type=f"deployment.{event_type}",
            reference_type="deployment", reference_id=deployment_id,
            payload={"idempotency_key": idempotency_key},
        )

        response = {
            "deployment_id": deployment_id,
            "event_id": idempotency_key,
            "status": status,
            "transition_applied": transition_applied,
            "environment": environment,
        }
        if deferred:
            response["deferred_reason"] = deferred
        self._finalize(scope, idempotency_key, response, resource_id=deployment_id)
        return response, True

    # -- monitoring -----------------------------------------------------------

    def submit_baseline(self, scope, *, environment, model, baseline, observed_at,
                        evidence_coverage, source, idempotency_key):
        environment = scope.require_environment(environment)
        self.store.ensure_tenant(scope.organization_id, scope.repository_id, environment)
        receipt, replayed = self._claim(
            scope, environment, idempotency_key,
            {"model": model, "baseline": baseline, "observed_at": isoformat(observed_at)},
            resource_kind="metadata_baseline",
        )
        if replayed:
            return dict(receipt.get("response") or {}, replayed=True), False

        self.store.record_metadata_baseline(
            scope.organization_id, scope.repository_id, environment, model, baseline
        )
        self.store.connection.execute(
            "UPDATE metadata_baselines SET observed_at=%s, evidence_coverage=%s, source=%s "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s AND model=%s",
            (observed_at, evidence_coverage, source,
             scope.organization_id, scope.repository_id, environment, model),
        )
        response = {
            "model": model, "environment": environment,
            "observed_at": isoformat(observed_at),
            "evidence_coverage": evidence_coverage,
        }
        self._finalize(scope, idempotency_key, response, resource_id=model)
        return response, True

    def submit_observation(self, scope, *, environment, deployment_id, model, metric,
                           value_payload, observed_at, evidence_coverage, source,
                           idempotency_key):
        environment = scope.require_environment(environment)
        self.store.ensure_tenant(scope.organization_id, scope.repository_id, environment)
        receipt, replayed = self._claim(
            scope, environment, idempotency_key,
            {"deployment_id": deployment_id, "model": model, "metric": metric,
             "payload": value_payload, "observed_at": isoformat(observed_at)},
            resource_kind="monitoring_observation",
        )
        if replayed:
            return dict(receipt.get("response") or {}, replayed=True), False

        if deployment_id is not None:
            owner = self.store.get_deployment(
                scope.organization_id, scope.repository_id, deployment_id
            )
            if owner is None:
                raise NotFoundError("unknown deployment")

        observation_id = str(uuid.uuid4())
        received_at = datetime.now(timezone.utc)
        self.store.connection.execute(
            "INSERT INTO monitoring_observations "
            "(observation_id, organization_id, repository_id, environment, deployment_id, "
            "model, metric, payload, observed_at, received_at, evidence_coverage, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (observation_id, scope.organization_id, scope.repository_id, environment,
             deployment_id, model, metric, self.store._Jsonb(value_payload),
             observed_at, received_at, evidence_coverage, source),
        )
        late = observed_at < received_at - _LATE_THRESHOLD
        response = {
            "observation_id": observation_id,
            "environment": environment,
            "observed_at": isoformat(observed_at),
            "received_at": isoformat(received_at),
            "late": late,
            "evidence_coverage": evidence_coverage,
        }
        self._finalize(scope, idempotency_key, response, resource_id=observation_id)
        return response, True

    # -- anomalies -------------------------------------------------------------

    def submit_anomaly(self, scope, *, environment, deployment_id, kind, severity,
                       detected_at, affected_models, affected_kpis, observation_ids,
                       evidence, idempotency_key):
        environment = scope.require_environment(environment)
        self.store.ensure_tenant(scope.organization_id, scope.repository_id, environment)
        receipt, replayed = self._claim(
            scope, environment, idempotency_key,
            {"deployment_id": deployment_id, "kind": kind, "severity": severity,
             "evidence": evidence},
            resource_kind="anomaly",
        )
        if replayed:
            return dict(receipt.get("response") or {}, replayed=True), False

        if deployment_id is not None:
            owner = self.store.get_deployment(
                scope.organization_id, scope.repository_id, deployment_id
            )
            if owner is None:
                raise NotFoundError("unknown deployment")

        record = self.store.create_anomaly(
            scope.organization_id, scope.repository_id, environment,
            deployment_id=deployment_id, kind=kind, payload=evidence,
        )
        self.store.connection.execute(
            "UPDATE anomalies SET severity=%s, detected_at=%s, affected_models=%s, "
            "affected_kpis=%s, observation_ids=%s WHERE anomaly_id=%s",
            (severity, detected_at, self.store._Jsonb(affected_models),
             self.store._Jsonb(affected_kpis), self.store._Jsonb(observation_ids),
             record["anomaly_id"]),
        )
        response = {
            "anomaly_id": record["anomaly_id"],
            "deployment_id": deployment_id,
            "kind": kind,
            "severity": severity,
            "environment": environment,
        }
        self._finalize(scope, idempotency_key, response, resource_id=record["anomaly_id"])
        return response, True

    # -- incidents and RCA -----------------------------------------------------

    def request_incident_rca(self, scope, *, environment, anomaly_id, deployment_id,
                             incident_id, idempotency_key):
        environment = scope.require_environment(environment)
        receipt, replayed = self._claim(
            scope, environment, idempotency_key,
            {"anomaly_id": anomaly_id, "deployment_id": deployment_id,
             "incident_id": incident_id},
            resource_kind="incident_rca",
        )
        if replayed:
            return dict(receipt.get("response") or {}, replayed=True), False

        anomaly = self.store.get_anomaly(scope.organization_id, scope.repository_id, anomaly_id)
        if anomaly is None:
            raise NotFoundError("unknown anomaly")

        incident = self.store.create_incident(
            scope.organization_id, scope.repository_id, environment,
            deployment_id=deployment_id or anomaly.get("deployment_id"),
            anomaly_id=anomaly_id, incident_id=incident_id,
        )
        # RCA is queued durably through the outbox; the worker performs the
        # deterministic analysis. Acceptance never implies a completed RCA.
        self.store.connection.execute(
            "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, "
            "deployment_id, event_type, payload) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, environment, deployment_id, event_type) "
            "DO NOTHING",
            (str(uuid.uuid4()), scope.organization_id, scope.repository_id, environment,
             incident["deployment_id"] or "", "incident.rca_requested",
             self.store._Jsonb({"incident_id": incident["incident_id"], "anomaly_id": anomaly_id})),
        )
        response = {
            "incident_id": incident["incident_id"],
            "status": incident["status"],
            "anomaly_id": anomaly_id,
            "rca_state": "queued",
            "environment": environment,
        }
        self._finalize(scope, idempotency_key, response, resource_id=incident["incident_id"])
        return response, True

    # -- reads -----------------------------------------------------------------

    def incident_detail(self, scope, incident_id):
        incident = self.store.get_incident_scoped(
            scope.organization_id, scope.repository_id, incident_id
        )
        if incident is None:
            raise NotFoundError("unknown incident")
        reports = self.store.rca_for_incident(scope.organization_id, scope.repository_id, incident_id)
        completed = next((r for r in reports if r["status"] == "completed"), None)
        rca = completed or (reports[-1] if reports else None)

        anomaly = None
        if incident.get("anomaly_id"):
            anomaly = self.store.get_anomaly(
                scope.organization_id, scope.repository_id, incident["anomaly_id"]
            )

        detail = {
            "incident_id": incident["incident_id"],
            "status": incident["status"],
            "environment": incident["environment"],
            "deployment_id": incident.get("deployment_id"),
            "anomaly_id": incident.get("anomaly_id"),
            "created_at": isoformat(incident.get("created_at")),
            "updated_at": isoformat(incident.get("updated_at")),
            "anomaly_evidence": (anomaly or {}).get("payload"),
            "affected_models": (anomaly or {}).get("affected_models", []),
            "affected_kpis": (anomaly or {}).get("affected_kpis", []),
            "rca": self._rca_view(rca),
        }
        return detail

    def _rca_view(self, rca):
        if rca is None:
            return {"state": "pending"}
        return {
            "state": rca["status"],
            "rca_id": rca["rca_id"],
            "attributed_deployment_id": rca.get("attributed_deployment_id"),
            "deployment_candidates": rca.get("deployment_candidates", []),
            "affected_model": rca.get("affected_model"),
            "downstream_models": rca.get("downstream_models", []),
            "affected_kpis": rca.get("affected_kpis", []),
            "primary_cause": rca.get("primary_cause"),
            "alternative_causes": rca.get("alternative_causes", []),
            "contributing_factors": rca.get("contributing_factors", []),
            "downstream_symptoms": rca.get("downstream_symptoms", []),
            "unrelated_concurrent_changes": rca.get("unrelated_concurrent_changes", []),
            "confidence": rca.get("confidence"),
            "unevaluated_evidence": rca.get("unevaluated_evidence", []),
            "remediation": rca.get("remediation", []),
            "rollback_recommendation": rca.get("rollback_recommendation"),
            "verification_steps": rca.get("verification_steps", []),
            "lineage_level": rca.get("lineage_level"),
            "lineage_completeness": rca.get("lineage_completeness"),
            "evidence_coverage": rca.get("evidence_coverage"),
            "created_at": isoformat(rca.get("created_at")),
        }

    def rca_detail(self, scope, incident_id):
        incident = self.store.get_incident_scoped(
            scope.organization_id, scope.repository_id, incident_id
        )
        if incident is None:
            raise NotFoundError("unknown incident")
        reports = self.store.rca_for_incident(scope.organization_id, scope.repository_id, incident_id)
        completed = next((r for r in reports if r["status"] == "completed"), None)
        rca = completed or (reports[-1] if reports else None)
        return {"incident_id": incident_id, **self._rca_view(rca)}
