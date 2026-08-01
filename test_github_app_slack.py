import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock


def _block_result():
    return {
        "decision": "BLOCK",
        "incident": {
            "health": 65,
            "affected_models": ["fct_orders\nselect private_value from customer_data"],
            "metadata": {"impacted_kpis": ["contribution_margin"]},
        },
        "material_findings": [
            {"title": "unsafe division", "raw_code": "select private_value"},
            {"title": "refund change", "evidence": "customer SQL"},
        ],
        "rendered": {"markdown": "select private_value from customer_data"},
    }


class _Receiver:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.payloads = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                receiver.payloads.append(
                    json.loads(self.rfile.read(length).decode("utf-8"))
                )
                status = receiver.statuses.pop(0) if receiver.statuses else 200
                self.send_response(status)
                self.end_headers()

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}/slack"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SlackPublicationSinkTests(unittest.TestCase):
    def test_block_payload_has_expected_shape_and_excludes_sql(self):
        from agent.github_app.slack import build_review_payload

        payload = build_review_payload(
            repository="example/relium-test",
            pull_number=17,
            result=_block_result(),
            pull_url="https://github.com/example/relium-test/pull/17",
        )

        text = payload["text"]
        self.assertIn("Relium blocked PR #17", text)
        self.assertIn("Repository: example/relium-test", text)
        self.assertIn("Model: fct_orders", text)
        self.assertIn("Health: 65", text)
        self.assertIn("2 material findings detected.", text)
        self.assertIn("Affected KPI: contribution_margin", text)
        self.assertIn("View GitHub review", text)
        serialized = json.dumps(payload).casefold()
        self.assertNotIn("private_value", serialized)
        self.assertNotIn("customer_data", serialized)
        self.assertNotIn("raw_code", serialized)
        self.assertNotIn("rendered", serialized)

    def test_single_line_sql_in_identifier_fields_is_replaced(self):
        from agent.github_app.slack import build_review_payload

        result = _block_result()
        result["incident"]["affected_models"] = [
            "select private_value from customer_data"
        ]
        result["incident"]["metadata"]["impacted_kpis"] = [
            "select environment_value"
        ]
        payload = build_review_payload(
            repository="example/relium-test",
            pull_number=17,
            result=result,
            pull_url="https://github.com/example/relium-test/pull/17",
        )

        serialized = json.dumps(payload).casefold()
        self.assertIn("model: not available", serialized)
        self.assertNotIn("private_value", serialized)
        self.assertNotIn("environment_value", serialized)

    def test_block_posts_once_to_fake_receiver(self):
        from agent.github_app.slack import SlackPublicationSink

        with _Receiver([200]) as receiver:
            sink = SlackPublicationSink(receiver.url, sleep=Mock())
            result = sink.publish(
                publication_id="review-12-head-shadow",
                repository="example/relium-test",
                pull_number=17,
                result=_block_result(),
                pull_url="https://github.com/example/relium-test/pull/17",
            )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["publication_id"], "review-12-head-shadow")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(receiver.payloads), 1)

    def test_allow_and_unconfigured_warn_do_not_post(self):
        from agent.github_app.slack import SlackPublicationSink

        with _Receiver([200]) as receiver:
            sink = SlackPublicationSink(receiver.url, sleep=Mock())
            allow = sink.publish(
                publication_id="allow",
                repository="example/relium-test",
                pull_number=1,
                result={"decision": "ALLOW"},
                pull_url="https://github.com/example/relium-test/pull/1",
            )
            warn = sink.publish(
                publication_id="warn",
                repository="example/relium-test",
                pull_number=2,
                result={"decision": "WARN"},
                pull_url="https://github.com/example/relium-test/pull/2",
            )

        self.assertEqual(allow["state"], "skipped")
        self.assertEqual(warn["state"], "skipped")
        self.assertEqual(receiver.payloads, [])

    def test_configured_warn_posts_and_uses_warn_heading(self):
        from agent.github_app.slack import SlackPublicationSink

        result = _block_result()
        result["decision"] = "WARN"
        with _Receiver([200]) as receiver:
            sink = SlackPublicationSink(
                receiver.url,
                notify_warn=True,
                sleep=Mock(),
            )
            response = sink.publish(
                publication_id="warn",
                repository="example/relium-test",
                pull_number=2,
                result=result,
                pull_url="https://github.com/example/relium-test/pull/2",
            )

        self.assertEqual(response["state"], "complete")
        self.assertIn("Relium warned on PR #2", receiver.payloads[0]["text"])

    def test_retryable_server_failure_is_bounded_and_recovers(self):
        from agent.github_app.slack import SlackPublicationSink

        sleep = Mock()
        with _Receiver([500, 200]) as receiver:
            sink = SlackPublicationSink(
                receiver.url,
                max_retries=2,
                retry_base_seconds=0.01,
                sleep=sleep,
            )
            result = sink.publish(
                publication_id="retry",
                repository="example/relium-test",
                pull_number=3,
                result=_block_result(),
                pull_url="https://github.com/example/relium-test/pull/3",
            )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(receiver.payloads), 2)
        sleep.assert_called_once_with(0.01)

    def test_rate_limit_is_retried_and_recovers(self):
        from agent.github_app.slack import SlackPublicationSink

        sleep = Mock()
        with _Receiver([429, 200]) as receiver:
            sink = SlackPublicationSink(
                receiver.url,
                max_retries=1,
                retry_base_seconds=0.02,
                sleep=sleep,
            )
            result = sink.publish(
                publication_id="rate-limit",
                repository="example/relium-test",
                pull_number=3,
                result=_block_result(),
                pull_url="https://github.com/example/relium-test/pull/3",
            )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(receiver.payloads), 2)
        sleep.assert_called_once_with(0.02)

    def test_timeout_retry_is_bounded_and_returns_safe_failure(self):
        from agent.github_app.slack import SlackPublicationSink

        opener = Mock(side_effect=TimeoutError("private timeout detail"))
        sleep = Mock()
        sink = SlackPublicationSink(
            "https://slack.test/services/redacted",
            max_retries=2,
            retry_base_seconds=0.01,
            opener=opener,
            sleep=sleep,
        )
        result = sink.publish(
            publication_id="timeout",
            repository="example/relium-test",
            pull_number=3,
            result=_block_result(),
            pull_url="https://github.com/example/relium-test/pull/3",
        )

        self.assertEqual(
            result,
            {
                "state": "failed",
                "publication_id": "timeout",
                "attempts": 3,
                "error_category": "slack_network",
            },
        )
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])
        self.assertNotIn("private timeout detail", json.dumps(result))

    def test_exponential_retry_delay_is_capped(self):
        from agent.github_app.slack import SlackPublicationSink

        opener = Mock(side_effect=TimeoutError("private timeout detail"))
        sleep = Mock()
        sink = SlackPublicationSink(
            "https://slack.test/services/redacted",
            max_retries=2,
            retry_base_seconds=8,
            max_delay_seconds=10,
            opener=opener,
            sleep=sleep,
        )
        sink.publish(
            publication_id="capped-delay",
            repository="example/relium-test",
            pull_number=3,
            result=_block_result(),
            pull_url="https://github.com/example/relium-test/pull/3",
        )

        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [8, 10],
        )

    def test_non_retryable_http_failure_stops_after_one_attempt(self):
        from agent.github_app.slack import SlackPublicationSink

        sleep = Mock()
        with _Receiver([400, 200]) as receiver:
            sink = SlackPublicationSink(receiver.url, max_retries=3, sleep=sleep)
            result = sink.publish(
                publication_id="failure",
                repository="example/relium-test",
                pull_number=4,
                result=_block_result(),
                pull_url="https://github.com/example/relium-test/pull/4",
            )

        self.assertEqual(
            result,
            {
                "state": "failed",
                "publication_id": "failure",
                "attempts": 1,
                "error_category": "slack_http",
            },
        )
        self.assertEqual(len(receiver.payloads), 1)
        sleep.assert_not_called()


