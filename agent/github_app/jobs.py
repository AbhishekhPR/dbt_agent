import logging
import math
import queue
import threading
import time
import urllib.error
from dataclasses import dataclass, field, replace

from agent.github_app.client import GitHubAPIError, safe_github_error_fields


class WebhookJobError(ValueError):
    """Raised when a queued webhook job is invalid."""


class TemporaryServiceError(RuntimeError):
    """Explicit marker for a safely retryable temporary service failure."""


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    event_name: str
    raw_body: bytes = field(repr=False)
    received_at: float
    attempt: int = 0

    def __post_init__(self):
        if not isinstance(self.delivery_id, str) or not self.delivery_id.strip():
            raise WebhookJobError("Webhook delivery id is required.")
        if not isinstance(self.event_name, str) or not self.event_name.strip():
            raise WebhookJobError("Webhook event name is required.")
        if not isinstance(self.raw_body, (bytes, bytearray)):
            raise WebhookJobError("Webhook raw body must be bytes.")
        if not isinstance(self.received_at, (int, float)) or not math.isfinite(
            self.received_at
        ):
            raise WebhookJobError("Webhook received time must be finite.")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 0
        ):
            raise WebhookJobError("Webhook attempt must be zero or greater.")
        object.__setattr__(self, "delivery_id", self.delivery_id.strip())
        object.__setattr__(self, "event_name", self.event_name.strip())
        object.__setattr__(self, "raw_body", bytes(bytearray(self.raw_body)))

    @property
    def job_id(self) -> str:
        return self.delivery_id


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    base_seconds: float
    max_delay_seconds: float = 60.0

    def __post_init__(self):
        if isinstance(self.max_retries, bool) or self.max_retries < 0:
            raise ValueError("Maximum retries must be zero or greater.")
        if self.base_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("Retry delays must be positive.")

    def delay_for_attempt(self, attempt: int) -> float:
        return min(self.base_seconds * (2**attempt), self.max_delay_seconds)


def is_retryable_error(error: Exception) -> bool:
    if isinstance(error, TemporaryServiceError):
        return True
    if isinstance(error, GitHubAPIError):
        status = error.status_code
        return status == 429 or (isinstance(status, int) and 500 <= status <= 599)
    return isinstance(
        error, (urllib.error.URLError, TimeoutError, ConnectionError)
    )


def safe_error_category(error: Exception) -> str:
    if isinstance(error, GitHubAPIError):
        status = error.status_code
        if status == 429:
            return "github_rate_limit"
        if isinstance(status, int) and 500 <= status <= 599:
            return "github_server"
        if status in {401, 403}:
            return "github_authorization"
        return "github_api"
    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "network"
    if isinstance(error, TemporaryServiceError):
        return "temporary_service"
    return "processing"


class RetryingJobProcessor:
    def __init__(self, processor, policy: RetryPolicy, *, sleep=time.sleep, logger=None):
        self.processor = processor
        self.policy = policy
        self.sleep = sleep
        self.logger = logger or logging.getLogger(__name__)

    def __call__(self, job: WebhookJob):
        current = job
        while True:
            try:
                return self.processor(current)
            except Exception as exc:
                if not is_retryable_error(exc) or current.attempt >= self.policy.max_retries:
                    raise
                delay = self.policy.delay_for_attempt(current.attempt)
                self.logger.warning(
                    "webhook_job_retry",
                    extra={
                        "delivery_id": current.delivery_id,
                        "event_name": current.event_name,
                        "attempt": current.attempt,
                        "error_category": safe_error_category(exc),
                        **safe_github_error_fields(exc),
                    },
                )
                self.sleep(delay)
                current = replace(current, attempt=current.attempt + 1)


_STOP = object()


class BoundedJobQueue:
    def __init__(
        self,
        processor,
        *,
        worker_count: int,
        capacity: int,
        clock=time.monotonic,
        logger=None,
    ):
        if isinstance(worker_count, bool) or worker_count <= 0:
            raise ValueError("Worker count must be positive.")
        if isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("Queue capacity must be positive.")
        self.processor = processor
        self.worker_count = worker_count
        self.capacity = capacity
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._threads = []
        self._running = False
        self._accepting = False

    @property
    def worker_thread_count(self) -> int:
        return len(self._threads)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running and all(thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if self._threads:
                raise RuntimeError("Stopped job queues cannot be restarted.")
            self._running = True
            self._accepting = True
            for number in range(self.worker_count):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"relium-webhook-{number + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def enqueue(self, job: WebhookJob) -> bool:
        with self._lock:
            if not self._accepting:
                return False
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                return False
            return True

    def stop(self, *, timeout: float) -> bool:
        if timeout < 0:
            raise ValueError("Shutdown timeout must be zero or greater.")
        with self._lock:
            if not self._running:
                self._accepting = False
                return True
            self._accepting = False
            self._running = False
        deadline = self.clock() + timeout
        for _ in self._threads:
            remaining = max(0.0, deadline - self.clock())
            try:
                self._queue.put(_STOP, timeout=remaining)
            except queue.Full:
                return False
        for thread in self._threads:
            thread.join(max(0.0, deadline - self.clock()))
        return not any(thread.is_alive() for thread in self._threads)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                try:
                    self.processor(item)
                except Exception as exc:
                    self.logger.error(
                        "webhook_job_failed",
                        extra={
                            "delivery_id": item.delivery_id,
                            "event_name": item.event_name,
                            "attempt": item.attempt,
                            "error_category": safe_error_category(exc),
                            **safe_github_error_fields(exc),
                        },
                    )
            finally:
                self._queue.task_done()
