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

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.api.auth import AuthenticationError, AuthorizationError, ServiceTokenAuthenticator, bearer_token
from agent.api.service import ConflictError, LifecycleService, NotFoundError
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


def create_api_routes(*, store_pool, authenticator_factory=None):
    """Build the /api route table. Registration stays explicit and inspectable."""

    def _authenticate(request, store):
        authenticator = (authenticator_factory or ServiceTokenAuthenticator)(store)
        return authenticator.authenticate(bearer_token(request.headers.get("Authorization")))

    def handler(fn, *, write: bool):
        async def wrapped(request):
            request_id = _request_id(request)
            body = None
            try:
                if write:
                    body = await _read_json(request, request_id)

                def work():
                    with store_pool.acquire() as store:
                        scope = _authenticate(request, store)
                        service = LifecycleService(store)
                        return fn(request, body, scope, service)

                status, payload = await run_in_threadpool(work)
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
            except Exception:
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

    routes = [
        Route("/api/deployments/events", handler(post_deployment_event, write=True), methods=["POST"]),
        Route("/api/monitoring/baselines", handler(post_baseline, write=True), methods=["POST"]),
        Route("/api/monitoring/observations", handler(post_observation, write=True), methods=["POST"]),
        Route("/api/anomalies", handler(list_anomalies, write=False), methods=["GET"]),
        Route("/api/anomalies", handler(post_anomaly, write=True), methods=["POST"]),
        Route("/api/incidents", handler(list_incidents, write=False), methods=["GET"]),
        Route("/api/incidents", handler(post_incident_rca, write=True), methods=["POST"]),
        Route("/api/reviews", handler(list_reviews, write=False), methods=["GET"]),
        Route("/api/reviews", handler(post_review, write=True), methods=["POST"]),
        Route("/api/reviews/{review_id}", handler(get_review, write=False), methods=["GET"]),
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
    ]
    return routes


# -- response projections. Only disclosed fields cross the API boundary. -------

def _review_view(record):
    return {
        "review_id": record["review_id"],
        "environment": record["environment"],
        "pull_number": record.get("pull_number"),
        "commit_sha": record.get("commit_sha"),
        "decision": record["decision"],
        "enforcement_mode": record.get("enforcement_mode"),
        "risk_score": record.get("risk_score"),
        "evidence_coverage": record.get("evidence_coverage"),
        "created_at": isoformat(record.get("created_at")),
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
