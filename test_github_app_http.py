import hashlib
import hmac
import json
import threading
import time
import unittest
from unittest.mock import Mock

from starlette.testclient import TestClient


SECRET = "test-webhook-secret"


def _body(*, action="opened"):
    return json.dumps(
        {
            "action": action,
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


def _signature(body, *, secret=SECRET):
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def _headers(body, **overrides):
    values = {
        "X-Hub-Signature-256": _signature(body),
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-1",
    }
    values.update(overrides)
    return values


class FakeQueue:
    def __init__(self, *, accepts=True):
        self.accepts = accepts
        self.jobs = []
        self.is_running = False
        self.start_calls = 0
        self.stop_calls = []

    def start(self):
        self.start_calls += 1
        self.is_running = True

    def enqueue(self, job):
        if not self.accepts or not self.is_running:
            return False
        self.jobs.append(job)
        return True

    def stop(self, *, timeout):
        self.stop_calls.append(timeout)
        self.is_running = False
        return True


class FailingStartQueue(FakeQueue):
    def start(self):
        raise RuntimeError("worker-start-secret")


class FailingEnqueueQueue(FakeQueue):
    def enqueue(self, job):
        raise RuntimeError("enqueue-secret")


class GitHubAppHttpTests(unittest.TestCase):
    def _client(self, queue=None, **options):
        from agent.github_app.http_app import create_http_app

        queue = queue or FakeQueue()
        app = create_http_app(
            webhook_secret=SECRET,
            job_queue=queue,
            max_body_bytes=options.get("max_body_bytes", 1024),
            shutdown_timeout_seconds=0.5,
            clock=options.get("clock", lambda: 20.0),
            logger=Mock(),
        )
        return TestClient(
            app,
            raise_server_exceptions=options.get("raise_server_exceptions", True),
        ), queue

    def test_put_tenant_preflight_allows_clerk_headers_without_authentication(self):
        from agent.github_app.http_app import create_http_app

        verifier = Mock()
        app = create_http_app(
            webhook_secret=SECRET,
            job_queue=FakeQueue(),
            max_body_bytes=1024,
            shutdown_timeout_seconds=0.5,
            clock=lambda: 20.0,
            logger=Mock(),
            store_pool=Mock(),
            clerk_verifier=verifier,
            cors_allowed_origins=("https://app.relium.dev",),
        )

        with TestClient(app) as client:
            response = client.options(
                "/api/tenants",
                headers={
                    "Origin": "https://app.relium.dev",
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "https://app.relium.dev",
        )
        allowed_methods = {
            method.strip()
            for method in response.headers["Access-Control-Allow-Methods"].split(",")
        }
        self.assertIn("PUT", allowed_methods)
        allowed_headers = {
            header.strip().lower()
            for header in response.headers["Access-Control-Allow-Headers"].split(",")
        }
        self.assertIn("authorization", allowed_headers)
        self.assertIn("content-type", allowed_headers)
        verifier.verify.assert_not_called()

    def test_valid_signature_preserves_raw_body_and_returns_202(self):
        body = _body()
        client, jobs = self._client()
        with client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(), {"status": "accepted", "delivery_id": "delivery-1"}
        )
        self.assertEqual(len(jobs.jobs), 1)
        self.assertEqual(jobs.jobs[0].raw_body, body)
        self.assertEqual(jobs.jobs[0].received_at, 20.0)

    def test_request_returns_without_adapter_or_processor_work(self):
        body = _body()
        client, jobs = self._client()
        started = time.monotonic()
        with client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(jobs.jobs), 1)

    def test_missing_required_headers_return_400(self):
        body = _body()
        for name in (
            "X-Hub-Signature-256",
            "X-GitHub-Event",
            "X-GitHub-Delivery",
        ):
            with self.subTest(name=name):
                headers = _headers(body)
                headers.pop(name)
                client, jobs = self._client()
                with client:
                    response = client.post(
                        "/github/webhook", content=body, headers=headers
                    )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"status": "invalid_request"})
                self.assertEqual(jobs.jobs, [])

    def test_malformed_event_and_delivery_headers_return_400(self):
        body = _body()
        cases = (
            {"X-GitHub-Event": "pull request"},
            {"X-GitHub-Delivery": "delivery/../../unsafe"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                client, jobs = self._client()
                with client:
                    response = client.post(
                        "/github/webhook",
                        content=body,
                        headers=_headers(body, **overrides),
                    )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"status": "invalid_request"})
                self.assertEqual(jobs.jobs, [])

    def test_invalid_signature_returns_401_without_secret_details(self):
        body = _body()
        headers = _headers(body, **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
        client, jobs = self._client()
        with client:
            response = client.post("/github/webhook", content=body, headers=headers)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"status": "unauthorized"})
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(jobs.jobs, [])

    def test_malformed_signature_header_returns_400(self):
        body = _body()
        headers = _headers(body, **{"X-Hub-Signature-256": "not-a-signature"})
        client, jobs = self._client()
        with client:
            response = client.post("/github/webhook", content=body, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"status": "invalid_request"})
        self.assertEqual(jobs.jobs, [])

    def test_malformed_json_after_valid_signature_returns_400(self):
        body = b"not-json secret-body-value"
        client, jobs = self._client()
        with client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"status": "invalid_request"})
        self.assertNotIn("secret-body-value", response.text)
        self.assertEqual(jobs.jobs, [])

    def test_oversized_body_returns_413(self):
        body = b"x" * 17
        client, jobs = self._client(max_body_bytes=16)
        with client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"status": "payload_too_large"})
        self.assertEqual(jobs.jobs, [])

    def test_queue_saturation_returns_503(self):
        body = _body()
        client, jobs = self._client(FakeQueue(accepts=False))
        with client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertEqual(jobs.jobs, [])

    def test_unsupported_event_and_action_are_ignored_without_enqueue(self):
        cases = (("ping", b"{}"), ("pull_request", _body(action="closed")))
        for event_name, body in cases:
            with self.subTest(event_name=event_name):
                headers = _headers(body, **{"X-GitHub-Event": event_name})
                client, jobs = self._client()
                with client:
                    response = client.post(
                        "/github/webhook", content=body, headers=headers
                    )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(
                    response.json(),
                    {"status": "ignored", "delivery_id": "delivery-1"},
                )
                self.assertEqual(jobs.jobs, [])

    def test_duplicate_delivery_is_accepted_for_worker_idempotency(self):
        body = _body()
        client, jobs = self._client()
        with client:
            first = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
            second = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual([job.delivery_id for job in jobs.jobs], ["delivery-1"] * 2)

    def test_health_reflects_startup_queue_and_shutdown_state(self):
        client, jobs = self._client()
        with client:
            healthy = client.get("/healthz")
            self.assertEqual(healthy.status_code, 200)
            self.assertEqual(healthy.json(), {"status": "ok"})
            jobs.is_running = False
            unhealthy = client.get("/healthz")
            self.assertEqual(unhealthy.status_code, 503)
            self.assertEqual(unhealthy.json(), {"status": "unavailable"})
        self.assertEqual(jobs.start_calls, 1)
        self.assertEqual(jobs.stop_calls, [0.5])

    def test_failed_worker_startup_keeps_health_unavailable(self):
        client, jobs = self._client(FailingStartQueue())
        with client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotIn("worker-start-secret", response.text)
        self.assertEqual(jobs.stop_calls, [])

    def test_unexpected_handler_error_is_safe(self):
        body = _body()
        client, jobs = self._client(
            FailingEnqueueQueue(), raise_server_exceptions=False
        )
        with client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotIn("enqueue-secret", response.text)

    def test_signed_request_crosses_queue_and_service_into_fake_adapter(self):
        from agent.github_app.http_app import create_http_app
        from agent.github_app.jobs import BoundedJobQueue
        from agent.github_app.service import WebhookProcessingService

        adapter = Mock()
        adapter.handle_verified.return_value = {"status": "reviewed"}
        completed = threading.Event()

        def process(job):
            try:
                return WebhookProcessingService(
                    adapter, logger=Mock()
                ).process(job)
            finally:
                completed.set()

        jobs = BoundedJobQueue(
            process, worker_count=1, capacity=2, logger=Mock()
        )
        app = create_http_app(
            webhook_secret=SECRET,
            job_queue=jobs,
            max_body_bytes=1024,
            shutdown_timeout_seconds=1.0,
            clock=lambda: 20.0,
            logger=Mock(),
        )
        body = _body()
        with TestClient(app) as client:
            response = client.post(
                "/github/webhook", content=body, headers=_headers(body)
            )
            self.assertEqual(response.status_code, 202)
            self.assertTrue(completed.wait(1.0))
        adapter.handle_verified.assert_called_once_with(
            event_name="pull_request", delivery_id="delivery-1", body=body
        )


if __name__ == "__main__":
    unittest.main()
