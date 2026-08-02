import json
import hashlib
import hmac
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path


def _event(delivery_id="delivery-hardening"):
    from agent.github_app.models import PullRequestEvent, Repository

    return PullRequestEvent(
        delivery_id=delivery_id,
        action="opened",
        installation_id=9,
        repository=Repository(
            id=12,
            owner="acme",
            name="analytics",
            full_name="acme/analytics",
        ),
        pull_number=4,
        head_sha="head-sha",
        base_sha="base-sha",
        sender_login="octocat",
    )


def _manifest(sql):
    return {
        "metadata": {"project_name": "pilot"},
        "nodes": {
            "model.pilot.revenue_refunds": {
                "resource_type": "model",
                "unique_id": "model.pilot.revenue_refunds",
                "name": "revenue_refunds",
                "original_file_path": "models/risky_revenue_refunds.sql",
                "raw_code": sql,
                "depends_on": {"nodes": []},
                "columns": {"net_revenue": {"name": "net_revenue"}},
            }
        },
    }


class _Client:
    def __init__(self, base_manifest, head_manifest, comments=None):
        self.base_manifest = base_manifest
        self.head_manifest = head_manifest
        self.comments = list(comments or [])
        self.checks = []
        self.comment_calls = []

    def get_file(self, owner, repository, path, ref):
        if path == "relium.yml":
            return b"manifest_path: target/manifest.json\n"
        manifest = self.base_manifest if ref == "base-sha" else self.head_manifest
        return json.dumps(manifest).encode("utf-8")

    def compare_files(self, owner, repository, base, head):
        return ["models/risky_revenue_refunds.sql"]

    def list_issue_comments(self, owner, repository, pull_number):
        return self.comments

    def create_issue_comment(self, owner, repository, pull_number, body):
        self.comment_calls.append(("create", body))
        comment = {
            "id": 9,
            "body": body,
            "performed_via_github_app": {"id": 123},
        }
        self.comments.append(comment)
        return comment

    def update_issue_comment(self, owner, repository, comment_id, body):
        self.comment_calls.append(("update", comment_id, body))
        return {"id": comment_id, "body": body, "performed_via_github_app": {"id": 123}}

    def create_check_run(self, owner, repository, payload):
        self.checks.append(payload)
        return {"id": 10}


class GitHubAppSemanticTrustTests(unittest.TestCase):
    def test_pilot_refund_removal_fixture_is_not_allow(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        root = Path(__file__).parent / "demo" / "github_app_pilot"
        base = json.loads((root / "previous_manifest.json").read_text(encoding="utf-8"))
        head = json.loads((root / "current_manifest.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp)
            ).run(_event("pilot-refund-removal"), _Client(base, head), expected_app_id=123)

        self.assertIn(response["result"]["decision"], {"WARN", "BLOCK"})

    def test_safe_equivalent_manifest_change_remains_allow(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        base = _manifest("select gross_revenue - refunds as net_revenue from orders")
        head = _manifest("-- formatting only\nSELECT gross_revenue - refunds AS net_revenue FROM orders")
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp)
            ).run(_event("safe-equivalent"), _Client(base, head), expected_app_id=123)

        self.assertEqual(response["result"]["decision"], "ALLOW")

    def test_safe_cte_rename_remains_allow(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        base = _manifest("with source_rows as (select x from orders) select x from source_rows")
        head = _manifest("with order_rows as (select x from orders) select x from order_rows")
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp)
            ).run(_event("safe-cte-rename"), _Client(base, head), expected_app_id=123)

        self.assertEqual(response["result"]["decision"], "ALLOW")

    def test_runner_fetches_base_and_head_and_material_refund_change_is_not_allow(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        base = _manifest("select gross_revenue - refunds as net_revenue from orders")
        head = _manifest("select gross_revenue as net_revenue from orders")
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp)
            ).run(_event(), _Client(base, head), expected_app_id=123)

        self.assertIn(response["result"]["decision"], {"WARN", "BLOCK"})
        metadata = response["result"]["incident"]["metadata"]
        self.assertTrue(metadata["semantic_comparison_evaluated"])
        self.assertEqual(metadata["manifest_source"]["base"], "github")
        self.assertEqual(metadata["manifest_source"]["head"], "github")
        self.assertEqual(metadata["base_sha"], "base-sha")
        self.assertEqual(metadata["head_sha"], "head-sha")

    def test_missing_base_manifest_is_explicitly_reduced_confidence(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = _Client(
            _manifest("select gross_revenue - refunds as net_revenue from orders"),
            _manifest("select gross_revenue as net_revenue from orders"),
        )
        original_get_file = client.get_file

        def missing_base(owner, repository, path, ref):
            if path != "relium.yml" and ref == "base-sha":
                from agent.github_app.client import GitHubNotFoundError

                raise GitHubNotFoundError("missing")
            return original_get_file(owner, repository, path, ref)

        client.get_file = missing_base
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp)
            ).run(_event("missing-base"), client, expected_app_id=123)

        metadata = response["result"]["incident"]["metadata"]
        self.assertFalse(metadata["semantic_comparison_evaluated"])
        self.assertEqual(metadata["semantic_comparison_status"], "unavailable")
        self.assertIn(response["result"]["decision"], {"ALLOW", "WARN"})


