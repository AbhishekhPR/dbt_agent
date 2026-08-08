"""Recording transports for the outbound boundaries.

The dedicated relium-e2e GitHub App credentials live only as GitHub Actions
secrets, which are write-only by design and cannot be read back on a
developer machine. The only private key present locally belongs to Relium
Pilot and is out of bounds. There is likewise no dedicated E2E Slack
destination configured.

So the local run uses the REAL publishers and records the FINAL transport:

  * ``GitHubAppClient`` is constructed with a recording ``transport``, so
    upsert/update logic, route construction, headers and payloads are all the
    product's own - only the socket is replaced.
  * ``SlackPublicationSink`` is constructed with a recording ``opener``, so
    classification, payload building and retry policy are all the product's
    own - only the socket is replaced.

Nothing here relaxes a rule or shortcuts a decision. It records what the
product tried to send. Evidence produced this way is labelled
``transport: recorded`` and must never be described as live-published.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Response:
    """Minimal urllib-shaped response."""

    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = {}

    def read(self):
        return self._body

    def getheader(self, _name, default=None):
        return default

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingGitHubTransport:
    """Answers GitHub API calls from local state and records every request.

    The identities it returns are stable, so "the same comment was updated"
    is a checkable property of the recording rather than an assumption.
    """

    COMMENT_ID = 900001
    CHECK_RUN_ID = 800001

    def __init__(self, path, *, app_id=424242):
        self.path = path
        self.app_id = app_id
        self.calls = []
        self._lock = threading.Lock()
        self._comment_body = None
        self._check = None

    # -- request handling -------------------------------------------------
    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        body = None
        if request.data:
            try:
                body = json.loads(request.data.decode("utf-8"))
            except ValueError:
                body = {"<unparsed>": len(request.data)}

        payload, status = self._respond(method, url, body)
        self._record(method, url, body, status)
        return _Response(payload, status)

    def _respond(self, method, url, body):
        path = url.split("api.github.com", 1)[-1]

        # List comments: report the one sticky comment we own, if created.
        if method == "GET" and "/issues/" in path and path.endswith("/comments"):
            if self._comment_body is None:
                return [], 200
            return [{
                "id": self.COMMENT_ID, "body": self._comment_body,
                "created_at": "2026-08-07T00:00:00Z",
                "performed_via_github_app": {"id": self.app_id},
            }], 200

        if method == "POST" and path.endswith("/comments"):
            self._comment_body = (body or {}).get("body")
            return {"id": self.COMMENT_ID, "body": self._comment_body}, 201

        if method == "PATCH" and "/issues/comments/" in path:
            self._comment_body = (body or {}).get("body")
            comment_id = int(path.rsplit("/", 1)[-1])
            return {"id": comment_id, "body": self._comment_body}, 200

        if method == "GET" and "/check-runs" in path and "/commits/" in path:
            runs = [self._check] if self._check else []
            return {"total_count": len(runs), "check_runs": runs}, 200

        if method == "POST" and path.endswith("/check-runs"):
            self._check = {
                "id": self.CHECK_RUN_ID, "name": (body or {}).get("name"),
                "head_sha": (body or {}).get("head_sha"),
                "conclusion": (body or {}).get("conclusion"),
                "external_id": (body or {}).get("external_id"),
                "app": {"id": self.app_id},
            }
            return dict(self._check), 201

        if method == "PATCH" and "/check-runs/" in path:
            check_run_id = int(path.rsplit("/", 1)[-1])
            self._check = {
                **(self._check or {}), "id": check_run_id,
                "conclusion": (body or {}).get("conclusion",
                                               (self._check or {}).get("conclusion")),
                "app": {"id": self.app_id},
            }
            return dict(self._check), 200

        return {}, 200

    def _record(self, method, url, body, status):
        path = url.split("api.github.com", 1)[-1]
        entry = {
            "at": _now(), "method": method, "path": path, "status": status,
            "transport": "recorded",
        }
        if isinstance(body, dict):
            # Bodies are product output: a rendered comment or a check payload.
            # Kept whole, minus nothing, because none of it is secret.
            entry["body"] = {k: v for k, v in body.items()}
            summary = (entry["body"].get("output") or {}).get("summary")
            if isinstance(summary, str) and len(summary) > 2000:
                entry["body"]["output"]["summary"] = summary[:2000] + "…[truncated]"
        with self._lock:
            self.calls.append(entry)
            self._flush()

    def _flush(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({
                "transport": "recorded",
                "live_published": False,
                "note": ("The real GitHub publisher ran; only the socket was "
                         "replaced. These were NOT posted to github.com."),
                "sticky_comment_id": self.COMMENT_ID,
                "check_run_id": self.CHECK_RUN_ID,
                "call_count": len(self.calls),
                "calls": self.calls,
            }, handle, indent=2, sort_keys=True)


class RecordingSlackOpener:
    """Captures what the real Slack sink decided to send."""

    def __init__(self, path):
        self.path = path
        self.messages = []
        self._lock = threading.Lock()
        self._flush()

    def __call__(self, request, timeout=None):
        try:
            payload = json.loads(request.data.decode("utf-8"))
        except (ValueError, AttributeError):
            payload = {"<unparsed>": True}
        with self._lock:
            self.messages.append({"at": _now(), "payload": payload,
                                  "transport": "recorded"})
            self._flush()
        return _Response({"ok": True})

    def _flush(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({
                "transport": "recorded",
                "live_published": False,
                "note": ("The real SlackPublicationSink ran, including its "
                         "classify() rules and retry policy. Only the socket "
                         "was replaced. Nothing reached a Slack workspace."),
                "message_count": len(self.messages),
                "messages": self.messages,
            }, handle, indent=2, sort_keys=True)
