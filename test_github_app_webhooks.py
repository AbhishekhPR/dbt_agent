import json
import unittest


class GitHubAppWebhookTests(unittest.TestCase):
    def _payload(self, *, action="opened"):
        return {
            "action": action,
            "installation": {"id": 9876},
            "repository": {
                "id": 123,
                "name": "analytics",
                "full_name": "acme/analytics",
                "owner": {"login": "acme"},
            },
            "pull_request": {
                "number": 42,
                "head": {"sha": "head-sha"},
                "base": {"sha": "base-sha"},
            },
            "sender": {"login": "octocat"},
        }

    def test_parses_pull_request_event_from_json_bytes(self):
        from agent.github_app.models import PullRequestEvent
        from agent.github_app.webhooks import parse_webhook

        event = parse_webhook(
            event_name="pull_request",
            delivery_id="delivery-1",
            body=json.dumps(self._payload()).encode("utf-8"),
        )

        self.assertIsInstance(event, PullRequestEvent)
        self.assertEqual(event.delivery_id, "delivery-1")
        self.assertEqual(event.action, "opened")
        self.assertEqual(event.installation_id, 9876)
        self.assertEqual(event.repository.id, 123)
        self.assertEqual(event.repository.owner, "acme")
        self.assertEqual(event.repository.name, "analytics")
        self.assertEqual(event.repository.full_name, "acme/analytics")
        self.assertEqual(event.pull_number, 42)
        self.assertEqual(event.head_sha, "head-sha")
        self.assertEqual(event.base_sha, "base-sha")
        self.assertEqual(event.sender_login, "octocat")

    def test_event_models_are_immutable(self):
        from dataclasses import FrozenInstanceError

        from agent.github_app.webhooks import parse_webhook

        event = parse_webhook(
            event_name="pull_request",
            delivery_id="delivery-1",
            body=self._payload(),
        )

        with self.assertRaises(FrozenInstanceError):
            event.pull_number = 99

    def test_supported_pull_request_actions_are_parsed(self):
        from agent.github_app.webhooks import parse_webhook

        for action in ("opened", "reopened", "synchronize"):
            with self.subTest(action=action):
                event = parse_webhook(
                    event_name="pull_request",
                    delivery_id=f"delivery-{action}",
                    body=self._payload(action=action),
                )
                self.assertEqual(event.action, action)

    def test_unrelated_events_and_actions_are_ignored(self):
        from agent.github_app.webhooks import parse_webhook

        self.assertIsNone(
            parse_webhook(
                event_name="issues",
                delivery_id="delivery-1",
                body=self._payload(),
            )
        )
        self.assertIsNone(
            parse_webhook(
                event_name="pull_request",
                delivery_id="delivery-2",
                body=self._payload(action="closed"),
            )
        )

    def test_invalid_json_and_missing_required_fields_are_rejected(self):
        from agent.github_app.webhooks import WebhookPayloadError, parse_webhook

        with self.assertRaisesRegex(WebhookPayloadError, "valid JSON"):
            parse_webhook(
                event_name="pull_request",
                delivery_id="delivery-1",
                body=b"not-json",
            )

        payload = self._payload()
        del payload["pull_request"]["head"]["sha"]
        with self.assertRaisesRegex(WebhookPayloadError, "pull_request.head.sha"):
            parse_webhook(
                event_name="pull_request",
                delivery_id="delivery-2",
                body=payload,
            )

    def test_empty_delivery_id_is_rejected(self):
        from agent.github_app.webhooks import WebhookPayloadError, parse_webhook

        with self.assertRaisesRegex(WebhookPayloadError, "delivery"):
            parse_webhook(
                event_name="pull_request",
                delivery_id="",
                body=self._payload(),
            )


if __name__ == "__main__":
    unittest.main()