class DurableWebhookTests(unittest.TestCase):
    def test_processing_lease_expires_back_to_recoverable_pending(self):
        from agent.github_app.jobs import WebhookJob
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            store.persist_verified_job(12, WebhookJob("processing-1", "pull_request", b"x", 1.0))
            store.claim_job(12, "processing-1", owner="worker-a", now=2.0, lease_seconds=5)
            store.mark_processing(12, "processing-1", owner="worker-a")
            recovered = store.recover_jobs(12, now=8.0)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "verified_pending")

    def test_queue_restart_recovers_pending_job(self):
        from agent.github_app.jobs import BoundedJobQueue, WebhookJob
        from agent.github_app.storage import RepositoryStorage

        processed = []
        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            store.persist_verified_job(
                12,
                WebhookJob("restart-1", "pull_request", b"payload", 1.0, repository_id=12),
            )
            queue = BoundedJobQueue(
                lambda job: processed.append(job.delivery_id),
                worker_count=1,
                capacity=2,
                job_store=store,
            )
            queue.start()
            queue.stop(timeout=2)

        self.assertEqual(processed, ["restart-1"])

    def test_http_persists_verified_job_before_202(self):
        from starlette.testclient import TestClient

        from agent.github_app.http_app import create_http_app
        from agent.github_app.storage import RepositoryStorage

        body = json.dumps(
            {
                "action": "opened",
                "installation": {"id": 9},
                "repository": {"id": 12, "name": "analytics", "full_name": "acme/analytics", "owner": {"login": "acme"}},
                "pull_request": {"number": 4, "head": {"sha": "head"}, "base": {"sha": "base"}},
                "sender": {"login": "octocat"},
            },
            separators=(",", ":"),
        ).encode()
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

        class Queue:
            is_running = True

            def start(self):
                self.is_running = True

            def stop(self, *, timeout):
                self.is_running = False

            def enqueue(self, job):
                self.assert_persisted(job)
                return True

            def assert_persisted(self, job):
                self.persisted = job

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            queue = Queue()
            app = create_http_app(
                webhook_secret="secret",
                job_queue=queue,
                job_store=store,
                max_body_bytes=10000,
                shutdown_timeout_seconds=1,
                clock=lambda: 1.0,
            )
            with TestClient(app) as client:
                response = client.post(
                    "/github/webhook",
                    content=body,
                    headers={
                        "X-Hub-Signature-256": signature,
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "durable-http-1",
                    },
                )

            self.assertEqual(response.status_code, 202)
            self.assertEqual(len(store.recover_jobs(12, now=2.0)), 1)

    def test_verified_job_is_durable_and_recoverable_after_restart(self):
        from agent.github_app.jobs import WebhookJob
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            first = RepositoryStorage(tmp)
            job = WebhookJob(
                delivery_id="durable-1",
                event_name="pull_request",
                raw_body=b"payload",
                received_at=10.0,
            )
            self.assertTrue(first.persist_verified_job(12, job))
            restarted = RepositoryStorage(tmp)
            recovered = restarted.recover_jobs(12, now=20.0)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].raw_body, b"payload")
        self.assertEqual(recovered[0].state, "verified_pending")

    def test_expired_claim_is_reclaimable_and_attempt_error_persisted(self):
        from agent.github_app.jobs import WebhookJob
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            job = WebhookJob("lease-1", "pull_request", b"x", 1.0)
            store.persist_verified_job(12, job)
            self.assertTrue(store.claim_job(12, "lease-1", owner="worker-a", now=2.0, lease_seconds=5))
            store.mark_processing(12, "lease-1", owner="worker-a")
            store.fail_job(12, "lease-1", attempt=1, last_error="timeout", retry_at=3.0)
            self.assertTrue(store.claim_job(12, "lease-1", owner="worker-b", now=4.0, lease_seconds=5))
            self.assertTrue(store.claim_job(12, "lease-1", owner="worker-c", now=20.0, lease_seconds=5))
            recovered = store.recover_jobs(12, now=20.0)

        self.assertEqual(recovered, [])

    def test_publication_journal_prevents_repeating_completed_steps(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            store.record_publication_step(12, "pub-1", "comment", {"id": 7})
            store.record_publication_step(12, "pub-1", "check", {"id": 8})
            journal = store.get_publication_journal(12, "pub-1")

        self.assertEqual(journal["comment"]["id"], 7)
        self.assertEqual(journal["check"]["id"], 8)


class GitHubCompletenessTests(unittest.TestCase):
    def test_runner_skips_incomplete_large_pr_with_actionable_neutral_result(self):
        from agent.github_app.client import ChangedFiles
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class LargeClient(_Client):
            def compare_files(self, owner, repository, base, head):
                return ChangedFiles([f"models/model_{i}.sql" for i in range(300)], complete=False)

        client = LargeClient(_manifest("select x from orders"), _manifest("select x from orders"))
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(storage=RepositoryStorage(tmp)).run(
                _event("large-pr"), client, expected_app_id=123
            )

        self.assertEqual(response["status"], "large_pr")
        self.assertEqual(response["result"]["decision"], "SKIPPED")
        self.assertEqual(client.checks[0]["conclusion"], "neutral")

    def test_compare_files_marks_a_300_file_response_incomplete(self):
        from agent.github_app.client import GitHubClient

        class Response:
            status = 200
            headers = {}

            def read(self):
                return json.dumps({"files": [{"filename": f"f{i}.sql"} for i in range(300)]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Transport:
            def __call__(self, request):
                return Response()

        result = GitHubClient(transport=Transport()).compare_files("a", "r", "b", "h")
        self.assertFalse(result.complete)
        self.assertEqual(len(result), 300)

    def test_comment_lookup_paginates_until_short_page(self):
        from agent.github_app.client import GitHubClient

        class Response:
            status = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        requests = []

        def transport(request):
            requests.append(request.full_url)
            if "page=2" in request.full_url:
                return Response([{"id": 201, "body": "owned"}])
            if "per_page=100" not in request.full_url:
                return Response([{"id": i, "body": "default-page"} for i in range(30)])
            return Response([{"id": i, "body": "other"} for i in range(100)])

        comments = GitHubClient(transport=transport).list_issue_comments("a", "r", 1)
        self.assertEqual(comments[-1]["id"], 201)
        self.assertIn("page=1", requests[0])
        self.assertIn("per_page=100", requests[0])
        self.assertEqual(len(requests), 2)

    def test_live_shaped_page_two_owned_marker_is_updated_not_duplicated(self):
        from agent.github_app.client import GitHubClient
        from agent.github_app.comments import upsert_review_comment

        class Response:
            status = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        writes = []
        marker = "<!-- relium-github-app-review -->"

        def transport(request):
            if request.method == "PATCH":
                writes.append((request.method, request.full_url))
                return Response({"id": 5159337517, "body": marker})
            if request.method == "POST":
                writes.append((request.method, request.full_url))
                return Response({"id": 5159354458, "body": marker})
            if "page=2" in request.full_url:
                return Response([
                    {
                        "id": 5159337517,
                        "body": f"{marker}\nold review",
                        "performed_via_github_app": {"id": 4456468},
                    }
                ])
            return Response([{"id": index, "body": "sentinel"} for index in range(100)])

        result = upsert_review_comment(
            GitHubClient("token", transport=transport),
            owner="AbhishekhPR",
            repository="relium-e2e-dbt",
            pull_number=15,
            body="updated review",
            expected_app_id=4456468,
        )

        self.assertEqual(result["id"], 5159337517)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], "PATCH")


class PublicationCrashTests(unittest.TestCase):
    def test_crash_before_comment_request_retries_request(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class CrashBeforeRequest(RepositoryStorage):
            crashed = False

            def record_publication_step(self, repository_id, publication_id, step, value):
                if step == "comment" and value.get("state") == "started" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash before comment request")
                return super().record_publication_step(repository_id, publication_id, step, value)

        client = _Client(_manifest("select x from orders"), _manifest("select x from orders"))
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = CrashBeforeRequest(tmp)
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            with self.assertRaises(RuntimeError):
                runner.run(_event("before-comment"), client, expected_app_id=123)
            runner.run(_event("before-comment"), client, expected_app_id=123)
        self.assertEqual(len([call for call in client.comment_calls if call[0] == "create"]), 1)

    def test_crash_after_comment_acceptance_reconciles_existing_comment(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class CrashBeforeJournalComplete(RepositoryStorage):
            crashed = False

            def record_publication_step(self, repository_id, publication_id, step, value):
                if step == "comment" and value.get("state") == "complete" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after comment acceptance")
                return super().record_publication_step(repository_id, publication_id, step, value)

        client = _Client(_manifest("select x from orders"), _manifest("select x from orders"))
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = CrashBeforeJournalComplete(tmp)
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            with self.assertRaises(RuntimeError):
                runner.run(_event("after-comment-api"), client, expected_app_id=123)
            runner.run(_event("after-comment-api"), client, expected_app_id=123)
        self.assertEqual(len([call for call in client.comment_calls if call[0] == "create"]), 1)
        self.assertEqual(len([call for call in client.comment_calls if call[0] == "update"]), 1)

    def test_crash_after_check_id_persisted_reuses_check(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class CheckClient(_Client):
            def list_check_runs(self, owner, repository, *, head_sha, check_name):
                return [
                    check for check in self.checks
                    if check.get("external_id") == "review-12-head-sha-shadow"
                ]

        class CrashAfterJournalComplete(RepositoryStorage):
            crashed = False

            def record_publication_step(self, repository_id, publication_id, step, value):
                super().record_publication_step(repository_id, publication_id, step, value)
                if step == "check" and value.get("state") == "complete" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after check id persistence")

        client = CheckClient(_manifest("select x from orders"), _manifest("select x from orders"))
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = CrashAfterJournalComplete(tmp)
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            with self.assertRaises(RuntimeError):
                runner.run(_event("after-check-api"), client, expected_app_id=123)
            runner.run(_event("after-check-api"), client, expected_app_id=123)
        self.assertEqual(len(client.checks), 1)

    def test_started_comment_is_reconciled_against_github(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = _Client(_manifest("select x from orders"), _manifest("select x from orders"))
        existing = {
            "id": 42,
            "body": "<!-- relium-github-app-review -->\nold",
            "performed_via_github_app": {"id": 123},
            "created_at": "2025-01-01T00:00:00Z",
        }
        client.comments = [existing]
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = RepositoryStorage(tmp)
            publication_id = "review-12-head-sha-shadow"
            storage.record_publication_step(12, publication_id, "comment", {"state": "started"})
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            runner.run(_event("ambiguous-comment"), client, expected_app_id=123)
        self.assertEqual([call[0] for call in client.comment_calls], ["update"])

    def test_started_check_is_reconciled_before_create(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class CheckClient(_Client):
            def __init__(self, *args):
                super().__init__(*args)
                self.existing_checks = [
                    {
                        "id": 99,
                        "name": "Relium deployment review",
                        "head_sha": "head-sha",
                        "external_id": "review-12-head-sha-shadow",
                    }
                ]

            def list_check_runs(self, owner, repository, *, head_sha, check_name):
                return self.existing_checks

        client = CheckClient(_manifest("select x from orders"), _manifest("select x from orders"))
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = RepositoryStorage(tmp)
            publication_id = "review-12-head-sha-shadow"
            storage.record_publication_step(12, publication_id, "check", {"state": "started"})
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            response = runner.run(_event("ambiguous-check"), client, expected_app_id=123)
        self.assertEqual(response["check"]["id"], 99)
        self.assertEqual(client.checks, [])

    def test_crash_after_remote_check_creation_reconciles_check_from_page_two(self):
        import urllib.parse

        from agent.github_app.client import GitHubClient
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_client import _Response

        class PagedCheckClient(_Client):
            def __init__(self, *args):
                super().__init__(*args)
                self.remote_checks = []
                self.check_list_pages = []
                self.check_create_count = 0
                self.api = GitHubClient(transport=self._transport)

            def _transport(self, request):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
                page = int(query.get("page", ["1"])[0])
                self.check_list_pages.append(page)
                if page == 1:
                    values = [
                        {
                            "id": index,
                            "name": "Relium deployment review",
                            "head_sha": "head-sha",
                            "external_id": f"other-{index}",
                        }
                        for index in range(100)
                    ]
                else:
                    values = list(self.remote_checks)
                return _Response(
                    200,
                    {"total_count": 100 + len(self.remote_checks), "check_runs": values},
                )

            def list_check_runs(self, owner, repository, *, head_sha, check_name):
                return self.api.list_check_runs(
                    owner,
                    repository,
                    head_sha=head_sha,
                    check_name=check_name,
                )

            def create_check_run(self, owner, repository, payload):
                self.check_create_count += 1
                remote = {"id": 7001, **payload}
                self.remote_checks.append(remote)
                return remote

        class CrashBeforeJournalCompletion(RepositoryStorage):
            crashed = False

            def record_publication_step(self, repository_id, publication_id, step, value):
                if step == "check" and value.get("state") == "complete" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after remote check creation")
                return super().record_publication_step(repository_id, publication_id, step, value)

        client = PagedCheckClient(
            _manifest("select x from orders"),
            _manifest("select x from orders"),
        )
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = CrashBeforeJournalCompletion(tmp)
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            with self.assertRaises(RuntimeError):
                runner.run(_event("check-page-two-crash"), client, expected_app_id=123)
            response = runner.run(
                _event("check-page-two-crash"), client, expected_app_id=123
            )

        self.assertEqual(response["check"]["id"], 7001)
        self.assertEqual(client.check_create_count, 1)
        self.assertEqual(client.check_list_pages, [1, 2])

    def test_crash_after_comment_does_not_create_a_second_comment(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class CrashStorage(RepositoryStorage):
            crashed = False

            def record_publication_step(self, repository_id, publication_id, step, value):
                super().record_publication_step(repository_id, publication_id, step, value)
                if step == "comment" and value.get("state") == "complete" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after comment")

        client = _Client(_manifest("select x from orders"), _manifest("select x from orders"))
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = CrashStorage(tmp)
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            with self.assertRaises(RuntimeError):
                runner.run(_event("crash-comment"), client, expected_app_id=123)
            runner.run(_event("crash-comment"), client, expected_app_id=123)

        self.assertEqual(len(client.comment_calls), 1)

    def test_crash_after_check_does_not_create_a_second_check(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        class CrashStorage(RepositoryStorage):
            crashed = False

            def record_publication_step(self, repository_id, publication_id, step, value):
                super().record_publication_step(repository_id, publication_id, step, value)
                if step == "check" and value.get("state") == "complete" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after check")

        client = _Client(_manifest("select x from orders"), _manifest("select x from orders"))
        result = {"decision": "ALLOW", "rendered": {"markdown": "review"}}
        with tempfile.TemporaryDirectory() as tmp:
            storage = CrashStorage(tmp)
            runner = PullRequestReviewRunner(storage=storage, reviewer=Mock(return_value=result))
            with self.assertRaises(RuntimeError):
                runner.run(_event("crash-check"), client, expected_app_id=123)
            runner.run(_event("crash-check"), client, expected_app_id=123)

        self.assertEqual(len(client.checks), 1)


class StorageIntegrityTests(unittest.TestCase):
    def test_concurrent_duplicate_persistence_has_one_winner(self):
        from agent.github_app.jobs import WebhookJob
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            storage = RepositoryStorage(tmp)
            results = []

            def persist():
                results.append(
                    storage.persist_verified_job(
                        7, WebhookJob("same-delivery", "pull_request", b"{}", 1.0)
                    )
                )

            threads = [threading.Thread(target=persist) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(results), [False, True])

    def test_corrupt_job_is_quarantined_during_recovery(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            storage = RepositoryStorage(tmp)
            jobs = Path(tmp) / "7" / "jobs"
            jobs.mkdir(parents=True)
            (jobs / "broken.json").write_text('{"state":', encoding="utf-8")
            self.assertEqual(storage.recover_jobs(7, now=1.0), [])
            self.assertFalse((jobs / "broken.json").exists())
            quarantined = list((jobs / "corrupt").glob("broken.json.*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(storage.recovery_issues[0]["error"], "JSONDecodeError")

    def test_atomic_json_write_syncs_parent_directory(self):
        from agent.github_app import storage as storage_module
        from agent.github_app.jobs import WebhookJob

        with tempfile.TemporaryDirectory() as tmp, patch.object(storage_module, "_sync_directory", wraps=storage_module._sync_directory) as sync:
            storage = storage_module.RepositoryStorage(tmp)
            storage.persist_verified_job(7, WebhookJob("sync", "pull_request", b"{}", 1.0))
            sync.assert_called_once()

if __name__ == "__main__":
    unittest.main()
