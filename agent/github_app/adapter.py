from agent.github_app.auth import create_app_jwt, get_installation_token
from agent.github_app.client import GitHubClient
from agent.github_app.signatures import verify_webhook_signature
from agent.github_app.webhooks import parse_webhook


class GitHubAppAdapter:
    """Composition root for a framework-neutral GitHub webhook endpoint."""

    def __init__(
        self,
        *,
        webhook_secret,
        app_id,
        private_key,
        runner,
        client_factory=GitHubClient,
        jwt_factory=create_app_jwt,
    ):
        self.webhook_secret = webhook_secret
        self.app_id = app_id
        self.private_key = private_key
        self.runner = runner
        self.client_factory = client_factory
        self.jwt_factory = jwt_factory

    def handle(self, *, event_name, delivery_id, signature, body):
        if not verify_webhook_signature(
            secret=self.webhook_secret, body=body, signature_header=signature
        ):
            raise PermissionError("Webhook signature is invalid.")
        event = parse_webhook(event_name=event_name, delivery_id=delivery_id, body=body)
        if event is None:
            return {"status": "ignored", "delivery_id": delivery_id}
        app_jwt = self.jwt_factory(self.app_id, self.private_key)
        app_client = self.client_factory()
        token = get_installation_token(app_client, event.installation_id, app_jwt)
        return self.runner.run(event, app_client.with_token(token))
