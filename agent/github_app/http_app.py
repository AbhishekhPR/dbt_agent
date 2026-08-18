import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.api.routes import create_api_routes
from agent.github_app.jobs import WebhookJob
from agent.github_app.models import InstallationEvent
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
    store_pool=None,
    review_lifecycle_mode=None,
    cors_allowed_origins=(),
    session_manager=None,
    auth_routes=(),
    clerk_verifier=None,
    installation_binder=None,
    identity_linker=None,
    app_url="",
    repository_service=None,
    api_url="",
    dashboard_bridge=None,
    secure_cookies=True,
):
    """Build the served application.

    ``store_pool`` enables the public lifecycle and dashboard API. When it is
    absent (deployments that only run the GitHub App), the /api routes are not
    registered and /readyz reports the API as disabled -- the webhook and health
    behaviour are unchanged either way.
    """
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

    async def readiness(request):
        """Read-only readiness probe. Never mutates lifecycle data."""
        worker_running = bool(request.app.state.started and job_queue.is_running)
        checks = {
            "worker": "ok" if worker_running else "unavailable",
            "configuration": "ok",
        }
        if review_lifecycle_mode is not None:
            # Which review lifecycle is actually active. Sanitized: a mode
            # name only, never a connection detail.
            checks["review_lifecycle"] = review_lifecycle_mode
        if store_pool is None:
            checks["database"] = "disabled"
            checks["migrations"] = "disabled"
            checks["public_api"] = "disabled"
        else:
            def probe():
                from agent.postgres_migrate import applied_versions, pending_migrations

                with store_pool.acquire() as store:
                    store.connection.execute("SELECT 1")
                    applied = applied_versions(store.connection)
                    pending = pending_migrations(set(applied))
                    stats = store.outbox_stats()
                    # Surface whether the durable outbox is actually being
                    # consumed, so a live run can tell a stalled worker from an
                    # idle one rather than inferring it.
                    backlog = store.connection.execute(
                        "SELECT COUNT(*) AS ready, MIN(next_attempt_at) AS oldest "
                        "FROM outbox_events WHERE state='PENDING' AND next_attempt_at <= now()"
                    ).fetchone()
                    stalled = store.connection.execute(
                        "SELECT COUNT(*) AS c FROM outbox_events "
                        "WHERE state='CLAIMED' AND lease_expires_at < now()"
                    ).fetchone()["c"]
                    stats["ready_now"] = backlog["ready"]
                    stats["oldest_ready_at"] = (
                        backlog["oldest"].isoformat() if backlog["oldest"] else None
                    )
                    stats["expired_claims"] = stalled
                    return applied, [p.name for p in pending], stats

            try:
                applied, pending, outbox = await run_in_threadpool(probe)
                checks["database"] = "ok"
                checks["migrations"] = "current" if not pending else "pending"
                checks["applied_migrations"] = applied
                checks["public_api"] = "ok"
                checks["outbox"] = outbox
            except Exception:
                logger.error("readiness_probe_failed", extra={"error_category": "database"})
                checks["database"] = "unavailable"
                checks["migrations"] = "unknown"
                checks["public_api"] = "unavailable"

        ready = (
            worker_running
            and checks.get("database") in {"ok", "disabled"}
            and checks.get("migrations") in {"current", "disabled"}
        )
        return JSONResponse(
            {"status": "ready" if ready else "unavailable", "checks": checks},
            status_code=200 if ready else 503,
        )

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

        # Installation lifecycle. Recorded directly rather than queued: it is a
        # small idempotent write with no analysis behind it, and it must land
        # even when the review worker is busy or stopped.
        #
        # ###########################################################
        # # THIS RECORDS FACTS. IT NEVER BINDS A TENANT.            #
        # ###########################################################
        #
        # A signature-verified delivery proves GitHub sent it. It does not say
        # which Relium customer installed the App — nothing in the payload
        # identifies a tenant, and the fields that look like they might (the
        # account login, the sender) are names anyone can choose. Tenancy is
        # established only by the verified Setup flow in
        # agent/api/github_installation.py.
        if isinstance(event, InstallationEvent):
            if store_pool is None:
                return JSONResponse(
                    {"status": "ignored", "delivery_id": delivery_id},
                    status_code=202,
                )
            try:
                await run_in_threadpool(_record_installation, store_pool, event)
            except Exception:
                logger.error(
                    "installation_event_persist_failed",
                    extra={"error_category": "database"},
                )
                # 503 so GitHub retries. The write is idempotent, so a retry
                # after a partial failure converges rather than duplicating.
                return JSONResponse({"status": "unavailable"}, status_code=503)
            return JSONResponse(
                {"status": "accepted", "delivery_id": delivery_id},
                status_code=202,
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

    routes = [
        Route("/healthz", health, methods=["GET"]),
        Route("/readyz", readiness, methods=["GET"]),
        Route("/github/webhook", webhook, methods=["POST"]),
    ]
    if store_pool is not None:
        routes.extend(create_api_routes(
            store_pool=store_pool,
            session_manager=session_manager,
            allowed_origins=cors_allowed_origins,
            clerk_verifier=clerk_verifier,
            installation_binder=installation_binder,
            identity_linker=identity_linker,
            app_url=app_url,
            repository_service=repository_service,
            api_url=api_url,
            dashboard_bridge=dashboard_bridge,
            secure_cookies=secure_cookies,
        ))
    # Dashboard sign-in. Registered only when the App's user-authorization
    # credentials are fully configured, so there is never a login route that
    # cannot complete a login.
    routes.extend(auth_routes)

    # Browser access to the dashboard API.
    #
    # The /api routes are declared dashboard resources, but a browser served
    # from any other origin could not call them: the Authorization header makes
    # every request preflighted, and OPTIONS had no handler, so the preflight
    # answered 405 and the fetch never happened.
    #
    # Opt-in and explicit. With no configured origins there is no middleware at
    # all and behaviour is byte-identical to before. Origins are listed exactly;
    # there is no wildcard, because these routes carry a bearer token and a
    # wildcard would invite any page to spend it.
    middleware = []
    if cors_allowed_origins:
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware

        middleware.append(Middleware(
            CORSMiddleware,
            allow_origins=list(cors_allowed_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                           "X-Request-Id", "X-Relium-CSRF"],
            expose_headers=["X-Request-Id"],
            # The dashboard now authenticates with an HttpOnly session cookie,
            # so credentialed requests are required. This is safe only because
            # the origins are listed exactly and '*' is rejected at load time:
            # a wildcard with credentials would let any page in a user's
            # browser spend their session.
            allow_credentials=True,
            max_age=600,
        ))

    app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
        exception_handlers={Exception: unexpected_error},
    )
    app.state.started = False
    app.state.store_pool = store_pool
    app.state.cors_allowed_origins = list(cors_allowed_origins or ())
    return app