class SlackPublicationJournalTests(unittest.TestCase):
    def test_slack_step_persists_and_unknown_step_remains_rejected(self):
        from agent.github_app.storage import RepositoryStorage, StorageError

        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            storage.record_publication_step(
                12,
                "review-12-head-shadow",
                "slack",
                {"state": "complete", "publication_id": "review-12-head-shadow"},
            )
            reloaded = RepositoryStorage(root).get_publication_journal(
                12, "review-12-head-shadow"
            )
            with self.assertRaises(StorageError):
                storage.record_publication_step(12, "review", "unknown", {})

        self.assertEqual(reloaded["slack"]["state"], "complete")

    def test_slack_step_has_exactly_one_atomic_claim_winner(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            barrier = threading.Barrier(8)
            results = []

            def claim():
                barrier.wait(timeout=2)
                results.append(
                    storage.claim_publication_step(
                        12,
                        "review-12-head-shadow",
                        "slack",
                        {
                            "state": "started",
                            "publication_id": "review-12-head-shadow",
                        },
                    )
                )

            threads = [threading.Thread(target=claim) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            journal = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)
        self.assertEqual(journal["slack"]["state"], "started")

    def test_stale_started_reconciliation_cannot_overwrite_complete(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            storage.record_publication_step(
                12,
                "review-12-head-shadow",
                "slack",
                {"state": "started", "publication_id": "review-12-head-shadow"},
            )
            stale = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )["slack"]
            self.assertEqual(stale["state"], "started")
            complete = {
                "state": "complete",
                "publication_id": "review-12-head-shadow",
                "attempts": 1,
            }
            storage.record_publication_step(
                12, "review-12-head-shadow", "slack", complete
            )
            resolved = storage.transition_publication_step(
                12,
                "review-12-head-shadow",
                "slack",
                expected_state="started",
                value={
                    "state": "indeterminate",
                    "publication_id": "review-12-head-shadow",
                    "reason": "prior_attempt_cannot_be_reconciled",
                },
            )
            journal = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )

        self.assertEqual(resolved, complete)
        self.assertEqual(journal["slack"], complete)


