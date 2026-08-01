import html
import json
import logging
import math
import re
import socket
import time
import urllib.error
import urllib.request


class SlackPublicationSink:
    """Optional SQL-free Slack sink with bounded transport retries."""

    def __init__(
        self,
        webhook_url,
        *,
        notify_warn=False,
        max_retries=2,
        retry_base_seconds=1.0,
        max_delay_seconds=10.0,
        timeout_seconds=10.0,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
        logger=None,
    ):
        if not isinstance(webhook_url, str) or not webhook_url.strip():
            raise ValueError("Slack webhook URL is required for an enabled sink.")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
            or max_retries > 5
        ):
            raise ValueError("Slack maximum retries must be between zero and five.")
        numeric_values = (retry_base_seconds, max_delay_seconds, timeout_seconds)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in numeric_values
        ):
            raise ValueError("Slack retry and timeout values must be positive.")
        self._webhook_url = webhook_url.strip()
        self.notify_warn = bool(notify_warn)
        self.max_retries = int(max_retries)
        self.retry_base_seconds = float(retry_base_seconds)
        self.max_delay_seconds = float(max_delay_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)

    def __repr__(self):
        return (
            "SlackPublicationSink("
            f"notify_warn={self.notify_warn!r}, "
            f"max_retries={self.max_retries!r}, "
            f"retry_base_seconds={self.retry_base_seconds!r}, "
            f"max_delay_seconds={self.max_delay_seconds!r})"
        )

    def classify(self, result):
        """Classify a review without constructing or sending a Slack payload."""
        decision = str((result or {}).get("decision", "")).upper()
        if decision == "BLOCK" or (decision == "WARN" and self.notify_warn):
            return "publish"
        if decision in {"ALLOW", "WARN"}:
            return "decision_not_configured_for_slack"
        return "decision_not_alertable"

    def publish(
        self,
        *,
        publication_id,
        repository,
        pull_number,
        result,
        pull_url,
    ):
        classification = self.classify(result)
        if classification != "publish":
            return {
                "state": "skipped",
                "publication_id": str(publication_id),
                "reason": classification,
            }
        payload = build_review_payload(
            repository=repository,
            pull_number=pull_number,
            result=result,
            pull_url=pull_url,
        )
        return self._send_bounded(str(publication_id), payload)

    def _send_bounded(self, publication_id, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        attempts = 0
        while attempts <= self.max_retries:
            attempts += 1
            request = urllib.request.Request(
                self._webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    status = int(response.status)
                if 200 <= status < 300:
                    return {
                        "state": "complete",
                        "publication_id": publication_id,
                        "attempts": attempts,
                    }
                category, retryable = _http_failure(status)
            except urllib.error.HTTPError as error:
                category, retryable = _http_failure(error.code)
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
                category, retryable = "slack_network", True
            except Exception:
                category, retryable = "slack_transport", False
            if not retryable or attempts > self.max_retries:
                return {
                    "state": "failed",
                    "publication_id": publication_id,
                    "attempts": attempts,
                    "error_category": category,
                }
            self._logger.warning(
                "slack_publication_retry",
                extra={
                    "publication_id": publication_id,
                    "attempt": attempts,
                    "error_category": category,
                },
            )
            self._sleep(
                min(
                    self.retry_base_seconds * (2 ** (attempts - 1)),
                    self.max_delay_seconds,
                )
            )
        raise AssertionError("bounded Slack retry loop exhausted unexpectedly")


def build_review_payload(*, repository, pull_number, result, pull_url):
    incident = (result or {}).get("incident") or {}
    decision = str((result or {}).get("decision", "")).upper()
    heading = (
        f"Relium blocked PR #{int(pull_number)}"
        if decision == "BLOCK"
        else f"Relium warned on PR #{int(pull_number)}"
    )
    affected_models = incident.get("affected_models") or []
    model = _safe_identifier(
        affected_models[0] if affected_models else None,
        fallback="not available",
    )
    metadata = incident.get("metadata") or {}
    impacted_kpis = metadata.get("impacted_kpis") or metadata.get("affected_kpis") or []
    kpi = _safe_identifier(impacted_kpis[0], fallback=None) if impacted_kpis else None
    health = incident.get("health")
    health_text = str(health) if isinstance(health, (int, float)) else "not available"
    finding_count = len((result or {}).get("material_findings") or [])
    link = _safe_github_link(pull_url)
    lines = [
        heading,
        "",
        f"Repository: {_safe_identifier(repository, fallback='not available')}",
        f"Model: {model}",
        f"Health: {health_text}",
        "",
        f"{finding_count} material findings detected.",
    ]
    if kpi:
        lines.append(f"Affected KPI: {kpi}")
    lines.extend(["", link])
    text = "\n".join(lines)
    return {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ],
    }


def _safe_text(value, *, maximum=200):
    lines = str(value or "").splitlines()
    text = (lines[0] if lines else "").strip()
    text = " ".join(text.split())
    return html.escape(text[:maximum], quote=False) or "not available"


def _safe_identifier(value, *, fallback):
    lines = str(value or "").splitlines()
    text = (lines[0] if lines else "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", text):
        return fallback
    return html.escape(text, quote=False)


def _safe_github_link(value):
    url = str(value or "")
    if url.startswith("https://github.com/") and "\n" not in url and "\r" not in url:
        return f"<{url}|View GitHub review>"
    return "View GitHub review"


def _http_failure(status):
    if status == 429:
        return "slack_rate_limit", True
    if 500 <= status <= 599:
        return "slack_server", True
    return "slack_http", False
