import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.github_app.jobs import WebhookJob
from agent.github_app.signatures import verify_webhook_signature
from agent.github_app.webhooks import WebhookPayloadError, parse_webhook


def create_http_app(
    *,
    webhook_secret,
    job_queue,
    max_body_bytes: int,
    shutdown_timeout_seconds: float,
    clock,
    logger=None,
    job_store=None,
):
    if max_body_bytes <= 0:
        raise ValueError("Maximum webhook body size must be positive.")
    logger = logger or logging.getLogger(__name__)

    @asynccontextmanager
    async def lifespan(app):
        app.state.started = False
        startup_succeeded = False
        try:
            job_queue.start()
            startup_succeeded = True
            app.state.started = bool(job_queue.is_running)
        except Exception:
            logger.error(
                "job_queue_start_failed",
                extra={"error_category": "worker_startup"},
            )
        try:
            yield
        finally:
            app.state.started = False
            if startup_succeeded:
                job_queue.stop(timeout=shutdown_timeout_seconds)

    async def health(request):
        if request.app.state.started and job_queue.is_running:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    async def webhook(request):
        signature = request.headers.get("X-Hub-Signature-256")
        event_name = request.headers.get("X-GitHub-Event")
        delivery_id = request.headers.get("X-GitHub-Delivery")
        if (
            not _safe_signature_header(signature)
            or not _safe_event_name(event_name)
            or not _safe_delivery_id(delivery_id)
        ):
            return JSONResponse({"status": "invalid_request"}, status_code=400)

        raw_body = await _read_bounded_body(request, max_body_bytes)
        if raw_body is None:
            return JSONResponse({"status": "payload_too_large"}, status_code=413)
        if not verify_webhook_signature(
            secret=webhook_secret,
            body=raw_body,
            signature_header=signature,
        ):
            return JSONResponse({"status": "unauthorized"}, status_code=401)

        try:
            event = parse_webhook(
                event_name=event_name,
                delivery_id=delivery_id,
                body=raw_body,
            )
        except WebhookPayloadError:
            return JSONResponse({"status": "invalid_request"}, status_code=400)
        if event is None:
            return JSONResponse(
                {"status": "ignored", "delivery_id": delivery_id}, status_code=202
            )
        job = WebhookJob(
            delivery_id=delivery_id,
            event_name=event_name,
            raw_body=raw_body,
            received_at=clock(),
            repository_id=event.repository.id,
        )
        if job_store is not None:
            persisted = job_store.persist_verified_job(event.repository.id, job)
            if not persisted:
                return JSONResponse(
                    {"status": "duplicate", "delivery_id": delivery_id},
                    status_code=202,
                )
        if not job_queue.enqueue(job):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse(
            {"status": "accepted", "delivery_id": delivery_id}, status_code=202
        )

    async def unexpected_error(request, exc):
        logger.error("http_request_failed", extra={"error_category": "internal"})
        return JSONResponse({"status": "unavailable"}, status_code=500)

    app = Starlette(
        debug=False,
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/github/webhook", webhook, methods=["POST"]),
        ],
        lifespan=lifespan,
        exception_handlers={Exception: unexpected_error},
    )
    app.state.started = False
    return app


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                return None
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return None
        body.extend(chunk)
    return bytes(body)


def _safe_event_name(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 100
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _safe_signature_header(value) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256="):
        return False
    digest = value.removeprefix("sha256=")
    return len(digest) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in digest
    )


def _safe_delivery_id(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value not in {".", ".."}
        and all(character.isalnum() or character in "-_." for character in value)
    )
