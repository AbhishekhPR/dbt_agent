"""The collection run: request -> validate -> query -> snapshot -> submit.

One request, one run, one truthful outcome. There is no retry loop, no
scheduler and no daemon: exit status is the report, and whatever runs the
collector decides what to do about it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.collector.client import ReliumApiError, ReliumClient
from agent.collector.config import COLLECTOR_VERSION
from agent.collector.signals import (
    UnknownSignalError,
    UnsafeIdentifierError,
    classify_signals,
)
from agent.collector.warehouse import (
    PostgresMetadataReader,
    RelationMissing,
    RelationNotReadable,
    WarehouseUnavailable,
)

logger = logging.getLogger("relium.collector")

# Only these are collected. A head-derived relation is produced by a model in
# the pull request itself, so it is not expected to exist in production yet and
# must never be queried for - asking would manufacture a false finding.
COLLECTABLE_KINDS = frozenset({"external", "internal"})


class CollectionError(RuntimeError):
    """The run failed. The message is safe to log and to report upstream."""


@dataclass
class CollectionOutcome:
    ok: bool
    reason: str = ""
    request_id: str | None = None
    review_id: str | None = None
    attempt: int | None = None
    snapshot_id: str | None = None
    status_code: int | None = None
    relations_collected: int = 0
    columns_collected: int = 0
    signals_collected: list = field(default_factory=list)
    signals_unsupported: list = field(default_factory=list)
    relations_missing: list = field(default_factory=list)
    completeness: str = "COMPLETE"

    def as_dict(self):
        return {
            "ok": self.ok, "reason": self.reason,
            "request_id": self.request_id, "review_id": self.review_id,
            "attempt": self.attempt, "snapshot_id": self.snapshot_id,
            "status_code": self.status_code,
            "relations_collected": self.relations_collected,
            "columns_collected": self.columns_collected,
            "signals_collected": list(self.signals_collected),
            "signals_unsupported": list(self.signals_unsupported),
            "relations_missing": list(self.relations_missing),
            "completeness": self.completeness,
        }


def _parse_expiry(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def validate_request(request, *, now=None):
    """Reject a request the collector must not act on.

    Malformed and expired requests are refused before the warehouse is touched,
    so a bad plan never costs a query.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(request, dict):
        raise CollectionError("collection request is not an object")

    for required in ("request_id", "review_id", "environment"):
        if not request.get(required):
            raise CollectionError(f"collection request is missing {required}")

    targets = request.get("targets")
    if not isinstance(targets, list) or not targets:
        raise CollectionError("collection request contains no targets")

    # Relium never sends SQL. If something that looks like SQL appears in a
    # request, treat the control plane as untrusted and stop.
    for forbidden in ("sql", "query", "statement", "command"):
        if forbidden in request:
            raise CollectionError(
                f"collection request contains a {forbidden!r} field; this "
                f"collector never executes remotely supplied SQL")
        for target in targets:
            if isinstance(target, dict) and forbidden in target:
                raise CollectionError(
                    f"collection request target contains a {forbidden!r} field; "
                    f"this collector never executes remotely supplied SQL")

    expires_at = _parse_expiry(request.get("expires_at"))
    if expires_at is None:
        raise CollectionError("collection request has no usable expires_at")
    if expires_at <= now:
        raise CollectionError(
            f"collection request {request['request_id']} expired at "
            f"{expires_at.isoformat()}")

    for index, target in enumerate(targets):
        if not isinstance(target, dict) or not target.get("relation_name"):
            raise CollectionError(f"target[{index}] is missing relation_name")
        # Fails closed on an unrecognised signal name.
        classify_signals(target.get("required_signals"))

    return request


def idempotency_key_for(request, snapshot):
    """Identify the measurement, not just the request.

    The first version of this keyed only on (request_id, attempt), which
    wedged the request: the API's payload hash includes observed_at, so a
    collector that retried after a failed submit would re-measure, produce a
    new observed_at under the same key, and be rejected as a conflicting
    replay - permanently, with no way to satisfy the request.

    Keying on the payload gives all three behaviours the API already defines:

      * the identical payload resubmitted  -> same key, same hash -> 200
      * a genuinely new measurement        -> new key             -> 202
      * the same key with different content-> conflict            -> 409

    A re-measurement really is different evidence, so treating it as a new
    snapshot rather than a conflict is the honest reading.
    """
    attempt = request.get("attempt") or 1
    material = json.dumps(
        {"review_id": snapshot.get("review_id"),
         "environment": snapshot.get("environment"),
         "relations": snapshot.get("relations"),
         "completeness": snapshot.get("completeness"),
         "observed_at": snapshot.get("observed_at")},
        sort_keys=True, separators=(",", ":"), default=str).encode()
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"relium-collector-{request['request_id']}-{attempt}-{digest}"


