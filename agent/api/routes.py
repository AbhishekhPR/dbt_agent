"""HTTP handlers for the public lifecycle and dashboard API.

Every /api route authenticates a service token and resolves its tenant scope
from that token. Values supplied in a request body or path are never trusted as
authorization input. Store access happens in a worker thread so the synchronous
psycopg driver never blocks the event loop.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.api.collector_routes import COLLECTOR_ROUTES, build_handlers
from agent.api.auth import AuthenticationError, AuthorizationError, ServiceTokenAuthenticator, bearer_token
from agent.api.authorization import (
    CI_MANIFEST_INGEST, COLLECTION_REQUEST_READ, COLLECTOR_INGEST, DASHBOARD_READ,
    GOVERNANCE_WRITE, PIPELINE_INGEST, CapabilityError, authorize,
)
from agent.api.sessions import CSRF_HEADER, SESSION_COOKIE, SessionError
from agent.metadata_evidence.evidence_export import (
    build_evidence_bundle,
    evidence_filename,
)
from agent.metadata_evidence.impact_report import (
    impact_report_filename,
    render_review_impact_report,
)
from agent.api.service import (
    ConflictError,
    LifecycleService,
    NotFoundError,
    scoped_integrity_error,
)
from agent.api.validation import (
    ValidationError,
    isoformat,
    optional_choice,
    optional_list,
    optional_object,
    optional_str,
    optional_timestamp,
    pagination,
    require_choice,
    require_idempotency_key,
    require_mapping,
    require_object,
    require_str,
    require_timestamp,
)
from agent.lifecycle_models import ALLOWED_TRANSITIONS


def _utcnow():
    return datetime.now(timezone.utc)

logger = logging.getLogger(__name__)

MAX_API_BODY_BYTES = 512 * 1024

DEPLOYMENT_EVENT_TYPES = {"created"} | set(ALLOWED_TRANSITIONS) | {
    target for targets in ALLOWED_TRANSITIONS.values() for target in targets
}
SEVERITIES = {"low", "medium", "high", "critical"}
COVERAGE_STATES = {"COMPLETE", "INCOMPLETE", "UNKNOWN"}
SUPPORTED_METRICS = {
    "row_count", "null_rate", "duplicate_rate", "freshness",
    "cardinality", "schema", "kpi_value",
}


def _request_id(request) -> str:
    supplied = request.headers.get("X-Request-Id")
    if isinstance(supplied, str) and supplied.strip() and len(supplied) <= 128:
        return supplied.strip()
    return uuid.uuid4().hex


def _json(payload: dict, status: int, request_id: str) -> JSONResponse:
    return JSONResponse(
        {**payload, "request_id": request_id},
        status_code=status,
        headers={"X-Request-Id": request_id},
    )


async def _read_json(request, request_id):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_API_BODY_BYTES:
                raise _HttpError(413, {"status": "payload_too_large"})
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_API_BODY_BYTES:
            raise _HttpError(413, {"status": "payload_too_large"})
        body.extend(chunk)
    if not body:
        raise _HttpError(400, {"status": "invalid_request", "detail": "empty body"})
    try:
        return require_mapping(json.loads(bytes(body).decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _HttpError(400, {"status": "invalid_request", "detail": "malformed JSON"}) from None


class _HttpError(Exception):
    def __init__(self, status: int, payload: dict):
        super().__init__(payload.get("detail", payload.get("status", "error")))
        self.status = status
        self.payload = payload


# What each collector-surface route is for, decided per route rather than by
# HTTP verb. Only `get_collection_request` is reachable by both a person and a
# machine, because the collector reads the request it is about to satisfy and
# the dashboard shows the operator what was asked for.
_COLLECTOR_CAPABILITY = {
    "list_collection_requests": COLLECTOR_INGEST,
    "get_collection_request": COLLECTION_REQUEST_READ,
    "acknowledge_collection_request": COLLECTOR_INGEST,
    "report_collection_failure": COLLECTOR_INGEST,
    "submit_manifest_evidence": CI_MANIFEST_INGEST,
    "submit_snapshot": COLLECTOR_INGEST,
    "register_collector": COLLECTOR_INGEST,
    # Read by the dashboard only; the collector never asks for these.
    "get_snapshot_status": DASHBOARD_READ,
    "get_review_evidence_coverage": DASHBOARD_READ,
}


# Fields each semantic change kind may disclose. An allowlist rather than a
# denylist: a new engine field is invisible until someone decides it should be
# public, which is the safe direction for a projection that carries customer
# SQL.
_SEMANTIC_CHANGE_FIELDS = {
    "projection_added": ("output_name", "after_sql"),
    "projection_removed": ("output_name", "before_sql"),
    "projection_expression_changed": ("output_name", "before_sql", "after_sql"),
    "join_added": ("relation", "after_join_type", "after_condition_sql"),
    "join_removed": ("relation", "before_join_type", "before_condition_sql"),
    "join_type_changed": ("relation", "before_join_type", "after_join_type"),
    "join_condition_changed": ("relation", "before_sql", "after_sql"),
    "filter_changed": ("scope", "before_sql", "after_sql"),
    "grouping_changed": ("before_sql", "after_sql"),
}

_SEMANTIC_STATUSES = {"evaluated", "partial", "unavailable"}


def _semantic_evidence_view(stored):
    """Project stored SQL semantic evidence for the dashboard.

    ``None`` is preserved as ``None``: the comparison never ran, which is a
    different fact from running and finding nothing, and the frontend has to
    be able to tell them apart.

    Model identity travels per change rather than per model so a card can name
    what it describes; base/head SHAs and the attempt are not repeated here
    because the review and attempt already carry them.
    """
    if not isinstance(stored, dict):
        return None
    status = stored.get("status")
    if status not in _SEMANTIC_STATUSES:
        return None

    changes = []
    unreadable = []
    for model in stored.get("models") or []:
        if not isinstance(model, dict):
            continue
        name = model.get("model_name")
        if model.get("status") != "evaluated":
            unreadable.append({"model_name": name,
                               "reason": model.get("unavailable_reason")})
            continue
        for change in model.get("changes") or []:
            kind = change.get("kind")
            allowed = _SEMANTIC_CHANGE_FIELDS.get(kind)
            if not allowed:
                continue
            view = {"kind": kind, "model_name": name}
            if change.get("model_unique_id"):
                view["model_unique_id"] = change["model_unique_id"]
            for field_name in allowed:
                view[field_name] = change.get(field_name)
            changes.append(view)

    return {
        "status": status,
        "changes": changes,
        "change_count": len(changes),
        # Named so a partial comparison can say which models it could not read
        # instead of implying they were clean.
        "unavailable_models": unreadable,
    }


# Fields each production-metadata change kind may disclose. An allowlist for
# the same reason the semantic one is: a field the engine gains later stays
# invisible until someone decides it is safe to publish. Nothing here can
# express a raw value, a query, or a collector detail - the widest field is a
# data type name.
_METADATA_CHANGE_FIELDS = {
    "relation_availability_changed": ("before", "after"),
    "schema_fingerprint_changed": ("before", "after"),
    "row_count_changed": ("before", "after", "absolute_delta", "relative_delta"),
    "freshness_changed": ("before", "after", "absolute_delta"),
    "column_availability_changed": ("before", "after"),
    "column_type_changed": ("before", "after"),
    "column_nullability_changed": ("before", "after"),
    "null_rate_changed": ("before", "after", "percentage_point_delta"),
    "duplicate_rate_changed": ("before", "after", "percentage_point_delta"),
    "distinct_count_changed": ("before", "after", "absolute_delta", "relative_delta"),
    # A ratio, so it carries a percentage-point delta like the other rates.
    "cardinality_changed": ("before", "after", "percentage_point_delta"),
}

# All four survive to the client as themselves. Collapsing any pair would erase
# a distinction the dashboard has to render differently.
_METADATA_STATUSES = {"evaluated", "partial", "no_baseline", "unavailable"}


def _metadata_coverage_view(stored):
    """Counts and identities only, so ``partial`` can say what it did not cover."""
    if not isinstance(stored, dict):
        return None

    def _scope(rows, *, with_column):
        items = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = {"model": row.get("model"), "relation": row.get("relation")}
            if with_column:
                item["column"] = row.get("column")
            items.append(item)
        return items

    return {
        "relations_observed": stored.get("relations_observed"),
        "relations_compared": stored.get("relations_compared"),
        "relations_without_baseline": _scope(
            stored.get("relations_without_baseline"), with_column=False),
        "columns_observed": stored.get("columns_observed"),
        "columns_compared": stored.get("columns_compared"),
        "columns_without_baseline": _scope(
            stored.get("columns_without_baseline"), with_column=True),
    }


def _metadata_comparison_view(stored):
    """Project stored production metadata comparison evidence for the dashboard.

    ``None`` is preserved as ``None``: the comparison never ran for this
    attempt, which is a different fact from running and finding no prior
    observation (``no_baseline``), running and finding nothing changed
    (``evaluated`` with an empty list), or running with incomplete coverage
    (``partial``). All four have to reach the client intact because the UI has
    to say something different about each.

    Snapshot identities and observation timestamps travel, so a reviewer can
    see WHICH two observations this describes. Nothing else from the snapshot
    does: no collector identity, no provenance, no evidence hash, no min/max
    value, no SQL.
    """
    if not isinstance(stored, dict):
        return None
    status = stored.get("status")
    if status not in _METADATA_STATUSES:
        return None

    changes = []
    for change in stored.get("changes") or []:
        if not isinstance(change, dict):
            continue
        kind = change.get("kind")
        allowed = _METADATA_CHANGE_FIELDS.get(kind)
        if not allowed:
            continue
        view = {
            "kind": kind,
            "model": change.get("model"),
            "relation": change.get("relation"),
            "column": change.get("column"),
            "signal": change.get("signal"),
        }
        for field_name in allowed:
            if field_name in change:
                view[field_name] = change[field_name]
        changes.append(view)

    return {
        "status": status,
        "baseline_snapshot_id": stored.get("baseline_snapshot_id"),
        "current_snapshot_id": stored.get("current_snapshot_id"),
        "baseline_observed_at": stored.get("baseline_observed_at"),
        "current_observed_at": stored.get("current_observed_at"),
        "changes": changes,
        "change_count": len(changes),
        "coverage": _metadata_coverage_view(stored.get("coverage")),
    }


def _governance_actor(body, principal) -> str:
    """The identity recorded against a governance action.

    It comes from the authenticated session and nowhere else. This used to be
    ``optional_str(body, "actor") or "dashboard"``, so the audit trail recorded
    whatever the caller typed — which made every exception approval
    unattributable, and let one person's action be filed under another's name.

    A body that still supplies ``actor`` is rejected rather than ignored. A
    caller trying to set it is either running against the old contract or
    trying to forge attribution, and both deserve an error instead of a
    silently different outcome.
    """
    if isinstance(body, dict) and "actor" in body:
        raise ValidationError(
            "'actor' is not accepted: the actor is taken from the "
            "authenticated session", field="actor")
    actor = getattr(principal, "actor", None)
    if not actor:
        raise AuthorizationError("this credential has no human actor")
    return actor


def create_api_routes(*, store_pool, authenticator_factory=None,
                      session_manager=None, allowed_origins=()):
    """Build the /api route table. Registration stays explicit and inspectable.

    Every route declares the capability it needs. Authentication answers *who*
    is calling — a GitHub-authenticated person or a machine token — and
    ``authorize`` answers whether that principal may do this. The two were
    previously the same question, which is how a token compiled into the
    dashboard's JavaScript ended up able to approve governance exceptions.
    """
    allowed_origins = tuple(allowed_origins or ())

    def _authenticate(request, store, capability, *, mutating):
        """Resolve the caller into a principal, or refuse.

        A session cookie and a bearer token are different kinds of caller and
        are never merged: whichever is presented is the one evaluated, and the
        capability decides whether that kind is acceptable here.
        """
        session_id = request.cookies.get(SESSION_COOKIE) if session_manager else None
        if session_id:
            if mutating:
                _require_csrf(request, store, session_id)
            try:
                principal = session_manager.authenticate(
                    store, session_id,
                    # A governance write re-verifies with GitHub rather than
                    # trusting the permission recorded at sign-in.
                    require_fresh_permission=(capability is GOVERNANCE_WRITE))
            except SessionError as exc:
                raise AuthenticationError(str(exc)) from None
        else:
            authenticator = (authenticator_factory or ServiceTokenAuthenticator)(store)
            principal = authenticator.authenticate(
                bearer_token(request.headers.get("Authorization")))

        try:
            authorize(principal, capability)
        except CapabilityError as exc:
            raise _HttpError(403, {"status": "forbidden", "detail": str(exc)}) from None
        return principal

    def _require_csrf(request, store, session_id):
        """Cookie-authenticated mutations need an origin and a bound token.

        SameSite is not relied on alone: the dashboard and the API share a
        registrable domain, so a sibling subdomain would be same-site.
        """
        origin = request.headers.get("Origin")
        if allowed_origins:
            if not origin:
                raise _HttpError(403, {"status": "forbidden",
                                       "detail": "Origin header is required"})
            if origin not in allowed_origins:
                raise _HttpError(403, {"status": "forbidden",
                                       "detail": "origin is not allowed"})
        if not session_manager.verify_csrf(
                store, session_id, request.headers.get(CSRF_HEADER)):
            raise _HttpError(403, {"status": "forbidden",
                                   "detail": "missing or invalid CSRF token"})

    def handler(fn, *, write: bool, capability=None, download=False):
        """Wrap one route function with authentication, scoping and error mapping.

        ``download`` changes only how a SUCCESSFUL response is built: the
        handler returns ``(status, payload, filename)`` and the body is written
        as the exact artifact, with no ``request_id`` mixed into it, plus a
        Content-Disposition attachment header. Errors still travel the ordinary
        JSON path, so a failed download is a normal API error rather than a
        file containing an error.
        """
        capability = capability or (GOVERNANCE_WRITE if write else DASHBOARD_READ)

        async def wrapped(request):
            request_id = _request_id(request)
            body = None
            try:
                if write:
                    body = await _read_json(request, request_id)

                def work():
                    with store_pool.acquire() as store:
                        scope = _authenticate(request, store, capability,
                                              mutating=write)
                        service = LifecycleService(store)
                        return fn(request, body, scope, service)

                result = await run_in_threadpool(work)
                if download:
                    status, payload, filename = result
                    # A str payload is already the artifact and is written
                    # verbatim: re-encoding Markdown as JSON would change the
                    # bytes the caller asked for. Anything else is a JSON
                    # bundle, exactly as before.
                    if isinstance(payload, str):
                        body_text, media_type = payload, "text/markdown"
                    else:
                        body_text = json.dumps(payload, indent=2, sort_keys=True)
                        media_type = "application/json"
                    return Response(
                        body_text,
                        status_code=status,
                        media_type=media_type,
                        headers={
                            "X-Request-Id": request_id,
                            "Content-Disposition":
                                f'attachment; filename="{filename}"',
                        },
                    )
                status, payload = result
                return _json(payload, status, request_id)
            except _HttpError as exc:
                return _json(exc.payload, exc.status, request_id)
            except ValidationError as exc:
                return _json(exc.as_dict(), 422, request_id)
            except AuthenticationError:
                return _json({"status": "unauthorized"}, 401, request_id)
            except AuthorizationError:
                # Non-disclosing: an out-of-scope resource is indistinguishable
                # from one that does not exist.
                return _json({"status": "not_found"}, 404, request_id)
            except NotFoundError:
                return _json({"status": "not_found"}, 404, request_id)
            except ConflictError as exc:
                return _json({"status": "conflict", "detail": str(exc)}, 409, request_id)
            except Exception as exc:
                # A database integrity error is an expected outcome of tenant
                # scoping, not an internal fault. Translate it to the documented
                # non-disclosing response instead of leaking a 500.
                if type(exc).__name__ in ("UniqueViolation", "ForeignKeyViolation",
                                          "IntegrityError", "CheckViolation",
                                          "ExclusionViolation"):
                    translated = scoped_integrity_error(exc)
                    logger.info(
                        "api_scoped_integrity_conflict",
                        extra={"error_category": "scoped_conflict",
                               "route_template": request.url.path},
                    )
                    if isinstance(translated, NotFoundError):
                        return _json({"status": "not_found"}, 404, request_id)
                    return _json({"status": "conflict", "detail": str(translated)},
                                 409, request_id)
                logger.error(
                    "api_request_failed",
                    extra={"error_category": "internal", "route_template": request.url.path},
                )
                return _json({"status": "unavailable"}, 500, request_id)

        return wrapped

    # -- write handlers -------------------------------------------------------

    def post_deployment_event(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        deployment_id = require_str(body, "deployment_id")
        event_type = require_choice(body, "event_type", DEPLOYMENT_EVENT_TYPES)
        environment = optional_str(body, "environment")
        payload = optional_object(body, "deployment")
        response, created = service.submit_deployment_event(
            scope, environment=environment, deployment_id=deployment_id,
            event_type=event_type, idempotency_key=key, payload=payload,
        )
        return (202 if created else 200), {"status": "accepted", **response}

    def post_baseline(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        response, created = service.submit_baseline(
            scope,
            environment=optional_str(body, "environment"),
            model=require_str(body, "model"),
            baseline=require_object(body, "baseline"),
            observed_at=require_timestamp(body, "observed_at"),
            evidence_coverage=optional_choice(body, "evidence_coverage", COVERAGE_STATES) or "UNKNOWN",
            source=optional_str(body, "source"),
            idempotency_key=key,
        )
        return (201 if created else 200), {"status": "recorded", **response}

    def post_observation(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        response, created = service.submit_observation(
            scope,
            environment=optional_str(body, "environment"),
            deployment_id=optional_str(body, "deployment_id"),
            model=optional_str(body, "model"),
            metric=require_choice(body, "metric", SUPPORTED_METRICS),
            value_payload=require_object(body, "value"),
            observed_at=require_timestamp(body, "observed_at"),
            evidence_coverage=optional_choice(body, "evidence_coverage", COVERAGE_STATES) or "UNKNOWN",
            source=optional_str(body, "source"),
            idempotency_key=key,
        )
        return (201 if created else 200), {"status": "recorded", **response}

    def post_anomaly(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        response, created = service.submit_anomaly(
            scope,
            environment=optional_str(body, "environment"),
            deployment_id=optional_str(body, "deployment_id"),
            kind=require_str(body, "kind"),
            severity=require_choice(body, "severity", SEVERITIES),
            detected_at=optional_timestamp(body, "detected_at"),
            affected_models=optional_list(body, "affected_models"),
            affected_kpis=optional_list(body, "affected_kpis"),
            observation_ids=optional_list(body, "observation_ids"),
            evidence=require_object(body, "evidence"),
            idempotency_key=key,
        )
        return (201 if created else 200), {"status": "recorded", **response}

    def post_incident_rca(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        response, created = service.request_incident_rca(
            scope,
            environment=optional_str(body, "environment"),
            anomaly_id=require_str(body, "anomaly_id"),
            deployment_id=optional_str(body, "deployment_id"),
            incident_id=optional_str(body, "incident_id"),
            idempotency_key=key,
        )
        return (202 if created else 200), {"status": "accepted", **response}

    def post_review(request, body, scope, service):
        key = require_idempotency_key(body, request.headers)
        environment = scope.require_environment(optional_str(body, "environment"))
        service.store.ensure_tenant(scope.organization_id, scope.repository_id, environment)
        record = service.store.create_review(
            scope.organization_id, scope.repository_id, environment,
            review_id=key,
            decision=require_choice(body, "decision", {"ALLOW", "WARN", "BLOCK", "NEUTRAL"}),
            pull_number=body.get("pull_number") if isinstance(body.get("pull_number"), int) else None,
            commit_sha=optional_str(body, "commit_sha"),
            enforcement_mode=optional_choice(body, "enforcement_mode", {"shadow", "enforce"}),
            risk_score=body.get("risk_score") if isinstance(body.get("risk_score"), int) else None,
            evidence_coverage=optional_choice(body, "evidence_coverage", COVERAGE_STATES),
            payload=optional_object(body, "payload"),
        )
        return 201, {"status": "recorded", "review_id": record["review_id"],
                     "decision": record["decision"], "environment": environment}

    # -- read handlers ---------------------------------------------------------

    def _env_filter(request, scope):
        requested = request.query_params.get("environment")
        if scope.environment is not None:
            if requested is not None and requested != scope.environment:
                raise AuthorizationError("environment outside token scope")
            return scope.environment
        return requested

    def _page(request):
        return pagination(request.query_params)

    def list_reviews(request, body, scope, service):
        limit, offset = _page(request)
        page = service.store.list_reviews(
            scope.organization_id, scope.repository_id,
            environment=_env_filter(request, scope), limit=limit, offset=offset,
        )
        return 200, {
            "total": page["total"], "limit": limit, "offset": offset,
            "items": [_review_view(r) for r in page["items"]],
        }

    def get_review(request, body, scope, service):
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, request.path_params["review_id"]
        )
        if record is None:
            raise NotFoundError("unknown review")
        return 200, _review_view(record)

    def rerun_review(request, body, scope, service):
        """Re-run the analysis for one review.

        What a re-run can honestly mean here is fixed by the lifecycle, not
        chosen:

        * ``review_id`` is a digest of (repository, pull number, head SHA), so
          a re-run is bound to immutable analysis inputs by construction. A
          different HEAD is a DIFFERENT review, and this endpoint says so
          rather than pretending the same analysis was repeated.
        * ``recompute_review`` is idempotent on the snapshot that triggered it,
          so recomputing against the SAME evidence produces no new attempt.
          Re-running therefore means *collect fresh production evidence*: a
          new collection request, which a collector answers with a new
          snapshot, which produces a genuinely new attempt and a
          republication.

        Nothing here computes a decision. It asks for the evidence a decision
        needs, and the existing worker path does the rest.
        """
        from datetime import timedelta

        from agent.metadata_evidence.collection_plan import ttl_minutes_for
        from agent.metadata_evidence.review_lifecycle import (
            REQUEST_TTL_MINUTES, review_id_for,
        )

        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")

        # The caller may assert which HEAD it believes it is re-running. If the
        # pull request has moved on, the honest answer is that a different
        # review covers the new HEAD - not a silent re-run of stale inputs.
        claimed_head = optional_str(body, "head_sha")
        if claimed_head and claimed_head != record.get("head_sha"):
            raise ConflictError(
                "the pull request HEAD has changed since this review; "
                "a new review covers the new HEAD. Expected "
                f"{record.get('head_sha')}, and this review cannot be re-run "
                f"against {claimed_head}. The review for that HEAD is "
                f"{review_id_for(scope.repository_id, record.get('pull_number'), claimed_head)}.")

        if not record.get("metadata_required"):
            raise ConflictError(
                "this review introduces no external production dependency, so "
                "there is no production evidence to re-collect. Its decision "
                "is derived from code evidence that has not changed.")

        # Double-click safety, and the correct answer regardless: a request
        # that is still actionable IS the re-run already in flight.
        existing = service.store.collection_requests_for_review(
            scope.organization_id, scope.repository_id, review_id)
        actionable = [r for r in existing
                      if r["state"] in ("PENDING", "ACKNOWLEDGED")]
        if actionable:
            return 200, {
                "status": "already_running",
                "review_id": review_id,
                "rerun_id": actionable[0]["request_id"],
                # NOT "request_id": the envelope reserves that key for the
                # correlation id and would overwrite it.
                "collection_request_id": actionable[0]["request_id"],
                "request_state": actionable[0]["state"],
                "attempt": record.get("attempt"),
                "expires_at": isoformat(actionable[0]["expires_at"]),
            }

        plan = (record.get("payload") or {}).get("plan") or {}
        targets = [t for t in plan.get("targets", [])
                   if t.get("dependency_kind") == "external"]
        if not targets:
            raise ConflictError(
                "the collection plan for this review names no external "
                "relation, so there is nothing to collect.")

        criticality = "critical" if any(
            t.get("criticality") == "critical" for t in targets) else "standard"
        request_id = f"req-{review_id}-rerun-{len(existing) + 1}"
        service.store.create_collection_request(
            scope.organization_id, scope.repository_id, record["environment"],
            request_id=request_id, review_id=review_id, reason="rerun",
            expires_at=_utcnow() + timedelta(minutes=REQUEST_TTL_MINUTES),
            targets=targets,
            base_sha=record["base_sha"], head_sha=record["head_sha"],
            base_manifest_hash=record["base_manifest_hash"],
            head_manifest_hash=record["head_manifest_hash"],
            priority=criticality,
            required_evidence_level=plan.get("required_evidence_level", "profile"),
            plan={"attempt": record.get("attempt"),
                  "policy_version": record.get("policy_version"),
                  "policy_hash": record.get("policy_hash"),
                  "ttl_minutes": ttl_minutes_for(criticality),
                  "rerun": True},
        )
        service.store.transition_review(
            scope.organization_id, scope.repository_id, review_id,
            "METADATA_REQUESTED", reason=f"re-run requested: {request_id}")
        service.store.append_audit(
            scope.organization_id, scope.repository_id, actor="dashboard",
            event_type="review.rerun_requested", reference_type="review",
            reference_id=review_id,
            payload={"request_id": request_id,
                     "attempt_before": record.get("attempt"),
                     "head_sha": record.get("head_sha")})

        return 202, {
            "status": "accepted",
            "review_id": review_id,
            "rerun_id": request_id,
            "collection_request_id": request_id,
            "request_state": "PENDING",
            # The attempt the re-run starts FROM. The new attempt appears once
            # a collector answers this request and the worker recomputes.
            "attempt": record.get("attempt"),
            "head_sha": record.get("head_sha"),
        }

    def request_changes(request, body, scope, service):
        """Submit a GitHub request-changes review on the reviewer's behalf.

        Durable and asynchronous: the intent is committed, then the worker
        performs the GitHub call. A GitHub outage therefore retries instead of
        becoming a lost decision or a fabricated success - the record stays
        PENDING until GitHub returns a review id.

        Uses ``pull_requests: write``, already in the App's enforced permission
        set. No permission is broadened.
        """
        from agent.metadata_evidence.change_request import EVENT_TYPE

        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")
        if not record.get("pull_number"):
            raise ConflictError(
                "this review has no pull request number recorded, so there is "
                "no pull request to request changes on.")

        message = require_str(body, "message")
        if len(message.strip()) < 3:
            raise ValidationError(
                "'message' must say what needs to change", field="message")
        actor = _governance_actor(body, scope)
        attempt = int(record.get("attempt") or 1)

        change_request_id = f"cr-{review_id}-{attempt}"
        row, created = service.store.create_change_request(
            scope.organization_id, scope.repository_id, record["environment"],
            change_request_id=change_request_id, review_id=review_id,
            attempt=attempt, pull_number=record["pull_number"],
            head_sha=record.get("head_sha") or "", actor=actor,
            message=message.strip()[:4000])

        if not created:
            # A request for this attempt already exists. Returning it is the
            # correct answer to a double click: one GitHub review, not two.
            return 200, {
                "status": "already_requested",
                "review_id": review_id,
                "change_request_id": row["change_request_id"],
                "state": row["state"],
                "attempt": row["attempt"],
                "remote_review_id": row.get("remote_review_id"),
            }

        service.store.enqueue_review_recomputation(
            scope.organization_id, scope.repository_id, record["environment"],
            review_id=review_id, event_type=EVENT_TYPE,
            payload={"review_id": review_id,
                     "change_request_id": change_request_id},
            dedup_key=change_request_id)
        service.store.append_audit(
            scope.organization_id, scope.repository_id, actor=actor,
            event_type="review.change_request_requested", reference_type="review",
            reference_id=review_id,
            payload={"change_request_id": change_request_id, "attempt": attempt,
                     "pull_number": record["pull_number"]})

        return 202, {
            "status": "accepted",
            "review_id": review_id,
            "change_request_id": change_request_id,
            "state": "PENDING",
            "attempt": attempt,
        }

    def list_change_requests(request, body, scope, service):
        review_id = request.path_params["review_id"]
        if service.store.get_review(scope.organization_id, scope.repository_id,
                                    review_id) is None:
            raise NotFoundError("unknown review")
        rows = service.store.change_requests_for_review(
            scope.organization_id, scope.repository_id, review_id)
        return 200, {"review_id": review_id, "total": len(rows),
                     "items": [_change_request_view(r) for r in rows]}

    def approve_exception(request, body, scope, service):
        """Approve a human exception against one attempt's decision.

        The review's decision is NOT rewritten. A BLOCK stays BLOCK, and the
        exception is recorded beside it - an override is governance metadata,
        not evidence that the analysis was wrong. The dashboard renders the
        pair, e.g. "BLOCK - exception approved".

        Scope defaults to the exact attempt: a later attempt analysed newer
        evidence, and must not inherit an override approved against findings
        nobody has re-examined.
        """
        import uuid as _uuid

        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")

        reason = require_str(body, "reason")
        if len(reason.strip()) < 3:
            raise ValidationError(
                "'reason' is required: an override without a stated reason "
                "cannot be audited", field="reason")
        actor = _governance_actor(body, scope)
        scope_value = optional_choice(body, "scope", {"attempt", "review"}) or "attempt"
        attempt = body.get("attempt")
        attempt = int(attempt) if isinstance(attempt, int) else int(record.get("attempt") or 1)

        attempts = service.store.review_attempts(
            scope.organization_id, scope.repository_id, review_id)
        matching = next((a for a in attempts if a["attempt"] == attempt), None)
        if matching is None:
            raise ConflictError(
                f"attempt {attempt} is not recorded for this review, so there "
                f"is no decision to override.")

        row, created = service.store.create_review_exception(
            scope.organization_id, scope.repository_id, record["environment"],
            exception_id=f"exc-{_uuid.uuid4().hex[:20]}", review_id=review_id,
            attempt=attempt, actor=actor, reason=reason.strip()[:2000],
            scope=scope_value, overridden_decision=matching.get("decision"),
            base_sha=record.get("base_sha"), head_sha=record.get("head_sha"))

        if not created:
            return 200, {"status": "already_approved",
                         "review_id": review_id,
                         **_exception_view(row)}

        service.store.append_audit(
            scope.organization_id, scope.repository_id, actor=actor,
            event_type="review.exception_approved", reference_type="review",
            reference_id=review_id,
            payload={"exception_id": row["exception_id"], "attempt": attempt,
                     "scope": scope_value,
                     "overridden_decision": matching.get("decision"),
                     "reason": reason.strip()[:200]})
        return 201, {"status": "approved", "review_id": review_id,
                     **_exception_view(row)}

    def revoke_exception(request, body, scope, service):
        review_id = request.path_params["review_id"]
        exception_id = request.path_params["exception_id"]
        if service.store.get_review(scope.organization_id, scope.repository_id,
                                    review_id) is None:
            raise NotFoundError("unknown review")
        existing = service.store.get_review_exception(
            scope.organization_id, scope.repository_id, exception_id)
        if existing is None or existing["review_id"] != review_id:
            raise NotFoundError("unknown exception")

        reason = require_str(body, "reason")
        actor = _governance_actor(body, scope)
        if existing["state"] == "revoked":
            return 200, {"status": "already_revoked", "review_id": review_id,
                         **_exception_view(existing)}

        row = service.store.revoke_review_exception(
            scope.organization_id, scope.repository_id, exception_id,
            actor=actor, reason=reason.strip()[:2000])
        service.store.append_audit(
            scope.organization_id, scope.repository_id, actor=actor,
            event_type="review.exception_revoked", reference_type="review",
            reference_id=review_id,
            payload={"exception_id": exception_id,
                     "reason": reason.strip()[:200]})
        return 200, {"status": "revoked", "review_id": review_id,
                     **_exception_view(row)}

    def list_exceptions(request, body, scope, service):
        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")
        rows = service.store.exceptions_for_review(
            scope.organization_id, scope.repository_id, review_id)
        active = service.store.active_exception_for_attempt(
            scope.organization_id, scope.repository_id, review_id,
            int(record.get("attempt") or 1))
        return 200, {
            "review_id": review_id,
            # The decision as Relium computed it. Unchanged by any exception.
            "decision": record.get("decision"),
            "attempt": record.get("attempt"),
            # The exception in force for the CURRENT attempt, if any.
            "active_exception": _exception_view(active) if active else None,
            "total": len(rows),
            "items": [_exception_view(r) for r in rows],
        }

    def review_findings(request, body, scope, service):
        """Findings for one review, by attempt.

        Findings are computed by the decision engine and preserved on each
        immutable attempt. They were reachable only by reading the database
        directly, so no dashboard could show why a decision was what it was.
        """
        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")

        attempts = service.store.review_attempts(
            scope.organization_id, scope.repository_id, review_id)
        requested = request.query_params.get("attempt")
        if requested is not None:
            try:
                wanted = int(requested)
            except ValueError:
                raise ValidationError("'attempt' must be an integer",
                                      field="attempt") from None
            attempts = [a for a in attempts if a["attempt"] == wanted]

        return 200, {
            "review_id": review_id,
            "current_attempt": record.get("attempt"),
            "attempts": [
                {
                    "attempt": a["attempt"],
                    "decision": a.get("decision"),
                    "evidence_coverage": a.get("evidence_coverage"),
                    "health": a.get("health"),
                    "lifecycle_state": a.get("lifecycle_state"),
                    "trigger": a.get("trigger"),
                    "snapshot_id": a.get("snapshot_id"),
                    "created_at": isoformat(a.get("created_at")),
                    "findings": [_finding_view(f)
                                 for f in (a.get("payload") or {}).get("findings", [])],
                }
                for a in attempts
            ],
        }

    def review_attempts(request, body, scope, service):
        """Attempt history and lifecycle transitions for one review."""
        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")

        attempts = service.store.review_attempts(
            scope.organization_id, scope.repository_id, review_id)
        transitions = service.store.review_transitions(
            scope.organization_id, scope.repository_id, review_id)
        return 200, {
            "review_id": review_id,
            "current_attempt": record.get("attempt"),
            "attempts": [
                {"attempt": a["attempt"], "decision": a.get("decision"),
                 "evidence_coverage": a.get("evidence_coverage"),
                 "health": a.get("health"),
                 "lifecycle_state": a.get("lifecycle_state"),
                 "trigger": a.get("trigger"), "snapshot_id": a.get("snapshot_id"),
                 "enforcement_mode": a.get("enforcement_mode"),
                 "policy_version": a.get("policy_version"),
                 "finding_count": len((a.get("payload") or {}).get("findings", [])),
                 # Bound to THIS attempt row, so evidence from attempt N can
                 # never surface on attempt N+1 just because the review id
                 # matches.
                 "semantic_evidence": _semantic_evidence_view(a.get("semantic_evidence")),
                 # Same binding rule: this describes the two production
                 # observations THIS attempt compared, and is never re-derived
                 # from whatever the newest snapshot happens to be now.
                 "metadata_comparison": _metadata_comparison_view(
                     a.get("metadata_comparison")),
                 "created_at": isoformat(a.get("created_at"))}
                for a in attempts
            ],
            "transitions": [
                {"from_state": t.get("from_state"), "to_state": t.get("to_state"),
                 "reason": t.get("reason"),
                 "created_at": isoformat(t.get("created_at"))}
                for t in transitions
            ],
        }

    def review_metadata_evidence(request, body, scope, service):
        """The downloadable production metadata evidence bundle for one attempt.

        Built from the comparison THAT ATTEMPT recorded, and from the two
        immutable snapshots whose ids are inside it. The store is never asked
        for "the latest snapshot" here, so this file does not change when a
        newer observation arrives - which is the whole reason it is worth
        keeping.

        A 404 when the attempt recorded no comparison: there is no evidence to
        download, and a file asserting that would be worse than no file.
        """
        review_id = request.path_params["review_id"]
        try:
            attempt = int(request.path_params["attempt"])
        except (TypeError, ValueError):
            raise NotFoundError("unknown attempt") from None

        review = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if review is None:
            raise NotFoundError("unknown review")

        rows = service.store.review_attempts(
            scope.organization_id, scope.repository_id, review_id)
        row = next((a for a in rows if a["attempt"] == attempt), None)
        if row is None:
            raise NotFoundError("unknown attempt")

        # The SAME projection the dashboard reads, so the file and the screen
        # can never disagree about what changed.
        comparison = _metadata_comparison_view(row.get("metadata_comparison"))
        if comparison is None:
            raise NotFoundError(
                "no production metadata comparison was computed for this attempt")

        bundle = build_evidence_bundle(
            service.store,
            organization_id=scope.organization_id,
            repository_id=scope.repository_id,
            environment=review.get("environment"),
            review_id=review_id, attempt=attempt, comparison=comparison,
        )
        return 200, bundle, evidence_filename(review_id, attempt)

    def review_impact_report(request, body, scope, service):
        """The canonical per-review impact report, as Markdown.

        Assembled from the SAME projections the dashboard reads, then handed
        to the one renderer. The in-app report view and this download are
        therefore the same bytes: there is no second generator that could
        drift from what the screen shows.

        Scoped like every other review read, so a collector token cannot
        reach it and a cross-tenant review is indistinguishable from one that
        does not exist.
        """
        review_id = request.path_params["review_id"]
        try:
            attempt = int(request.path_params["attempt"])
        except (TypeError, ValueError):
            raise NotFoundError("unknown attempt") from None

        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")

        rows = service.store.review_attempts(
            scope.organization_id, scope.repository_id, review_id)
        row = next((a for a in rows if a["attempt"] == attempt), None)
        if row is None:
            raise NotFoundError("unknown attempt")

        payload = row.get("payload") or {}
        snapshot_id = row.get("snapshot_id")
        snapshot = None
        if snapshot_id:
            snapshot = next(
                (s for s in service.store.snapshots_for_review(
                    scope.organization_id, scope.repository_id, review_id)
                 if s.get("snapshot_id") == snapshot_id), None)

        review_view = dict(_review_view(record))
        review_view["repository"] = (
            f"{scope.organization_id}/{scope.repository_id}")

        markdown = render_review_impact_report(
            review=review_view,
            attempt={
                "attempt": row.get("attempt"),
                "decision": row.get("decision"),
                "evidence_coverage": row.get("evidence_coverage"),
                "health": row.get("health"),
                "trigger": row.get("trigger"),
                "snapshot_id": snapshot_id,
                "policy_version": row.get("policy_version"),
            },
            findings=[_finding_view(f)
                      for f in (payload.get("findings") or [])],
            semantic=row.get("semantic_evidence"),
            change_plan=_change_plan_view(record),
            comparison=_metadata_comparison_view(row.get("metadata_comparison")),
            snapshot={
                "completeness": snapshot.get("completeness"),
                "freshness_state": snapshot.get("freshness_state"),
                "observed_at": isoformat(snapshot.get("observed_at")),
            } if snapshot else None,
            attempts=[
                {"attempt": a.get("attempt"), "trigger": a.get("trigger"),
                 "decision": a.get("decision"),
                 "evidence_coverage": a.get("evidence_coverage"),
                 "lifecycle_state": a.get("lifecycle_state")}
                for a in rows
            ],
        )
        return 200, markdown, impact_report_filename(review_id, attempt)

    def review_collection_requests(request, body, scope, service):
        """Every collection request raised for one review, with its outcome."""
        review_id = request.path_params["review_id"]
        if service.store.get_review(scope.organization_id, scope.repository_id,
                                    review_id) is None:
            raise NotFoundError("unknown review")
        rows = service.store.collection_requests_for_review(
            scope.organization_id, scope.repository_id, review_id)
        return 200, {"review_id": review_id, "total": len(rows),
                     "items": [_collection_request_view(r) for r in rows]}

    def review_snapshots(request, body, scope, service):
        """Metadata snapshots submitted against one review, newest first."""
        review_id = request.path_params["review_id"]
        if service.store.get_review(scope.organization_id, scope.repository_id,
                                    review_id) is None:
            raise NotFoundError("unknown review")
        rows = service.store.snapshots_for_review(
            scope.organization_id, scope.repository_id, review_id)
        bindings = {}
        for binding in service.store.review_bindings(
                scope.organization_id, scope.repository_id, review_id):
            bindings.setdefault(binding["snapshot_id"], []).append({
                "binding_state": binding["binding_state"],
                "rejection_reason": binding.get("rejection_reason"),
            })
        return 200, {
            "review_id": review_id, "total": len(rows),
            "items": [{**_snapshot_summary_view(r),
                       "bindings": bindings.get(r["snapshot_id"], [])}
                      for r in rows],
        }

    def review_publications(request, body, scope, service):
        """Where this review has been published, and under which identity.

        The GitHub comment and check-run ids are the reconciliation evidence:
        an unchanged id across attempts proves the sticky object was updated
        rather than duplicated.
        """
        review_id = request.path_params["review_id"]
        record = service.store.get_review(
            scope.organization_id, scope.repository_id, review_id)
        if record is None:
            raise NotFoundError("unknown review")
        channels = {}
        for delivery in service.store.deliveries(
                scope.organization_id, scope.repository_id, record["environment"]):
            if review_id not in str(delivery.get("event_key", "")):
                continue
            channels.setdefault(delivery["channel"], []).append({
                "event_key": delivery["event_key"], "status": delivery["status"],
                "attempts": delivery["attempts"],
                "remote_id": delivery.get("remote_id"),
                "reconciled_at": isoformat(delivery.get("reconciled_at")),
            })
        return 200, {
            "review_id": review_id,
            "github": {
                "comment_id": record.get("github_comment_id"),
                "check_run_id": record.get("github_check_run_id"),
                "pull_number": record.get("pull_number"),
            },
            "channels": channels,
        }

    def list_deployments(request, body, scope, service):
        limit, offset = _page(request)
        page = service.store.list_deployments(
            scope.organization_id, scope.repository_id,
            environment=_env_filter(request, scope),
            status=request.query_params.get("status"),
            limit=limit, offset=offset,
        )
        return 200, {
            "total": page["total"], "limit": limit, "offset": offset,
            "items": [_deployment_view(d) for d in page["items"]],
        }

    def get_deployment(request, body, scope, service):
        record = service.store.get_deployment(
            scope.organization_id, scope.repository_id, request.path_params["deployment_id"]
        )
        if record is None:
            raise NotFoundError("unknown deployment")
        transitions = service.store.transitions(
            scope.organization_id, scope.repository_id, record["environment"], record["deployment_id"]
        )
        view = _deployment_view(record)
        view["transitions"] = [
            {"from_status": t["from_status"], "to_status": t["to_status"],
             "sequence": t["sequence"], "created_at": isoformat(t["created_at"])}
            for t in transitions
        ]
        return 200, view

    def monitoring_status(request, body, scope, service):
        status = service.store.monitoring_status(
            scope.organization_id, scope.repository_id, environment=_env_filter(request, scope)
        )
        status["latest_observation_at"] = isoformat(status.get("latest_observation_at"))
        return 200, status

    def list_observations(request, body, scope, service):
        limit, offset = _page(request)
        page = service.store.list_observations(
            scope.organization_id, scope.repository_id,
            environment=_env_filter(request, scope),
            deployment_id=request.query_params.get("deployment_id"),
            limit=limit, offset=offset,
        )
        return 200, {
            "total": page["total"], "limit": limit, "offset": offset,
            "items": [_observation_view(o) for o in page["items"]],
        }

    def list_anomalies(request, body, scope, service):
        limit, offset = _page(request)
        page = service.store.list_anomalies(
            scope.organization_id, scope.repository_id,
            environment=_env_filter(request, scope),
            deployment_id=request.query_params.get("deployment_id"),
            limit=limit, offset=offset,
        )
        return 200, {
            "total": page["total"], "limit": limit, "offset": offset,
            "items": [_anomaly_view(a) for a in page["items"]],
        }

    def list_incidents(request, body, scope, service):
        limit, offset = _page(request)
        page = service.store.list_incidents(
            scope.organization_id, scope.repository_id,
            environment=_env_filter(request, scope), limit=limit, offset=offset,
        )
        return 200, {
            "total": page["total"], "limit": limit, "offset": offset,
            "items": [
                {"incident_id": i["incident_id"], "status": i["status"],
                 "environment": i["environment"], "deployment_id": i.get("deployment_id"),
                 "anomaly_id": i.get("anomaly_id"), "created_at": isoformat(i.get("created_at"))}
                for i in page["items"]
            ],
        }

    def incident_detail(request, body, scope, service):
        return 200, service.incident_detail(scope, request.path_params["incident_id"])

    def incident_rca(request, body, scope, service):
        return 200, service.rca_detail(scope, request.path_params["incident_id"])

    def model_lineage(request, body, scope, service):
        records = service.store.lineage_for_model(
            scope.organization_id, scope.repository_id, request.path_params["model"],
            environment=_env_filter(request, scope),
        )
        if not records:
            raise NotFoundError("unknown model lineage")
        return 200, {
            "model": request.path_params["model"],
            "snapshots": [
                {"lineage_id": r["lineage_id"], "completeness": r.get("completeness"),
                 "payload": r["payload"], "edges": r.get("edges", []),
                 "created_at": isoformat(r.get("created_at"))}
                for r in records
            ],
        }

    def kpi_impact(request, body, scope, service):
        limit, offset = _page(request)
        page = service.store.kpi_impact_for_kpi(
            scope.organization_id, scope.repository_id, request.path_params["kpi"],
            environment=_env_filter(request, scope), limit=limit, offset=offset,
        )
        return 200, {
            "kpi": request.path_params["kpi"],
            "total": page["total"], "limit": limit, "offset": offset,
            "items": [
                {"kpi_impact_id": k["kpi_impact_id"], "deployment_id": k.get("deployment_id"),
                 "impact": k["impact"], "created_at": isoformat(k.get("created_at"))}
                for k in page["items"]
            ],
        }

    def repository_settings(request, body, scope, service):
        requested = request.path_params["repository"]
        if requested != scope.repository_id:
            raise AuthorizationError("repository outside token scope")
        settings = service.store.repository_settings(scope.organization_id, scope.repository_id)
        settings["environments"] = [
            {"environment": e["environment"], "connected": e["connected"],
             "created_at": isoformat(e.get("created_at"))}
            for e in settings["environments"]
        ]
        return 200, settings

    def evidence_coverage(request, body, scope, service):
        return 200, service.store.evidence_coverage(
            scope.organization_id, scope.repository_id, environment=_env_filter(request, scope)
        )

    def delivery_status(request, body, scope, service):
        environment = _env_filter(request, scope)
        if environment is None:
            raise ValidationError("'environment' query parameter is required", field="environment")
        channels: dict[str, list] = {}
        for record in service.store.deliveries(
            scope.organization_id, scope.repository_id, environment
        ):
            channels.setdefault(record["channel"], []).append({
                "journal_id": record["journal_id"],
                "event_key": record["event_key"],
                "status": record["status"],
                "attempts": record["attempts"],
                "remote_id": record.get("remote_id"),
                "reconciled_at": isoformat(record.get("reconciled_at")),
            })
        for entries in channels.values():
            entries.sort(key=lambda e: e["event_key"])
        return 200, {"environment": environment, "channels": channels}

    _collector = build_handlers()

    routes = [
        Route("/api/deployments/events", handler(post_deployment_event, write=True, capability=PIPELINE_INGEST), methods=["POST"]),
        Route("/api/monitoring/baselines", handler(post_baseline, write=True, capability=PIPELINE_INGEST), methods=["POST"]),
        Route("/api/monitoring/observations", handler(post_observation, write=True, capability=PIPELINE_INGEST), methods=["POST"]),
        Route("/api/anomalies", handler(list_anomalies, write=False), methods=["GET"]),
        Route("/api/anomalies", handler(post_anomaly, write=True, capability=PIPELINE_INGEST), methods=["POST"]),
        Route("/api/incidents", handler(list_incidents, write=False), methods=["GET"]),
        Route("/api/incidents", handler(post_incident_rca, write=True, capability=PIPELINE_INGEST), methods=["POST"]),
        Route("/api/reviews", handler(list_reviews, write=False), methods=["GET"]),
        Route("/api/reviews", handler(post_review, write=True, capability=PIPELINE_INGEST), methods=["POST"]),
        Route("/api/reviews/{review_id}", handler(get_review, write=False), methods=["GET"]),
        Route("/api/reviews/{review_id}/rerun",
              handler(rerun_review, write=True), methods=["POST"]),
        Route("/api/reviews/{review_id}/request-changes",
              handler(request_changes, write=True), methods=["POST"]),
        Route("/api/reviews/{review_id}/change-requests",
              handler(list_change_requests, write=False), methods=["GET"]),
        Route("/api/reviews/{review_id}/exceptions",
              handler(approve_exception, write=True), methods=["POST"]),
        Route("/api/reviews/{review_id}/exceptions",
              handler(list_exceptions, write=False), methods=["GET"]),
        Route("/api/reviews/{review_id}/exceptions/{exception_id}/revoke",
              handler(revoke_exception, write=True), methods=["POST"]),
        Route("/api/reviews/{review_id}/findings",
              handler(review_findings, write=False), methods=["GET"]),
        Route("/api/reviews/{review_id}/attempts",
              handler(review_attempts, write=False), methods=["GET"]),
        # Dashboard read, so a collector token cannot fetch it: the capability
        # model already refuses COLLECTOR_INGEST here, and this route is one
        # more reason that separation has to hold.
        # Same capability and same scoping as the JSON bundle beside it: the
        # report is review evidence, so DASHBOARD_READ governs it and a
        # collector credential is refused.
        Route("/api/reviews/{review_id}/attempts/{attempt}/impact-report.md",
              handler(review_impact_report, write=False, download=True),
              methods=["GET"]),
        Route("/api/reviews/{review_id}/attempts/{attempt}/metadata-evidence.json",
              handler(review_metadata_evidence, write=False, download=True),
              methods=["GET"]),
        Route("/api/reviews/{review_id}/collection-requests",
              handler(review_collection_requests, write=False), methods=["GET"]),
        Route("/api/reviews/{review_id}/snapshots",
              handler(review_snapshots, write=False), methods=["GET"]),
        Route("/api/reviews/{review_id}/publications",
              handler(review_publications, write=False), methods=["GET"]),
        Route("/api/deployments", handler(list_deployments, write=False), methods=["GET"]),
        Route("/api/deployments/{deployment_id}", handler(get_deployment, write=False), methods=["GET"]),
        Route("/api/monitoring", handler(monitoring_status, write=False), methods=["GET"]),
        Route("/api/monitoring/observations", handler(list_observations, write=False), methods=["GET"]),
        Route("/api/incidents/{incident_id}", handler(incident_detail, write=False), methods=["GET"]),
        Route("/api/incidents/{incident_id}/rca", handler(incident_rca, write=False), methods=["GET"]),
        Route("/api/models/{model}/lineage", handler(model_lineage, write=False), methods=["GET"]),
        Route("/api/kpis/{kpi}/impact", handler(kpi_impact, write=False), methods=["GET"]),
        Route("/api/repositories/{repository}/settings", handler(repository_settings, write=False), methods=["GET"]),
        Route("/api/evidence-coverage", handler(evidence_coverage, write=False), methods=["GET"]),
        Route("/api/delivery-status", handler(delivery_status, write=False), methods=["GET"]),

        # Collector control and metadata snapshot surface. Registered on the
        # same application, behind the same authentication and tenant scoping.
        *[
            Route(path,
                  handler(_collector[name], write=write,
                          capability=_COLLECTOR_CAPABILITY[name]),
                  methods=[method])
            for method, path, name, write in COLLECTOR_ROUTES
        ],
    ]
    return routes


# -- response projections. Only disclosed fields cross the API boundary. -------

def _change_plan_view(record):
    payload = record.get("payload")
    plan = payload.get("plan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        plan = {}

    def string_list(field):
        values = plan.get(field)
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, str)]

    target_values = plan.get("targets")
    if not isinstance(target_values, list):
        target_values = []

    targets = []
    for target in target_values:
        if not isinstance(target, dict):
            continue
        columns = target.get("columns")
        targets.append({
            "relation_name": target.get("relation_name")
            if isinstance(target.get("relation_name"), str) else None,
            "model_unique_id": target.get("model_unique_id")
            if isinstance(target.get("model_unique_id"), str) else None,
            "dependency_kind": target.get("dependency_kind")
            if isinstance(target.get("dependency_kind"), str) else None,
            "columns": [value for value in columns if isinstance(value, str)]
            if isinstance(columns, list) else [],
            "reason": target.get("reason")
            if isinstance(target.get("reason"), str) else None,
        })

    # Direct blast-radius edges, when the analysis that produced this review
    # recorded them. The distinction between "not recorded" and "recorded and
    # empty" is load-bearing and must survive the boundary: null means this
    # review predates the evidence and a graph cannot be drawn for it, while
    # [] means the analysis ran and found no direct downstream. Reconstructing
    # edges from the flat lists would be inference, so it is not done here.
    edge_values = plan.get("direct_edges")
    if isinstance(edge_values, list):
        direct_edges = []
        for edge in edge_values:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source_model_unique_id")
            target = edge.get("target_model_unique_id")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            if not source or not target:
                continue
            direct_edges.append({
                "source_model_unique_id": source,
                "target_model_unique_id": target,
            })
    else:
        direct_edges = None

    return {
        "changed_models": string_list("changed_models"),
        "added_dependencies": string_list("added_dependencies"),
        "removed_dependencies": string_list("removed_dependencies"),
        "downstream_models": string_list("downstream_models"),
        "direct_edges": direct_edges,
        "targets": targets,
    }


def _review_view(record):
    """Review identity, decision and evidence state.

    Attempt, lifecycle state, health and the git/manifest binding were all
    persisted but none of them crossed the API, so a dashboard could show a
    decision without being able to say which code state it described or
    whether it was still provisional. Every field here is already non-secret
    review metadata. The change plan is a deliberately small projection; raw
    manifests and arbitrary persisted payload fields never cross this boundary.
    """
    return {
        "review_id": record["review_id"],
        "environment": record["environment"],
        "pull_number": record.get("pull_number"),
        "commit_sha": record.get("commit_sha"),
        "decision": record["decision"],
        "lifecycle_state": record.get("lifecycle_state"),
        "attempt": record.get("attempt"),
        "enforcement_mode": record.get("enforcement_mode"),
        "risk_score": record.get("risk_score"),
        "evidence_coverage": record.get("evidence_coverage"),
        "health": record.get("health"),
        "metadata_required": record.get("metadata_required"),
        "base_sha": record.get("base_sha"),
        "head_sha": record.get("head_sha"),
        "base_manifest_hash": record.get("base_manifest_hash"),
        "head_manifest_hash": record.get("head_manifest_hash"),
        "policy_version": record.get("policy_version"),
        "policy_hash": record.get("policy_hash"),
        "change_plan": _change_plan_view(record),
        "created_at": isoformat(record.get("created_at")),
        "updated_at": isoformat(record.get("updated_at")),
    }


def _change_request_view(record):
    return {
        "change_request_id": record["change_request_id"],
        "review_id": record["review_id"],
        "attempt": record["attempt"],
        "pull_number": record["pull_number"],
        "actor": record["actor"],
        "message": record["message"],
        "state": record["state"],
        "remote_review_id": record.get("remote_review_id"),
        "failure_reason": record.get("failure_reason"),
        "created_at": isoformat(record.get("created_at")),
        "published_at": isoformat(record.get("published_at")),
    }


def _exception_view(record):
    """An exception, beside the decision it overrides - never replacing it."""
    if record is None:
        return None
    return {
        "exception_id": record["exception_id"],
        "review_id": record["review_id"],
        "attempt": record["attempt"],
        # What RELIUM decided. Preserved so the pair stays legible.
        "overridden_decision": record.get("overridden_decision"),
        "actor": record["actor"],
        "reason": record["reason"],
        "scope": record["scope"],
        "state": record["state"],
        "base_sha": record.get("base_sha"),
        "head_sha": record.get("head_sha"),
        "created_at": isoformat(record.get("created_at")),
        "revoked_at": isoformat(record.get("revoked_at")),
        "revoked_by": record.get("revoked_by"),
        "revocation_reason": record.get("revocation_reason"),
    }


def _finding_view(finding):
    """One finding, exactly as the decision engine produced it.

    ``detail`` carries the measured value and the configured threshold, which
    is what makes a finding checkable rather than assertable. It is engine
    output - never a warehouse row - so it is disclosed unchanged.
    """
    if not isinstance(finding, dict):
        return {"code": "unreadable", "severity": "info", "category": "evidence",
                "message": "finding could not be read", "detail": {}}
    return {
        "code": finding.get("code"),
        "severity": finding.get("severity"),
        "category": finding.get("category"),
        "message": finding.get("message"),
        "relation": finding.get("relation"),
        "column": finding.get("column"),
        "detail": finding.get("detail") or {},
    }


def _collection_request_view(record):
    plan = record.get("plan") or {}
    return {
        "request_id": record["request_id"],
        "review_id": record.get("review_id"),
        "environment": record["environment"],
        "state": record["state"],
        "reason": record.get("reason"),
        "priority": record.get("priority"),
        "required_evidence_level": record.get("required_evidence_level"),
        "acknowledged_by": record.get("acknowledged_by"),
        "acknowledged_at": isoformat(record.get("acknowledged_at")),
        "completed_at": isoformat(record.get("completed_at")),
        "failure_reason": record.get("failure_reason"),
        "expires_at": isoformat(record.get("expires_at")),
        "created_at": isoformat(record.get("created_at")),
        "base_sha": record.get("base_sha"),
        "head_sha": record.get("head_sha"),
        "base_manifest_hash": record.get("base_manifest_hash"),
        "head_manifest_hash": record.get("head_manifest_hash"),
        # The plan states what was asked for. It contains relation and column
        # names and requested signal names - never data.
        "plan": plan,
    }


def _snapshot_summary_view(record):
    return {
        "snapshot_id": record["snapshot_id"],
        "environment": record.get("environment"),
        "completeness": record.get("completeness"),
        "freshness_state": record.get("freshness_state"),
        "observed_at": isoformat(record.get("observed_at")),
        "collected_at": isoformat(record.get("collected_at")),
        "received_at": isoformat(record.get("received_at")),
        "request_id": record.get("request_id"),
        "collector_id": record.get("collector_id"),
        "collector_version": record.get("collector_version"),
        "adapter_type": record.get("adapter_type"),
        "ttl_seconds": record.get("ttl_seconds"),
        "base_sha": record.get("base_sha"),
        "head_sha": record.get("head_sha"),
        "base_manifest_hash": record.get("base_manifest_hash"),
        "head_manifest_hash": record.get("head_manifest_hash"),
    }


def _deployment_view(record):
    return {
        "deployment_id": record["deployment_id"],
        "environment": record["environment"],
        "status": record["status"],
        "reviewed_sha": record.get("reviewed_sha"),
        "merge_sha": record.get("merge_sha"),
        "manifest_hash": record.get("manifest_hash"),
        "created_at": isoformat(record.get("created_at")),
        "updated_at": isoformat(record.get("updated_at")),
    }


def _observation_view(record):
    return {
        "observation_id": record["observation_id"],
        "environment": record["environment"],
        "deployment_id": record.get("deployment_id"),
        "model": record.get("model"),
        "metric": record["metric"],
        "value": record["payload"],
        "observed_at": isoformat(record.get("observed_at")),
        "received_at": isoformat(record.get("received_at")),
        "evidence_coverage": record.get("evidence_coverage"),
        "source": record.get("source"),
    }


def _anomaly_view(record):
    return {
        "anomaly_id": record["anomaly_id"],
        "environment": record["environment"],
        "deployment_id": record.get("deployment_id"),
        "kind": record["kind"],
        "severity": record.get("severity"),
        "evidence": record["payload"],
        "affected_models": record.get("affected_models", []),
        "affected_kpis": record.get("affected_kpis", []),
        "observation_ids": record.get("observation_ids", []),
        "detected_at": isoformat(record.get("detected_at")),
        "created_at": isoformat(record.get("created_at")),
    }
