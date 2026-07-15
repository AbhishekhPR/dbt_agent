import json
from collections.abc import Mapping
from typing import Any

from agent.github_app.models import PullRequestEvent, Repository


SUPPORTED_PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize"})


class WebhookPayloadError(ValueError):
    """Raised when a supported webhook has an invalid payload."""


def parse_webhook(*, event_name: str, delivery_id: str, body) -> PullRequestEvent | None:
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        raise WebhookPayloadError("Webhook delivery id is required.")
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