class SlackRunnerIntegrationTests(unittest.TestCase):
    def test_runner_publishes_after_github_and_persists_terminal_state(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_runner import FakeClient, _event, _material_block_result

        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            publication_id = "review-12-head-shadow"

            def publish(**arguments):
                journal = storage.get_publication_journal(12, publication_id)
                self.assertEqual(journal["comment"]["state"], "complete")
                self.assertEqual(journal["check"]["state"], "complete")
                return {
                    "state": "complete",
                    "publication_id": arguments["publication_id"],
                    "attempts": 1,
                }

            publisher = Mock()
            publisher.classify.return_value = "publish"
            publisher.publish.side_effect = publish
            response = PullRequestReviewRunner(
                storage=storage,
                reviewer=Mock(return_value=_material_block_result()),
                slack_publisher=publisher,
            ).run(_event("slack-block"), FakeClient(), expected_app_id=123)
            reloaded = RepositoryStorage(root).get_publication_journal(
                12, publication_id
            )

        self.assertEqual(response["status"], "reviewed")
        self.assertEqual(response["slack"]["state"], "complete")
        self.assertEqual(reloaded["slack"]["state"], "complete")
        publisher.publish.assert_called_once_with(
            publication_id=publication_id,
            repository="acme/analytics",
            pull_number=4,
            result=response["result"],
            pull_url="https://github.com/acme/analytics/pull/4",
        )

    def test_duplicate_delivery_produces_at_most_one_slack_publication(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.slack import SlackPublicationSink
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_runner import FakeClient, _event, _material_block_result

        with _Receiver([200]) as receiver:
            publisher = SlackPublicationSink(receiver.url, sleep=Mock())
            with tempfile.TemporaryDirectory() as root:
                runner = PullRequestReviewRunner(
                    storage=RepositoryStorage(root),
                    reviewer=Mock(return_value=_material_block_result()),
                    slack_publisher=publisher,
                )
                client = FakeClient()
                first = runner.run(
                    _event("same-delivery"), client, expected_app_id=123
                )
                duplicate = runner.run(
                    _event("same-delivery"), client, expected_app_id=123
                )
                redelivery = runner.run(
                    _event("new-delivery-same-publication"),
                    client,
                    expected_app_id=123,
                )

        self.assertEqual(first["slack"]["state"], "complete")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(redelivery["status"], "reviewed")
        self.assertEqual(redelivery["slack"]["state"], "complete")
        self.assertEqual(len(receiver.payloads), 1)

    def test_slack_failure_cannot_undo_successful_github_publication(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_runner import FakeClient, _event, _material_block_result

        publisher = Mock()
        publisher.classify.return_value = "publish"
        publisher.publish.side_effect = RuntimeError("private webhook failure")
        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            client = FakeClient()
            response = PullRequestReviewRunner(
                storage=storage,
                reviewer=Mock(return_value=_material_block_result()),
                slack_publisher=publisher,
            ).run(_event("slack-failure"), client, expected_app_id=123)
            journal = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )

        self.assertEqual(response["status"], "reviewed")
        self.assertEqual(response["slack"]["state"], "failed")
        self.assertNotIn("private webhook failure", json.dumps(response))
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(len(client.checks), 1)
        self.assertEqual(journal["comment"]["state"], "complete")
        self.assertEqual(journal["check"]["state"], "complete")
        self.assertEqual(journal["slack"]["state"], "failed")

    def test_started_slack_intent_becomes_indeterminate_without_resend(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_runner import FakeClient, _event, _material_block_result

        publisher = Mock()
        publisher.classify.return_value = "publish"
        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            storage.record_publication_step(
                12,
                "review-12-head-shadow",
                "slack",
                {
                    "state": "started",
                    "publication_id": "review-12-head-shadow",
                },
            )
            response = PullRequestReviewRunner(
                storage=storage,
                reviewer=Mock(return_value=_material_block_result()),
                slack_publisher=publisher,
            ).run(_event("recover-slack"), FakeClient(), expected_app_id=123)
            journal = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )

        self.assertEqual(response["slack"]["state"], "indeterminate")
        self.assertEqual(journal["slack"]["state"], "indeterminate")
        publisher.publish.assert_not_called()

    def test_allow_is_persisted_skipped_without_started_intent(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.slack import SlackPublicationSink
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_runner import FakeClient, _event

        publisher = SlackPublicationSink(
            "https://slack.test/services/redacted",
            opener=Mock(side_effect=AssertionError("network must not be called")),
            sleep=Mock(),
        )
        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            response = PullRequestReviewRunner(
                storage=storage,
                reviewer=Mock(return_value={"decision": "ALLOW"}),
                slack_publisher=publisher,
            ).run(_event("allow-slack"), FakeClient(), expected_app_id=123)
            journal = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )

        self.assertEqual(response["slack"]["state"], "skipped")
        self.assertEqual(journal["slack"]["state"], "skipped")
        self.assertEqual(
            journal["slack"]["reason"], "decision_not_configured_for_slack"
        )

    def test_concurrent_deliveries_atomically_claim_one_slack_send(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from test_github_app_runner import FakeClient, _event, _material_block_result

        entered = threading.Event()
        release = threading.Event()
        publisher = Mock()
        publisher.classify.return_value = "publish"

        def publish(**arguments):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return {
                "state": "complete",
                "publication_id": arguments["publication_id"],
                "attempts": 1,
            }

        publisher.publish.side_effect = publish
        with tempfile.TemporaryDirectory() as root:
            storage = RepositoryStorage(root)
            for step in ("comment", "check"):
                storage.record_publication_step(
                    12,
                    "review-12-head-shadow",
                    step,
                    {"state": "complete", "value": {"id": 1}},
                )
            runner = PullRequestReviewRunner(
                storage=storage,
                reviewer=Mock(return_value=_material_block_result()),
                slack_publisher=publisher,
            )
            responses = []

            def run_delivery(delivery_id):
                responses.append(
                    runner.run(_event(delivery_id), FakeClient(), expected_app_id=123)
                )

            first = threading.Thread(target=run_delivery, args=("concurrent-one",))
            second = threading.Thread(target=run_delivery, args=("concurrent-two",))
            first.start()
            self.assertTrue(entered.wait(timeout=2))
            second.start()
            second.join(timeout=2)
            release.set()
            first.join(timeout=2)
            journal = storage.get_publication_journal(
                12, "review-12-head-shadow"
            )

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(publisher.publish.call_count, 1)
        self.assertEqual(journal["slack"]["state"], "complete")
        self.assertEqual(len(responses), 2)


if __name__ == "__main__":
    unittest.main()
