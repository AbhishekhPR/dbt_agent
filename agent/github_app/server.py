import json
import logging
import sys
import time
from functools import partial

import uvicorn

from agent.github_app.adapter import GitHubAppAdapter
from agent.github_app.client import GitHubClient
from agent.github_app.http_app import create_http_app
from agent.github_app.jobs import BoundedJobQueue, RetryPolicy, RetryingJobProcessor
from agent.github_app.runner import PullRequestReviewRunner
from agent.metadata_evidence.service import build_review_lifecycle
from agent.github_app.service import WebhookProcessingService
from agent.github_app.settings import SettingsError, load_settings
from agent.github_app.slack import SlackPublicationSink
from agent.github_app.storage import RepositoryStorage


_LOG_FIELDS = (
    "delivery_id",
    "publication_id",
    "event_name",
    "repository",
    "pull_number",
    "attempt",
    "processing_outcome",
    "duration",
    "error_category",
    "operation",
    "http_method",
    "route_template",
    "http_status",
    "github_request_id",
    "accepted_github_permissions",
    "github_message_category",
    "response_representation",
    "retryable",
)


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for name in _LOG_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(level=logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def build_application(settings, *, client_factory=None, logger=None, sleep=time.sleep):
    logger = logger or logging.getLogger("relium.github_app")
    storage = RepositoryStorage(settings.storage_root)
    slack_publisher = None
    if settings.slack_webhook_url:
        slack_publisher = SlackPublicationSink(
            settings.slack_webhook_url,
            notify_warn=settings.slack_notify_warn,
            max_retries=settings.slack_max_retries,
            retry_base_seconds=settings.slack_retry_base_seconds,
            timeout_seconds=settings.request_timeout_seconds,
            sleep=sleep,
            logger=logger,
        )
    # The store pool is built before the runner so the served review path can
    # receive an explicit lifecycle dependency. Release 1 built the pool after
    # the runner and never passed it, which is why genuine webhooks never
    # reached PostgreSQL.
    store_pool = None
    if settings.database_url:
        from agent.api.pool import StorePool
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store_pool = StorePool(
            lambda: PostgresLifecycleStore(settings.database_url),
            size=settings.api_pool_size,
        )

    lifecycle = build_review_lifecycle(
        store_pool,
        metadata_review_enabled=settings.metadata_review_enabled,
        environment=settings.metadata_review_environment,
    )
    logger.info("review lifecycle mode: %s", lifecycle.mode)

    runner = PullRequestReviewRunner(
        storage=storage,
        slack_publisher=slack_publisher,
        lifecycle=lifecycle,
    )
    if client_factory is None:
        client_factory = partial(
            GitHubClient, timeout=settings.request_timeout_seconds
        )
    adapter = GitHubAppAdapter(
        webhook_secret=settings.webhook_secret,
        app_id=settings.app_id,
        private_key=settings.private_key,
        runner=runner,
        client_factory=client_factory,
    )
    service = WebhookProcessingService(adapter, logger=logger)
    retrying_processor = RetryingJobProcessor(
        service.process,
        RetryPolicy(
            max_retries=settings.max_retries,
            base_seconds=settings.retry_base_seconds,
        ),
        sleep=sleep,
        logger=logger,
        job_store=storage,
    )
    jobs = BoundedJobQueue(
        retrying_processor,
        worker_count=settings.worker_count,
        capacity=settings.queue_capacity,
        logger=logger,
        job_store=storage,
    )
    app = create_http_app(
        webhook_secret=settings.webhook_secret,
        job_queue=jobs,
        max_body_bytes=settings.max_body_bytes,
        shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
        clock=time.time,
        logger=logger,
        job_store=storage,
        store_pool=store_pool,
        review_lifecycle_mode=lifecycle.mode,
        cors_allowed_origins=settings.cors_allowed_origins,
    )
    app.state.job_queue = jobs
    return app


def main(environ=None, *, run_server=None) -> None:
    configure_logging()
    try:
        settings = load_settings(environ)
        app = build_application(settings)
    except SettingsError as exc:
        print(f"Relium GitHub App server configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except OSError:
        print(
            "Relium GitHub App server startup error: runtime storage could not "
            "be initialized.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    (run_server or uvicorn.run)(
        app,
        host=settings.host,
        port=settings.port,
        lifespan="on",
        log_config=None,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )


if __name__ == "__main__":
    main()
