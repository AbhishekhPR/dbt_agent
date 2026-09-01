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


def build_application(settings, *, client_factory=None, logger=None,
                      sleep=time.sleep, environ=None):
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
    # Dashboard sign-in, when the App's user-authorization credentials are
    # configured. Without them the /auth routes are absent and the dashboard
    # has no way in — which is the correct outcome, because the alternative
    # was a service token compiled into its JavaScript.
    session_manager = None
    auth_routes = ()
    if store_pool is not None and settings.dashboard_login_enabled:
        from agent.api.auth_routes import create_auth_routes
        from agent.api.sessions import SessionManager

        session_manager = SessionManager(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            encryption_key=settings.session_encryption_key,
        )
        auth_routes = create_auth_routes(
            store_pool=store_pool,
            session_manager=session_manager,
            dashboard_url=settings.dashboard_url,
            callback_url=settings.oauth_callback_url,
            organization_id=settings.dashboard_organization,
            repository_id=settings.dashboard_repository,
            environment=settings.metadata_review_environment,
            secure_cookies=settings.secure_cookies,
        )
        logger.info("dashboard login enabled for %s/%s",
                    settings.dashboard_organization, settings.dashboard_repository)
    elif store_pool is not None:
        logger.warning("dashboard login is NOT configured; /auth routes are absent")

    # ------------------------------------------------------------------
    # First-run onboarding: Clerk identity, installation binding, repository
    # and dbt configuration.
    #
    # Assembled here rather than inside create_http_app because every part of
    # it is optional configuration, and a deployment that has not configured
    # Clerk or the GitHub App must still start and serve reviews. What it must
    # NOT do is start and quietly authenticate people anyway, so each component
    # is built only when the credentials it needs are actually present, and the
    # routes answer 503 rather than vanishing when one is absent.
    clerk_verifier = None
    installation_binder = None
    identity_linker = None
    repository_service = None
    dashboard_bridge = None

    if store_pool is not None:
        from agent.api.clerk_identity import (
            ClerkConfigurationError, ClerkSettings, ClerkVerifier,
        )

        try:
            clerk_settings = ClerkSettings.from_environ(environ)
        except ClerkConfigurationError as exc:
            # Misconfiguration is fatal at boot rather than at the first
            # request: an https-only issuer that is not https would otherwise
            # fail on a customer, not on us.
            raise SettingsError(str(exc)) from None
        if clerk_settings is not None:
            clerk_verifier = ClerkVerifier(clerk_settings)
            logger.info("clerk authentication enabled")
        else:
            logger.warning(
                "RELIUM_CLERK_ISSUER is not set; onboarding routes are served "
                "but authenticate nobody")

        from agent.api.github_installation import (
            GitHubAppIdentity, GitHubIdentityLinker, InstallationBinder,
        )
        from agent.api.repository_onboarding import RepositoryOnboardingService
        from agent.github_app.auth import create_app_jwt

        # The App JWT is minted per call and lives for ten minutes. The private
        # key stays in settings and never leaves this closure.
        def app_jwt():
            return create_app_jwt(settings.app_id, settings.private_key)

        onboarding_client = client_factory() if client_factory else GitHubClient(
            timeout=settings.request_timeout_seconds)
        app_identity = GitHubAppIdentity(onboarding_client, app_jwt)

        # The installation binder needs the session encryption key: it decrypts
        # the stored GitHub user credential to ask GitHub, as that human,
        # whether they can really see the installation being claimed.
        if settings.session_encryption_key:
            installation_binder = InstallationBinder(
                app_identity=app_identity,
                client=onboarding_client,
                jwt_factory=app_jwt,
                session_key=settings.session_encryption_key,
            )
            repository_service = RepositoryOnboardingService(
                client=onboarding_client, jwt_factory=app_jwt)
        else:
            logger.warning(
                "RELIUM_SESSION_ENCRYPTION_KEY is not set; GitHub installation "
                "binding is disabled and onboarding cannot be completed")

        # Linking a Clerk user to a verified GitHub identity reuses the App's
        # user-authorization credentials -- the same client id and secret the
        # dashboard login uses, on a separate callback.
        if (settings.github_client_id and settings.github_client_secret
                and settings.session_encryption_key and settings.public_url):
            identity_linker = GitHubIdentityLinker(
                client_id=settings.github_client_id,
                client_secret=settings.github_client_secret,
                redirect_uri=f"{settings.public_url}/auth/github/link/callback",
                session_key=settings.session_encryption_key,
            )
        else:
            logger.warning(
                "GitHub user-authorization is not fully configured; a Clerk "
                "user cannot prove a GitHub identity and no installation can "
                "be bound")

    # The bridge from a completed Clerk onboarding into the EXISTING GitHub
    # dashboard session. Built after the session manager, and only when there
    # is one -- without it there is no dashboard session to establish, and
    # inventing a second session system would be the wrong answer.
    if (session_manager is not None and repository_service is not None
            and settings.session_encryption_key):
        from agent.api.dashboard_bridge import DashboardSessionBridge

        dashboard_bridge = DashboardSessionBridge(
            session_manager=session_manager,
            session_key=settings.session_encryption_key,
            repository_service=repository_service,
            environment=settings.metadata_review_environment,
        )
        logger.info("dashboard session bridge enabled")
    elif store_pool is not None:
        logger.warning(
            "the dashboard session bridge is NOT configured; an onboarded "
            "tenant cannot enter the dashboard")

    # Polar billing. Built on the same rule as Clerk above: absent
    # configuration is a supported deployment and leaves the routes answering
    # 503, but BROKEN configuration stops the process here rather than failing
    # on the first customer who reaches checkout.
    billing_service = None
    if store_pool is not None:
        from agent.billing.config import PolarConfigurationError, PolarSettings

        try:
            polar_settings = PolarSettings.from_environ(environ)
        except PolarConfigurationError as exc:
            raise SettingsError(str(exc)) from None
        if polar_settings is None:
            logger.warning(
                "Polar billing is not configured; /api/billing routes are "
                "served but answer 503")
        else:
            from agent.billing.client import PolarClient
            from agent.billing.service import BillingService

            if polar_settings.is_sandbox:
                # Said out loud at boot. A production deployment pointed at the
                # sandbox takes real sign-ups and charges nobody, and the only
                # visible symptom is that money never arrives.
                logger.warning("polar billing is using the SANDBOX environment")
            billing_service = BillingService(
                polar_settings,
                PolarClient(polar_settings,
                            timeout=settings.request_timeout_seconds),
                app_url=settings.dashboard_url or "")
            logger.info("polar billing enabled (%s)", polar_settings.server)

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
        session_manager=session_manager,
        auth_routes=auth_routes,
        clerk_verifier=clerk_verifier,
        installation_binder=installation_binder,
        identity_linker=identity_linker,
        repository_service=repository_service,
        dashboard_bridge=dashboard_bridge,
        billing_service=billing_service,
        secure_cookies=settings.secure_cookies,
        # Where a GitHub round trip returns the browser to, and the API origin
        # the customer's CI will submit manifests to. Both come from
        # configuration; neither is derived from a request.
        app_url=settings.dashboard_url or "",
        api_url=settings.public_url or "",
    )
    app.state.job_queue = jobs
    return app


def main(environ=None, *, run_server=None) -> None:
    configure_logging()
    try:
        settings = load_settings(environ)
        app = build_application(settings, environ=environ)
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