#: How an installation action maps to stored status. `deleted` is a soft
#: delete: uninstalling stops reviews, it does not erase what Relium did while
#: the App was installed.
_INSTALLATION_STATUS = {
    "created": "active",
    "deleted": "deleted",
    "suspend": "suspended",
    "unsuspend": "active",
    # installation_repositories deliveries do not change status; they refresh
    # repository_selection on a live installation.
    "added": None,
    "removed": None,
}


def _record_installation(store_pool, event):
    """Persist installation facts. Idempotent, so replays are harmless.

    GitHub retries deliveries and can reorder them, so this must converge
    rather than accumulate. The upsert is keyed on the installation id, which
    makes a duplicate delivery a no-op instead of a second row.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    status = _INSTALLATION_STATUS.get(event.action)

    with store_pool.acquire() as store:
        if status is None:
            # A repository-selection change on an installation we do not know
            # about is not a reason to create one: we would be recording an
            # installation nobody has verified. Ignore it; the Setup flow or a
            # `created` delivery is what brings it into existence.
            if store.github_installation(event.installation_id) is None:
                return
            existing = store.github_installation(event.installation_id)
            store.record_github_installation(
                event.installation_id,
                github_app_id=event.app_id,
                github_account_id=event.account_id,
                github_account_login=event.account_login,
                github_account_type=event.account_type,
                repository_selection=event.repository_selection,
                status=existing["status"],
                suspended_at=existing.get("suspended_at"),
                deleted_at=existing.get("deleted_at"),
            )
            return

        store.record_github_installation(
            event.installation_id,
            github_app_id=event.app_id,
            github_account_id=event.account_id,
            github_account_login=event.account_login,
            github_account_type=event.account_type,
            repository_selection=event.repository_selection,
            status=status,
            suspended_at=now if status == "suspended" else None,
            deleted_at=now if status == "deleted" else None,
        )


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
