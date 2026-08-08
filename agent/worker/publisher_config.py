"""Build the worker's outbound publisher from configuration.

The worker owns two outbound jobs - republishing a recomputed decision, and
submitting a reviewer's request-changes review - and both need a GitHub
client. Nothing was building one: ``configure_publisher`` existed but only
the E2E harness ever called it, so in a real deployment both handlers ran,
found no publisher, and correctly recorded that nothing was published. The
work was durable and the record was honest, but it never left the building.

This is that missing wiring, and nothing more. When the App is not
configured the factory returns None and the previous behaviour is unchanged:
the handlers record 'no publisher' rather than inventing a delivery.

Credentials are read from the environment the process was given. Nothing here
logs, copies or returns a credential.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("relium.worker.publisher")


def _read_private_key(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def build_publisher_factory(environ=None):
    """Return a factory(organization_id, repository_id, environment) -> publisher.

    Returns None when the GitHub App is not configured, which leaves the
    handlers on their existing 'nothing was attempted' path.
    """
    values = os.environ if environ is None else environ
    app_id = (values.get("RELIUM_GITHUB_APP_ID") or "").strip()
    key_path = (values.get("RELIUM_GITHUB_PRIVATE_KEY_PATH") or "").strip()
    if not app_id or not key_path:
        logger.info("worker_publisher_not_configured",
                    extra={"operation": "publisher_config"})
        return None
    if not os.path.isfile(key_path):
        # Fail loudly rather than silently degrading to "nothing published".
        raise RuntimeError(
            "RELIUM_GITHUB_PRIVATE_KEY_PATH does not name a readable file")

    configured_installation = (values.get("RELIUM_GITHUB_INSTALLATION_ID")
                               or values.get("RELIUM_E2E_INSTALLATION_ID") or "").strip()
    slack_url = (values.get("RELIUM_SLACK_WEBHOOK_URL") or "").strip()
    notify_warn = str(values.get("RELIUM_SLACK_NOTIFY_WARN", "")).lower() == "true"

    def factory(*, organization_id, repository_id, environment):
        from agent.github_app.auth import create_app_jwt, get_installation_token
        from agent.github_app.client import GitHubClient
        from agent.github_app.slack import SlackPublicationSink
        from agent.metadata_evidence.publishers import GitHubSlackPublisher

        jwt = create_app_jwt(app_id, _read_private_key(key_path))
        anonymous = GitHubClient()

        installation_id = configured_installation
        if not installation_id:
            # Resolve the installation from the repository itself, so a
            # multi-tenant worker needs no per-tenant configuration.
            installation = anonymous.get_repository_installation(
                organization_id, repository_id, jwt)
            installation_id = str(installation.get("id") or "")
        if not installation_id:
            raise RuntimeError(
                f"no GitHub App installation found for "
                f"{organization_id}/{repository_id}")

        # A fresh short-lived installation token per job. Never cached, never
        # logged, and it goes out of scope with the publisher.
        token = get_installation_token(anonymous, int(installation_id), jwt)
        client = GitHubClient(token)

        slack = None
        if slack_url:
            slack = SlackPublicationSink(slack_url, notify_warn=notify_warn)

        return GitHubSlackPublisher(
            client, owner=organization_id, repository=repository_id,
            expected_app_id=int(app_id), slack_publisher=slack)

    return factory
