import dataclasses
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock


def _job(delivery="delivery-1", *, attempt=0, raw_body=b"{}"):
    from agent.github_app.jobs import WebhookJob

    return WebhookJob(
        delivery_id=delivery,
        event_name="pull_request",
        raw_body=raw_body,
        received_at=10.5,
        attempt=attempt,
    )


class WebhookJobTests(unittest.TestCase):
    def test_job_is_immutable_and_copies_raw_bytes(self):
        payload = bytearray(b"original")
        job = _job(raw_body=payload)
        payload[:] = b"modified"
        self.assertEqual(job.raw_body, b"original")
        self.assertEqual(job.job_id, "delivery-1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            job.attempt = 2

    def test_job_rejects_invalid_metadata(self):
        from agent.github_app.jobs import WebhookJobError

        for field, value in (
            ("delivery_id", ""),
            ("event_name", ""),
            ("raw_body", "not-bytes"),
            ("attempt", -1),
        ):
            with self.subTest(field=field):
                values = {
                    "delivery_id": "delivery-1",
                    "event_name": "pull_request",
                    "raw_body": b"{}",
                    "received_at": 1.0,
                    "attempt": 0,
                }
                values[field] = value
                with self.assertRaises(WebhookJobError):
                    from agent.github_app.jobs import WebhookJob

                    WebhookJob(**values)


class RetryingJobProcessorTests(unittest.TestCase):
    def _processor(self, function, *, max_retries=3, base=0.1):
        from agent.github_app.jobs import RetryPolicy, RetryingJobProcessor

        sleeps = []
        processor = RetryingJobProcessor(
            function,
            RetryPolicy(
                max_retries=max_retries,
                base_seconds=base,
                max_delay_seconds=1.0,
            ),
            sleep=sleeps.append,
            logger=Mock(),
        )
        return processor, sleeps

    def test_retryable_network_429_and_500_errors_are_classified(self):
        from agent.github_app.client import GitHubAPIError
        from agent.github_app.jobs import TemporaryServiceError, is_retryable_error

        retryable = (
            urllib.error.URLError("temporary"),
            TimeoutError("temporary"),
            ConnectionError("temporary"),
            GitHubAPIError("rate limited", status_code=429),
            GitHubAPIError("server", status_code=500),
            GitHubAPIError("server", status_code=503),
            TemporaryServiceError("temporary"),
        )
        for error in retryable:
            with self.subTest(error=type(error).__name__):
                self.assertTrue(is_retryable_error(error))

    def test_401_403_and_configuration_errors_are_not_retried(self):
        from agent.github_app.client import GitHubAPIError
        from agent.github_app.config import RepositoryConfigError
        from agent.github_app.jobs import is_retryable_error

        errors = (
            GitHubAPIError("authentication", status_code=401),
            GitHubAPIError("permission", status_code=403),
            GitHubAPIError("missing", status_code=404),
            RepositoryConfigError("invalid repository configuration"),
            ValueError("deterministic analysis failure"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_retryable_error(error))

    def test_successful_retry_preserves_delivery_and_updates_attempt(self):
        attempts = []

        def process(job):
            attempts.append((job.delivery_id, job.attempt))
            if job.attempt == 0:
                raise urllib.error.URLError("temporary")
            return "processed"

        processor, sleeps = self._processor(process)
        self.assertEqual(processor(_job()), "processed")
        self.assertEqual(attempts, [("delivery-1", 0), ("delivery-1", 1)])
        self.assertEqual(sleeps, [0.1])

    def test_maximum_retry_limit_and_capped_backoff(self):
        attempts = []

        def process(job):
            attempts.append(job.attempt)
            raise urllib.error.URLError("temporary")

        processor, sleeps = self._processor(process, max_retries=3, base=0.6)
        with self.assertRaises(urllib.error.URLError):
            processor(_job())
        self.assertEqual(attempts, [0, 1, 2, 3])
        self.assertEqual(sleeps, [0.6, 1.0, 1.0])

    def test_non_retryable_error_is_raised_without_sleep(self):
        from agent.github_app.client import GitHubAPIError
        from agent.github_app.config import RepositoryConfigError

        errors = (
            GitHubAPIError("unauthorized", status_code=401),
            GitHubAPIError("forbidden", status_code=403),
            RepositoryConfigError("invalid configuration"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                calls = []

                def process(job):
                    calls.append(job.attempt)
                    raise error

                processor, sleeps = self._processor(process)
                with self.assertRaises(type(error)):
                    processor(_job())
                self.assertEqual(calls, [0])
                self.assertEqual(sleeps, [])

    def test_429_and_500_are_retried_then_succeed(self):
        from agent.github_app.client import GitHubAPIError

        for status in (429, 500):
            with self.subTest(status=status):
                calls = []

                def process(job):
                    calls.append(job.attempt)
                    if job.attempt == 0:
                        raise GitHubAPIError("temporary", status_code=status)
                    return "processed"

                processor, sleeps = self._processor(process, max_retries=1)
                self.assertEqual(processor(_job()), "processed")
                self.assertEqual(calls, [0, 1])
                self.assertEqual(sleeps, [0.1])

    def test_retry_log_includes_safe_github_operation_fields(self):
        from agent.github_app.client import GitHubAPIError

        logger = Mock()
        error = GitHubAPIError(
            "safe",
            status_code=429,
            operation="create_check_run",
            http_method="POST",
            route_template="/repos/{owner}/{repo}/check-runs",
            github_request_id="SAFE-REQUEST-ID",
            accepted_github_permissions="checks=write",
        )
        calls = []

        def process(job):
            calls.append(job.attempt)
            if job.attempt == 0:
                raise error
            return "processed"

        from agent.github_app.jobs import RetryPolicy, RetryingJobProcessor

        processor = RetryingJobProcessor(
            process,
            RetryPolicy(max_retries=1, base_seconds=0.1),
            sleep=Mock(),
            logger=logger,
        )
        self.assertEqual(processor(_job()), "processed")
        extra = logger.warning.call_args.kwargs["extra"]
        self.assertEqual(extra["operation"], "create_check_run")
        self.assertEqual(extra["http_status"], 429)
        self.assertEqual(extra["github_request_id"], "SAFE-REQUEST-ID")
        self.assertTrue(extra["retryable"])
        self.assertEqual(extra["attempt"], 0)


class BoundedJobQueueTests(unittest.TestCase):
    def test_queue_processes_jobs_with_bounded_workers(self):
        from agent.github_app.jobs import BoundedJobQueue

        processed = []
        completed = threading.Event()

        def process(job):
            processed.append(job.delivery_id)
            completed.set()

        jobs = BoundedJobQueue(process, worker_count=2, capacity=3)
        jobs.start()
        try:
            self.assertTrue(jobs.enqueue(_job()))
            self.assertTrue(completed.wait(1.0))
            self.assertEqual(jobs.worker_thread_count, 2)
            self.assertEqual(processed, ["delivery-1"])
            self.assertTrue(jobs.is_running)
        finally:
            self.assertTrue(jobs.stop(timeout=1.0))

    def test_full_queue_returns_false(self):
        from agent.github_app.jobs import BoundedJobQueue

        entered = threading.Event()
        release = threading.Event()

        def process(job):
            entered.set()
            release.wait(1.0)

        jobs = BoundedJobQueue(process, worker_count=1, capacity=1)
        jobs.start()
        try:
            self.assertTrue(jobs.enqueue(_job("running")))
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(jobs.enqueue(_job("queued")))
            self.assertFalse(jobs.enqueue(_job("rejected")))
        finally:
            release.set()
            self.assertTrue(jobs.stop(timeout=1.0))

    def test_processor_failure_does_not_kill_worker(self):
        from agent.github_app.jobs import BoundedJobQueue

        processed = []
        completed = threading.Event()

        def process(job):
            processed.append(job.delivery_id)
            if job.delivery_id == "first":
                raise RuntimeError("secret failure detail")
            completed.set()

        logger = Mock()
        jobs = BoundedJobQueue(
            process, worker_count=1, capacity=2, logger=logger
        )
        jobs.start()
        try:
            self.assertTrue(jobs.enqueue(_job("first")))
            self.assertTrue(jobs.enqueue(_job("second")))
            self.assertTrue(completed.wait(1.0))
            self.assertEqual(processed, ["first", "second"])
            self.assertTrue(jobs.is_running)
            logged = logger.error.call_args
            self.assertEqual(logged.args[0], "webhook_job_failed")
            self.assertNotIn("secret failure detail", str(logged))
        finally:
            self.assertTrue(jobs.stop(timeout=1.0))

    def test_final_failure_log_includes_safe_github_operation_fields(self):
        from agent.github_app.client import GitHubAPIError
        from agent.github_app.jobs import BoundedJobQueue

        completed = threading.Event()
        logger = Mock()
        error = GitHubAPIError(
            "safe",
            status_code=403,
            operation="create_issue_comment",
            http_method="POST",
            route_template="/repos/{owner}/{repo}/issues/{pull_number}/comments",
            github_request_id="SAFE-REQUEST-ID",
            accepted_github_permissions="issues=write",
        )

        def process(job):
            completed.set()
            raise error

        jobs = BoundedJobQueue(process, worker_count=1, capacity=1, logger=logger)
        jobs.start()
        try:
            self.assertTrue(jobs.enqueue(_job()))
            self.assertTrue(completed.wait(1.0))
            while logger.error.call_count == 0:
                time.sleep(0.001)
            extra = logger.error.call_args.kwargs["extra"]
            self.assertEqual(extra["operation"], "create_issue_comment")
            self.assertEqual(extra["http_status"], 403)
            self.assertEqual(extra["github_request_id"], "SAFE-REQUEST-ID")
            self.assertFalse(extra["retryable"])
            self.assertEqual(extra["attempt"], 0)
        finally:
            self.assertTrue(jobs.stop(timeout=1.0))

    def test_shutdown_drains_jobs_and_rejects_new_work(self):
        from agent.github_app.jobs import BoundedJobQueue

        processed = threading.Event()
        jobs = BoundedJobQueue(
            lambda job: processed.set(), worker_count=1, capacity=1
        )
        jobs.start()
        self.assertTrue(jobs.enqueue(_job()))
        self.assertTrue(jobs.stop(timeout=1.0))
        self.assertTrue(processed.is_set())
        self.assertFalse(jobs.is_running)
        self.assertFalse(jobs.enqueue(_job("late")))

    def test_shutdown_timeout_is_bounded(self):
        from agent.github_app.jobs import BoundedJobQueue

        entered = threading.Event()
        release = threading.Event()

        def process(job):
            entered.set()
            release.wait(2.0)

        jobs = BoundedJobQueue(process, worker_count=1, capacity=1)
        jobs.start()
        self.assertTrue(jobs.enqueue(_job()))
        self.assertTrue(entered.wait(1.0))
        started = time.monotonic()
        try:
            self.assertFalse(jobs.stop(timeout=0.05))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(jobs.is_running)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
