"""Durable lifecycle worker.

Claims outbox jobs through the PostgreSQL locking semantics already implemented
by the store, dispatches them to typed handlers, and completes them
idempotently. Attempts, retry timing, leases and dead letters live in the
database, so a restart resumes rather than loses work.

Runs as its own process (``python -m agent.worker.lifecycle_worker``), not as a
hidden thread inside the API.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from agent.worker.rca_runtime import run_rca

logger = logging.getLogger("relium.worker")

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 5.0


class UnsupportedEvent(Exception):
    """The event type has no handler. Retained and dead-lettered, never dropped."""


class HandlerRegistry:
    """Explicit typed dispatch. An unknown type is an error, not a silent skip."""

    def __init__(self):
        self._handlers = {}

    def register(self, event_type):
        def decorator(fn):
            self._handlers[event_type] = fn
            return fn
        return decorator

    def supported(self):
        return sorted(self._handlers)

    def dispatch(self, event_type, context):
        handler = self._handlers.get(event_type)
        if handler is None:
            raise UnsupportedEvent(event_type)
        return handler(context)


registry = HandlerRegistry()


class JobContext:
    def __init__(self, store, event):
        self.store = store
        self.event = event
        self.organization_id = event["organization_id"]
        self.repository_id = event["repository_id"]
        self.environment = event["environment"]
        self.payload = event.get("payload") or {}
        # A job's subject is either a deployment or a review. Handlers read the
        # subject rather than assuming a deployment.
        self.subject_type = event.get("subject_type") or "deployment"
        self.subject_id = event.get("subject_id") or event.get("deployment_id")


@registry.register("incident.rca_requested")
def handle_rca_requested(ctx: JobContext):
    incident_id = ctx.payload.get("incident_id")
    if not incident_id:
        raise ValueError("incident.rca_requested payload is missing incident_id")
    record, created = run_rca(ctx.store, ctx.organization_id, ctx.repository_id,
                              ctx.environment, incident_id)
    return {"incident_id": incident_id, "rca_id": record["rca_id"],
            "status": record["status"], "created": created}


def _register_lifecycle_ack(event_type):
    """Deployment lifecycle events are durable publication points.

    They carry no external side effect yet, so the handler records the audit
    trail and completes. Registering them explicitly keeps them out of the
    dead-letter path while making the supported set inspectable.
    """
    @registry.register(event_type)
    def _handler(ctx: JobContext, _event_type=event_type):
        ctx.store.append_audit(
            ctx.organization_id, ctx.repository_id, actor="worker:lifecycle",
            event_type=_event_type, reference_type="deployment",
            reference_id=ctx.event.get("deployment_id"),
            payload={"outbox_event_id": ctx.event["event_id"]},
        )
        return {"acknowledged": _event_type}


for _event in ("deployment.reviewed", "deployment.approved", "deployment.deployment_started",
               "deployment.deployment_succeeded", "deployment.deployment_failed",
               "deployment.post_deployment_monitoring", "deployment.healthy",
               "deployment.post_deployment_anomaly", "deployment.rolled_back",
               "deployment.incident_open", "deployment.incident_resolved"):
    _register_lifecycle_ack(_event)


# Review recomputation runs on this same worker and this same outbox. It is
# registered here so the supported event set stays inspectable in one place.
from agent.metadata_evidence.recompute import register as _register_recompute  # noqa: E402
from agent.metadata_evidence.publication_reconcile import (  # noqa: E402
    register as _register_publication,
)
from agent.metadata_evidence.change_request import (  # noqa: E402
    register as _register_change_request,
)

_register_recompute(registry)

# Republication of a recomputed decision. The factory is installed by
# configure_publisher(); until then the handler runs and records that the
# decision was not published, rather than reporting a delivery that never
# happened.
_publisher_factory = {"build": None}


def configure_publisher(factory):
    """Install the per-tenant publisher factory used by republication."""
    _publisher_factory["build"] = factory


def _build_publisher(**scope):
    build = _publisher_factory["build"]
    return build(**scope) if build is not None else None


_register_publication(registry, publisher_factory=_build_publisher)
_register_change_request(registry, publisher_factory=_build_publisher)


class WorkerState:
    """Inspectable runtime state. Contains no credentials."""

    def __init__(self, identity):
        self.identity = identity
        self.started_at = datetime.now(timezone.utc)
        self.last_heartbeat = self.started_at
        self.last_successful_claim = None
        self.claimed_now = 0
        self.processed = 0
        self.retried = 0
        self.dead_lettered = 0
        self.unsupported = 0
        self._lock = threading.Lock()

    def snapshot(self, store=None, tenants=()):
        with self._lock:
            state = {
                "worker_identity": self.identity,
                "started_at": self.started_at.isoformat(),
                "last_heartbeat": self.last_heartbeat.isoformat(),
                "last_successful_claim": (self.last_successful_claim.isoformat()
                                          if self.last_successful_claim else None),
                "claimed_job_count": self.claimed_now,
                "processed": self.processed,
                "retry_count": self.retried,
                "dead_letter_count": self.dead_lettered,
                "unsupported_events": self.unsupported,
                "supported_event_types": registry.supported(),
            }
        if store is not None:
            state["queue_depth"] = store.outbox_stats()
            oldest = store.connection.execute(
                "SELECT MIN(next_attempt_at) AS oldest FROM outbox_events WHERE state='PENDING'"
            ).fetchone()["oldest"]
            state["oldest_ready_job_at"] = oldest.isoformat() if oldest else None
        return state


class LifecycleWorker:
    def __init__(self, store_factory, *, identity=None, poll_seconds=DEFAULT_POLL_SECONDS,
                 max_attempts=DEFAULT_MAX_ATTEMPTS, backoff_seconds=DEFAULT_BACKOFF_SECONDS):
        self.store_factory = store_factory
        self.identity = identity or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.state = WorkerState(self.identity)
        self._stop = threading.Event()

    def request_stop(self):
        self._stop.set()

    def _tenants(self, store):
        """Tenants with work the worker should look at.

        Includes tenants whose only rows are CLAIMED with an expired lease: a
        crashed worker leaves those behind, and if they did not appear here the
        lease would never be recovered because recovery runs inside the claim.
        """
        rows = store.connection.execute(
            "SELECT DISTINCT organization_id, repository_id, environment FROM outbox_events "
            "WHERE (state='PENDING' AND next_attempt_at <= now()) "
            "   OR (state='CLAIMED' AND lease_expires_at < now())"
        ).fetchall()
        return [(r["organization_id"], r["repository_id"], r["environment"]) for r in rows]

    def process_once(self, store) -> int:
        """Claim and process at most one job per eligible tenant. Returns the count."""
        processed = 0
        for organization_id, repository_id, environment in self._tenants(store):
            event = store.claim_outbox(organization_id, repository_id, environment, self.identity)
            if event is None:
                continue
            self.state.last_successful_claim = datetime.now(timezone.utc)
            self.state.claimed_now += 1
            try:
                result = registry.dispatch(event["event_type"], JobContext(store, event))
                store.complete_outbox(organization_id, repository_id, event["event_id"])
                self.state.processed += 1
                processed += 1
                logger.info("worker_job_complete", extra={
                    "operation": event["event_type"],
                    "processing_outcome": "completed",
                    "publication_id": event["event_id"],
                })
            except UnsupportedEvent:
                # Retained with inspectable evidence, retried per policy, then
                # dead-lettered. Never silently discarded.
                self.state.unsupported += 1
                store.fail_outbox(organization_id, repository_id, event["event_id"],
                                  error="unsupported event type",
                                  max_attempts=0, retry_backoff_seconds=self.backoff_seconds)
                self.state.dead_lettered += 1
                logger.info("worker_job_unsupported", extra={
                    "operation": event["event_type"],
                    "processing_outcome": "dead_letter",
                    "publication_id": event["event_id"],
                })
            except Exception as exc:
                attempts = event.get("attempts", 0)
                store.fail_outbox(organization_id, repository_id, event["event_id"],
                                  error=type(exc).__name__,
                                  max_attempts=self.max_attempts,
                                  retry_backoff_seconds=self.backoff_seconds)
                if attempts >= self.max_attempts:
                    self.state.dead_lettered += 1
                else:
                    self.state.retried += 1
                logger.error("worker_job_failed", extra={
                    "operation": event["event_type"],
                    "error_category": "handler",
                    "processing_outcome": "retry",
                    "publication_id": event["event_id"],
                })
            finally:
                self.state.claimed_now -= 1
        return processed

    def run(self, *, max_iterations=None):
        store = self.store_factory()
        iterations = 0
        try:
            logger.info("worker_started", extra={"operation": "startup"})
            while not self._stop.is_set():
                self.state.last_heartbeat = datetime.now(timezone.utc)
                try:
                    did = self.process_once(store)
                except Exception:
                    logger.error("worker_iteration_failed",
                                 extra={"error_category": "worker"})
                    did = 0
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                if did == 0:
                    self._stop.wait(self.poll_seconds)
        finally:
            # Graceful shutdown: in-flight claims keep their lease and are
            # reclaimed after expiry rather than being lost.
            logger.info("worker_stopped", extra={"operation": "shutdown"})
            store.close()


def build_worker(dsn, **kwargs):
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    return LifecycleWorker(lambda: PostgresLifecycleStore(dsn), **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Relium durable lifecycle worker")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--backoff-seconds", type=float, default=DEFAULT_BACKOFF_SECONDS)
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="stop after N polls; used by tests and one-shot runs")
    parser.add_argument("--print-state", action="store_true",
                        help="print an inspectable state snapshot and exit")
    args = parser.parse_args(argv)

    from agent.github_app.server import configure_logging

    configure_logging()

    dsn = os.environ.get("RELIUM_DATABASE_URL", "").strip()
    if not dsn:
        print("RELIUM_DATABASE_URL is required", file=sys.stderr)
        return 2
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("RELIUM_DATABASE_URL must be a PostgreSQL DSN", file=sys.stderr)
        return 2

    worker = build_worker(dsn, poll_seconds=args.poll_seconds,
                          max_attempts=args.max_attempts,
                          backoff_seconds=args.backoff_seconds)

    if args.print_state:
        store = worker.store_factory()
        try:
            print(json.dumps(worker.state.snapshot(store), indent=2, sort_keys=True))
        finally:
            store.close()
        return 0

    def _handle_signal(signum, _frame):
        logger.info("worker_signal", extra={"operation": f"signal-{signum}"})
        worker.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, AttributeError):  # pragma: no cover - platform dependent
            pass

    worker.run(max_iterations=args.max_iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