def collect_snapshot(request, reader, *, config, now=None):
    """Query the warehouse for exactly what the request names."""
    now = now or datetime.now(timezone.utc)
    relations = []
    missing = []
    collected_signals = set()
    unsupported_signals = set()
    column_count = 0

    for target in request["targets"]:
        kind = target.get("dependency_kind") or "external"
        if kind not in COLLECTABLE_KINDS:
            # head_derived: expected to be absent from production.
            continue

        supported, unimplemented = classify_signals(target.get("required_signals"))
        collected_signals.update(supported)
        unsupported_signals.update(unimplemented)

        relation_name = target["relation_name"]
        columns = list(target.get("columns") or [])
        try:
            relation = reader.collect_relation(
                relation_name=relation_name, columns=columns, signals=supported)
        except RelationMissing:
            missing.append(relation_name)
            relations.append(reader.missing_relation(relation_name))
            continue
        except RelationNotReadable as exc:
            # Submitting here would report a readable table as absent and turn
            # a missing GRANT into a production BLOCK.
            raise CollectionError(str(exc)) from None
        except UnsafeIdentifierError as exc:
            raise CollectionError(f"refusing to query an unsafe name: {exc}") from None
        except WarehouseUnavailable as exc:
            raise CollectionError(str(exc)) from None

        if unimplemented:
            relation["unevaluated_checks"] = sorted(unimplemented)
        relation["model_unique_id"] = target.get("model_unique_id")
        relation["observed_at"] = now.isoformat()
        relations.append(relation)
        column_count += len(relation.get("columns") or [])

    if not relations:
        raise CollectionError(
            "no collectable targets in this request; nothing was queried")

    # PARTIAL is honest when a signal was requested that this version cannot
    # compute. The decision engine already refuses to treat partial evidence
    # as a pass.
    completeness = "PARTIAL" if unsupported_signals else "COMPLETE"

    snapshot = {
        "review_id": request["review_id"],
        "request_id": request["request_id"],
        "environment": request["environment"],
        "attempt": request.get("attempt"),
        "completeness": completeness,
        "observed_at": now.isoformat(),
        "collected_at": now.isoformat(),
        "ttl_seconds": 3600,
        # Identity is carried straight through from the request, so the
        # snapshot binds to the exact code state the review was computed from.
        "base_sha": request.get("base_sha"),
        "head_sha": request.get("head_sha"),
        "base_manifest_hash": request.get("base_manifest_hash"),
        "head_manifest_hash": request.get("head_manifest_hash"),
        "collector_id": config.collector_id,
        "collector_version": COLLECTOR_VERSION,
        "adapter_type": config.adapter_type,
        "relations": relations,
    }
    return snapshot, {
        "signals_collected": sorted(collected_signals),
        "signals_unsupported": sorted(unsupported_signals),
        "relations_missing": missing,
        "columns_collected": column_count,
        "completeness": completeness,
    }


def run_collection(config, *, client=None, reader=None, request_id=None, now=None):
    """Run one collection and report truthfully.

    Returns a CollectionOutcome. Never raises for an ordinary failure: the
    outcome carries ok=False and a reason, and the caller maps that to an
    exit status.
    """
    client = client or ReliumClient(config)
    reader = reader or PostgresMetadataReader(
        config.warehouse_dsn, statement_timeout_ms=config.statement_timeout_ms)
    outcome = CollectionOutcome(ok=False)

    try:
        if request_id:
            request = client.get_request(request_id)
            if request is None:
                raise CollectionError(f"collection request {request_id} not found")
        else:
            pending = client.pending_requests(limit=1)
            if not pending:
                return CollectionOutcome(
                    ok=True, reason="no pending collection request")
            request = pending[0]

        outcome.request_id = request.get("request_id")
        outcome.review_id = request.get("review_id")
        outcome.attempt = request.get("attempt")

        validate_request(request, now=now)
        logger.info("collection_request_accepted request_id=%s review_id=%s targets=%d",
                    outcome.request_id, outcome.review_id, len(request["targets"]))

        # The API rejects a snapshot from an unregistered collector, so the
        # identity must exist before the warehouse is queried - failing after
        # the work is done would waste the collection.
        client.register()

        try:
            client.acknowledge(request["request_id"])
        except ReliumApiError as exc:
            # Acknowledgement is bookkeeping; losing it must not lose the run.
            logger.warning("acknowledge_failed request_id=%s status=%s",
                           outcome.request_id, exc.status)

        snapshot, summary = collect_snapshot(request, reader, config=config, now=now)
        outcome.relations_collected = len(snapshot["relations"])
        outcome.columns_collected = summary["columns_collected"]
        outcome.signals_collected = summary["signals_collected"]
        outcome.signals_unsupported = summary["signals_unsupported"]
        outcome.relations_missing = summary["relations_missing"]
        outcome.completeness = summary["completeness"]

        status, payload = client.submit_snapshot(
            snapshot, idempotency_key_for(request, snapshot))
        outcome.status_code = status
        outcome.snapshot_id = payload.get("snapshot_id")

        if status == 409:
            detail = payload.get("reason") or (
                "idempotency key reused with different evidence")
            outcome.reason = f"snapshot rejected as a conflicting replay: {detail}"
            return outcome

        outcome.ok = True
        outcome.reason = ("snapshot accepted" if status == 202
                          else "snapshot already recorded (idempotent replay)")
        logger.info("snapshot_submitted review_id=%s status=%s relations=%d",
                    outcome.review_id, status, outcome.relations_collected)
        return outcome

    except (CollectionError, UnknownSignalError, UnsafeIdentifierError) as exc:
        outcome.reason = str(exc)
        _report_failure(client, outcome)
        return outcome
    except ReliumApiError as exc:
        outcome.reason = str(exc)
        outcome.status_code = exc.status
        return outcome


def _report_failure(client, outcome):
    if not outcome.request_id:
        return
    try:
        client.report_failure(outcome.request_id, outcome.reason)
    except ReliumApiError:
        # The run already failed; the reason is in the outcome either way.
        logger.warning("failure_report_failed request_id=%s", outcome.request_id)
