import logging
import time
from dataclasses import dataclass

from agent.github_app.client import safe_github_error_fields
from agent.github_app.jobs import WebhookJob, safe_error_category


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    delivery_id: str
    event_name: str
    attempt: int
    duration_seconds: float
    error_category: str | None = None


class WebhookProcessingService:
    """Framework-neutral worker service that delegates to the Phase 1 adapter."""

    def __init__(self, adapter, *, clock=time.monotonic, logger=None):
        self.adapter = adapter
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)

    def process(self, job: WebhookJob) -> ProcessingResult:
        started = self.clock()
        try:
            response = self.adapter.handle_verified(
                event_name=job.event_name,
                delivery_id=job.delivery_id,
                body=job.raw_body,
            )
        except Exception as exc:
            duration = max(0.0, self.clock() - started)
            self.logger.error(
                "webhook_processing_failed",
                extra={
                    "delivery_id": job.delivery_id,
                    "event_name": job.event_name,
                    "attempt": job.attempt,
                    "duration": duration,
                    "error_category": safe_error_category(exc),
                    **safe_github_error_fields(exc),
                },
            )
            raise
        duration = max(0.0, self.clock() - started)
        status = str(response.get("status", "unknown"))
        self.logger.info(
            "webhook_processing_complete",
            extra={
                "delivery_id": job.delivery_id,
                "event_name": job.event_name,
                "attempt": job.attempt,
                "duration": duration,
                "processing_outcome": status,
            },
        )
        return ProcessingResult(
            status=status,
            delivery_id=job.delivery_id,
            event_name=job.event_name,
            attempt=job.attempt,
            duration_seconds=duration,
        )
