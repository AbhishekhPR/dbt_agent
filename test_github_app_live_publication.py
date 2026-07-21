import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import Mock


STATELESS_TOKEN = "ghs_1234567890_eyJhbGciOiJSUzI1NiJ9.payload_signature.with.periods"


class _Response(io.BytesIO):
    def __init__(self, status, value, *, headers=None):
        content = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        super().__init__(content)
        self.status = status
        self.headers = headers or {}

    def json(self):
        raise AssertionError("raw repository responses must not be JSON-decoded")


def _webhook_body():
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
                "head": {"sha": "head-sha"},
                "base": {"sha": "base-sha"},
            },
            "sender": {"login": "octocat"},
        },
        separators=(",", ":"),
    ).encode("utf-8")


class GitHubAppLivePublicationTests(unittest.TestCase):
    def test_opened_pr_publishes_one_comment_and_one_neutral_check(self):
        from agent.github_app.adapter import GitHubAppAdapter
        from agent.github_app.client import GitHubClient
        from agent.github_app.jobs import WebhookJob
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.service import WebhookProcessingService
        from agent.github_app.storage import RepositoryStorage

        requests = []
        comments = []
        checks = []
        config_content = (
            b"manifest_path: target/manifest.json\nmode: warn\nenabled: true\n"
        )
        manifest_content = b'{"nodes":{}}'

        def transport(request):
            method = request.get_method()
            path = urlsplit(request.full_url).path
            authorization = request.get_header("Authorization")
            accept = request.get_header("Accept")
            requests.append((method, path, authorization, accept))
            if path == "/app/installations/9/access_tokens":
                self.assertEqual(authorization, "Bearer app-jwt")
                return _Response(
                    201,
                    {
                        "token": STATELESS_TOKEN,
                        "permissions": {
                            "contents": "read",
                            "issues": "write",
                            "checks": "write",
                        },
                    },
                )
            self.assertEqual(authorization, f"Bearer {STATELESS_TOKEN}")
            if path.endswith("/contents/relium.yml"):
                self.assertEqual(accept, "application/vnd.github.raw+json")
                return _Response(
                    200,
                    config_content,
                    headers={
                        "Content-Type": "text/plain",
                        "X-GitHub-Media-Type": "github.v3; format=json",
                    },
                )
            if path.endswith("/contents/target/manifest.json"):
                self.assertEqual(accept, "application/vnd.github.raw+json")
                return _Response(
                    200,
                    manifest_content,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Media-Type": "github.v3; format=json",
                    },
                )
            if path.endswith("/compare/base-sha...head-sha"):
                return _Response(200, {"files": [{"filename": "models/orders.sql"}]})
            if path.endswith("/issues/4/comments") and method == "GET":
                return _Response(200, [])
            if path.endswith("/issues/4/comments") and method == "POST":
                payload = json.loads(request.data)
                comments.append(payload)
                return _Response(201, {"id": 101, "body": payload["body"]})
            if path.endswith("/check-runs") and method == "POST":
                payload = json.loads(request.data)
                checks.append(payload)
                return _Response(201, {"id": 202, **payload})
            self.fail(f"Unexpected fake GitHub request: {method} {path}")

        reviewer = Mock(
            return_value={
                "decision": "BLOCK",
                "rendered": {"markdown": "## Relium review\n\nBlocked safely."},
            }
        )
        logger = Mock()
        body = _webhook_body()
        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            runner = PullRequestReviewRunner(
                storage=storage, reviewer=reviewer
            )
            adapter = GitHubAppAdapter(
                webhook_secret="webhook-secret",
                app_id=123,
                private_key="private-key-secret",
                runner=runner,
                client_factory=lambda: GitHubClient(transport=transport),
                jwt_factory=Mock(return_value="app-jwt"),
            )
            service = WebhookProcessingService(adapter, logger=logger)
            job = WebhookJob(
                delivery_id="live-delivery-1",
                event_name="pull_request",
                raw_body=body,
                received_at=1.0,
            )
            result = service.process(job)
            duplicate = service.process(job)
            delivery_state = (
                Path(root) / "12" / "deliveries" / "live-delivery-1"
            ).read_text(encoding="utf-8")

        self.assertEqual(result.status, "reviewed")
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(delivery_state, "complete\n")
        self.assertEqual(len(comments), 1)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["head_sha"], "head-sha")
        self.assertEqual(checks[0]["conclusion"], "neutral")
        self.assertIn("relium-github-app-review", comments[0]["body"])
        reviewer.assert_called_once()
        repository_file_requests = [
            request for request in requests if "/contents/" in request[1]
        ]
        self.assertEqual(len(repository_file_requests), 2)
        self.assertTrue(
            all(
                accept == "application/vnd.github.raw+json"
                for _, _, _, accept in repository_file_requests
            )
        )
        self.assertEqual(
            sum(
                path.endswith("/issues/4/comments") and method == "POST"
                for method, path, _, _ in requests
            ),
            1,
        )
        self.assertEqual(
            sum(
                path.endswith("/check-runs") and method == "POST"
                for method, path, _, _ in requests
            ),
            1,
        )
        rendered_logs = repr(logger.mock_calls)
        self.assertNotIn("invalid_response", rendered_logs)
        for secret in (
            STATELESS_TOKEN,
            "app-jwt",
            "webhook-secret",
            "private-key-secret",
            body.decode("utf-8"),
            config_content.decode("utf-8"),
            manifest_content.decode("utf-8"),
        ):
            self.assertNotIn(secret, rendered_logs)


if __name__ == "__main__":
    unittest.main()
