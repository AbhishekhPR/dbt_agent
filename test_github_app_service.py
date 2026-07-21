import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock


def _body():
    return json.dumps(
        {
            "action": "opened",
            "installation": {"id": 9},
            "repository": {
                "id": 12,
                "name": "analytics",
                "full_name": "acme/analytics",
                "owner": {"login": "acme"},
            },
            "pull_request": {
                "number": 4,
                "head": {"sha": "head"},
                "base": {"sha": "base"},
            },
            "sender": {"login": "octocat"},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _job(*, attempt=0):
    from agent.github_app.jobs import WebhookJob

    return WebhookJob(
        delivery_id="delivery-1",
        event_name="pull_request",
        raw_body=_body(),
        received_at=1.0,
        attempt=attempt,
    )


class GitHubAppServiceTests(unittest.TestCase):
    def test_service_invokes_only_verified_adapter_path(self):
        from agent.github_app.service import WebhookProcessingService

        adapter = Mock()
        adapter.handle_verified.return_value = {"status": "reviewed"}
        clock = Mock(side_effect=[10.0, 10.25])
        result = WebhookProcessingService(
            adapter, clock=clock, logger=Mock()
        ).process(_job(attempt=2))
        adapter.handle_verified.assert_called_once_with(
            event_name="pull_request",
            delivery_id="delivery-1",
            body=_body(),
        )
        adapter.handle.assert_not_called()
        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.delivery_id, "delivery-1")
        self.assertEqual(result.event_name, "pull_request")
        self.assertEqual(result.attempt, 2)
        self.assertEqual(result.duration_seconds, 0.25)

    def test_unsupported_event_result_is_structured(self):
        from agent.github_app.service import WebhookProcessingService

        adapter = Mock()
        adapter.handle_verified.return_value = {
            "status": "ignored",
            "delivery_id": "delivery-1",
        }
        result = WebhookProcessingService(
            adapter, clock=Mock(side_effect=[1.0, 1.0]), logger=Mock()
        ).process(_job())
        self.assertEqual(result.status, "ignored")
        self.assertIsNone(result.error_category)

    def test_service_logs_only_safe_error_category(self):
        from agent.github_app.service import WebhookProcessingService

        adapter = Mock()
        adapter.handle_verified.side_effect = RuntimeError(
            "token-secret raw-body-secret"
        )
        logger = Mock()
        with self.assertRaises(RuntimeError):
            WebhookProcessingService(
                adapter, clock=Mock(side_effect=[1.0, 1.1]), logger=logger
            ).process(_job())
        logged = logger.error.call_args
        self.assertEqual(logged.args[0], "webhook_processing_failed")
        self.assertNotIn("token-secret", str(logged))
        self.assertNotIn("raw-body-secret", str(logged))

    def test_service_logs_safe_github_operation_diagnostics(self):
        from agent.github_app.client import GitHubAPIError
        from agent.github_app.service import WebhookProcessingService

        error = GitHubAPIError(
            "safe failure",
            status_code=403,
            operation="get_repository_file",
            http_method="GET",
            route_template="/repos/{owner}/{repo}/contents/{path}",
            github_request_id="SAFE-REQUEST-ID",
            accepted_github_permissions="contents=read",
            message_category="permission",
            response_representation="raw",
        )
        adapter = Mock()
        adapter.handle_verified.side_effect = error
        logger = Mock()
        with self.assertRaises(GitHubAPIError):
            WebhookProcessingService(
                adapter, clock=Mock(side_effect=[1.0, 1.1]), logger=logger
            ).process(_job(attempt=2))

        extra = logger.error.call_args.kwargs["extra"]
        self.assertEqual(extra["operation"], "get_repository_file")
        self.assertEqual(extra["http_method"], "GET")
        self.assertEqual(extra["http_status"], 403)
        self.assertEqual(extra["github_request_id"], "SAFE-REQUEST-ID")
        self.assertEqual(extra["accepted_github_permissions"], "contents=read")
        self.assertEqual(extra["github_message_category"], "permission")
        self.assertEqual(extra["response_representation"], "raw")
        self.assertFalse(extra["retryable"])
        self.assertEqual(extra["attempt"], 2)

    def test_verified_adapter_reuses_fake_auth_client_and_runner(self):
        from agent.github_app.adapter import GitHubAppAdapter
        from agent.github_app.service import WebhookProcessingService

        installation_client = Mock()
        installation_client.create_installation_access_token.return_value = {
            "token": "installation-token"
        }
        scoped_client = Mock()
        installation_client.with_token.return_value = scoped_client
        runner = Mock()
        runner.run.return_value = {"status": "reviewed"}
        adapter = GitHubAppAdapter(
            webhook_secret="secret",
            app_id=123,
            private_key="private-key",
            runner=runner,
            client_factory=Mock(return_value=installation_client),
            jwt_factory=Mock(return_value="app-jwt"),
        )
        result = WebhookProcessingService(adapter, logger=Mock()).process(_job())
        self.assertEqual(result.status, "reviewed")
        installation_client.create_installation_access_token.assert_called_once_with(
            9, "app-jwt"
        )
        installation_client.with_token.assert_called_once_with("installation-token")
        runner.run.assert_called_once_with(
            runner.run.call_args.args[0], scoped_client, expected_app_id=123
        )

    def test_existing_adapter_handle_still_verifies_signature(self):
        from agent.github_app.adapter import GitHubAppAdapter

        body = _body()
        signature = "sha256=" + hmac.new(
            b"secret", body, hashlib.sha256
        ).hexdigest()
        adapter = GitHubAppAdapter(
            webhook_secret="secret",
            app_id=123,
            private_key="private-key",
            runner=Mock(),
        )
        adapter.handle_verified = Mock(return_value={"status": "ignored"})
        self.assertEqual(
            adapter.handle(
                event_name="ping",
                delivery_id="delivery-1",
                signature=signature,
                body=body,
            ),
            {"status": "ignored"},
        )
        adapter.handle_verified.assert_called_once_with(
            event_name="ping", delivery_id="delivery-1", body=body
        )


if __name__ == "__main__":
    unittest.main()
