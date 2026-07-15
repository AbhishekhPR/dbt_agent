import hashlib
import hmac
import json
import tempfile
import unittest
from unittest.mock import Mock

from agent.github_app.models import PullRequestEvent, Repository


def _event(delivery="delivery-1"):
    return PullRequestEvent(
        delivery_id=delivery,
        action="opened",
        installation_id=9,
        repository=Repository(id=12, owner="acme", name="analytics", full_name="acme/analytics"),
        pull_number=4,
        head_sha="head",
        base_sha="base",
        sender_login="octocat",
    )


class FakeClient:
    def __init__(self):
        self.comments = []
        self.checks = []

    def get_file(self, owner, repository, path, ref):
        if path == "relium.yml":
            return b"version: 1\nmanifest_path: build/manifest.json\n"
        return json.dumps({"nodes": {}}).encode()

    def compare_files(self, owner, repository, base, head):
        return ["models/orders.sql"]

    def list_issue_comments(self, owner, repository, pull_number):
        return self.comments

    def create_issue_comment(self, owner, repository, pull_number, body):
        value = {"id": 1, "body": body}
        self.comments.append(value)
        return value

    def update_issue_comment(self, owner, repository, comment_id, body):
        return {"id": comment_id, "body": body}

    def create_check_run(self, owner, repository, payload):
        self.checks.append(payload)
        return {"id": 2}


class GitHubAppRunnerTests(unittest.TestCase):
    def test_runner_reuses_review_manifest_change_contract_and_publishes(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value={
            "decision": "ALLOW", "rendered": {"markdown": "## review"}
        })
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer)
            response = runner.run(_event(), client)

        self.assertEqual(response["status"], "reviewed")
        reviewer.assert_called_once_with(
            manifest={"nodes": {}},
            changed_files=["models/orders.sql"],
            deployment_id="github:12:head",
        )
        self.assertEqual(client.checks[0]["conclusion"], "success")
        self.assertIn("relium-github-app-review", client.comments[0]["body"])

    def test_duplicate_delivery_does_not_review_or_publish(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value={"decision": "ALLOW", "rendered": {"markdown": "ok"}})
        with tempfile.TemporaryDirectory() as tmp:
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer)
            client = FakeClient()
            runner.run(_event(), client)
            response = runner.run(_event(), client)
        self.assertEqual(response["status"], "duplicate")
        reviewer.assert_called_once()

    def test_disabled_repository_does_not_load_manifest(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = Mock()
        client.get_file.return_value = b"version: 1\nenabled: false\n"
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(storage=RepositoryStorage(tmp)).run(_event(), client)
        self.assertEqual(response["status"], "disabled")
        client.get_file.assert_called_once()

    def test_publication_failure_releases_delivery_for_retry(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value={"decision": "ALLOW", "rendered": {"markdown": "ok"}})
        with tempfile.TemporaryDirectory() as tmp:
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer)
            client = FakeClient()
            client.create_check_run = Mock(side_effect=RuntimeError("publication failed"))
            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                runner.run(_event(), client)
            client.create_check_run = Mock(return_value={"id": 2})
            self.assertEqual(runner.run(_event(), client)["status"], "reviewed")

    def test_missing_manifest_publishes_actionable_neutral_result(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = FakeClient()
        original = client.get_file
        client.get_file = lambda owner, repository, path, ref: None if path == "build/manifest.json" else original(owner, repository, path, ref)
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(storage=RepositoryStorage(tmp)).run(_event(), client)
        self.assertEqual(response["status"], "missing_manifest")
        self.assertEqual(client.checks[0]["conclusion"], "neutral")
        self.assertIn("build/manifest.json", client.comments[0]["body"])

    def test_no_changed_models_publishes_neutral_skipped_result(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(side_effect=ValueError("At least one changed model is required."))
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            response = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer).run(_event(), client)
        self.assertEqual(response["status"], "skipped")
        self.assertEqual(client.checks[0]["conclusion"], "neutral")

    def test_adapter_validates_signature_authenticates_and_injects_client(self):
        from agent.github_app.adapter import GitHubAppAdapter

        body = json.dumps({
            "action": "opened", "installation": {"id": 9},
            "repository": {"id": 12, "name": "analytics", "full_name": "acme/analytics", "owner": {"login": "acme"}},
            "pull_request": {"number": 4, "head": {"sha": "head"}, "base": {"sha": "base"}},
            "sender": {"login": "octocat"},
        }).encode()
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        installation_client = Mock()
        installation_client.create_installation_access_token.return_value = {"token": "token"}
        scoped_client = Mock()
        installation_client.with_token.return_value = scoped_client
        runner = Mock()
        runner.run.return_value = {"status": "reviewed"}
        adapter = GitHubAppAdapter(
            webhook_secret="secret", app_id=1, private_key="key", runner=runner,
            client_factory=Mock(return_value=installation_client),
            jwt_factory=Mock(return_value="jwt"),
        )
        response = adapter.handle(event_name="pull_request", delivery_id="d-1", signature=signature, body=body)
        self.assertEqual(response["status"], "reviewed")
        installation_client.create_installation_access_token.assert_called_once_with(9, "jwt")
        installation_client.with_token.assert_called_once_with("token")
        runner.run.assert_called_once_with(runner.run.call_args.args[0], scoped_client)

    def test_adapter_rejects_bad_signature_before_authentication(self):
        from agent.github_app.adapter import GitHubAppAdapter

        factory = Mock()
        adapter = GitHubAppAdapter(
            webhook_secret="secret", app_id=1, private_key="key", runner=Mock(), client_factory=factory
        )
        with self.assertRaises(PermissionError):
            adapter.handle(event_name="pull_request", delivery_id="d-1", signature="bad", body=b"{}")
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
