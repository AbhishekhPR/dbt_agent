import json
from collections.abc import Mapping
from typing import Any

from agent.github_app.models import (
    InstallationEvent, PullRequestEvent, Repository,
)


SUPPORTED_PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize"})

#: Installation lifecycle actions Relium acts on.
#:
#: `new_permissions_accepted` is deliberately absent: it changes what the App
#: may do, not what the installation IS, and nothing in this phase depends on
#: it. Unlisted actions are ignored rather than guessed at.
SUPPORTED_INSTALLATION_ACTIONS = frozenset({
    "created", "deleted", "suspend", "unsuspend",
})

#: `installation_repositories` actions. Recorded only because they change
#: `repository_selection`; repository CONTENTS are Phase 3 and are not read.
SUPPORTED_INSTALLATION_REPOSITORY_ACTIONS = frozenset({"added", "removed"})


class WebhookPayloadError(ValueError):
    """Raised when a supported webhook has an invalid payload."""


def parse_webhook(*, event_name: str, delivery_id: str, body):
    """Parse a supported delivery, or return None for one we ignore.

    Returns a PullRequestEvent or an InstallationEvent. Anything else — and any
    unsupported action on a supported event — returns None, which the HTTP
    layer answers as an explicit `ignored`, rather than being partially
    interpreted.
    """
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        raise WebhookPayloadError("Webhook delivery id is required.")
    if event_name in ("installation", "installation_repositories"):
        return _parse_installation(event_name=event_name,
                                   delivery_id=delivery_id, body=body)
    if event_name != "pull_request":
        return None

    payload = _decode_payload(body)
    action = _required_string(payload, "action")
    if action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        return None

    repository = Repository(
        id=_required_integer(payload, "repository.id"),
        owner=_required_string(payload, "repository.owner.login"),
        name=_required_string(payload, "repository.name"),
        full_name=_required_string(payload, "repository.full_name"),
    )
    return PullRequestEvent(
        delivery_id=delivery_id,
        action=action,
        installation_id=_required_integer(payload, "installation.id"),
        repository=repository,
        pull_number=_required_integer(payload, "pull_request.number"),
        head_sha=_required_string(payload, "pull_request.head.sha"),
        base_sha=_required_string(payload, "pull_request.base.sha"),
        sender_login=_required_string(payload, "sender.login"),
    )


def _parse_installation(*, event_name, delivery_id, body):
    """Read an installation delivery into facts, or ignore it.

    Every field is validated. A payload that does not carry a usable
    installation id and account identity is rejected rather than stored with
    holes, because a partially-known installation is exactly the kind of row
    that later gets matched on a name.
    """
    payload = _decode_payload(body)
    action = _required_string(payload, "action")

    if event_name == "installation":
        if action not in SUPPORTED_INSTALLATION_ACTIONS:
            return None
    else:
        if action not in SUPPORTED_INSTALLATION_REPOSITORY_ACTIONS:
            return None

    installation = payload.get("installation")
    if not isinstance(installation, Mapping):
        raise WebhookPayloadError("Missing required webhook field: installation")

    account = installation.get("account")
    if not isinstance(account, Mapping):
        raise WebhookPayloadError("Missing required webhook field: installation.account")

    account_type = account.get("type")
    if account_type not in ("User", "Organization"):
        raise WebhookPayloadError(
            "Webhook field installation.account.type must be User or Organization")

    selection = installation.get("repository_selection")
    if selection is not None and selection not in ("all", "selected"):
        selection = None

    app_id = installation.get("app_id")
    if isinstance(app_id, bool) or not isinstance(app_id, int):
        app_id = None

    sender = payload.get("sender")
    sender_login = sender.get("login") if isinstance(sender, Mapping) else None
    if not isinstance(sender_login, str) or not sender_login:
        sender_login = None

    return InstallationEvent(
        delivery_id=delivery_id,
        action=action,
        installation_id=_required_integer(payload, "installation.id"),
        app_id=app_id,
        account_id=_required_integer(payload, "installation.account.id"),
        account_login=_required_string(payload, "installation.account.login"),
        account_type=account_type,
        repository_selection=selection,
        sender_login=sender_login,
    )


def _decode_payload(body) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        return body
    if not isinstance(body, (bytes, bytearray)):
        raise WebhookPayloadError("Webhook body must be JSON bytes or an object.")
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookPayloadError("Webhook body must contain valid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise WebhookPayloadError("Webhook JSON must be an object.")
    return payload


def _required_value(payload: Mapping[str, Any], path: str):
    value: Any = payload
    for component in path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            raise WebhookPayloadError(f"Missing required webhook field: {path}")
        value = value[component]
    return value


def _required_string(payload: Mapping[str, Any], path: str) -> str:
    value = _required_value(payload, path)
    if not isinstance(value, str) or not value.strip():
        raise WebhookPayloadError(f"Webhook field must be a non-empty string: {path}")
    return value


def _required_integer(payload: Mapping[str, Any], path: str) -> int:
    value = _required_value(payload, path)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WebhookPayloadError(f"Webhook field must be a positive integer: {path}")
    return value
