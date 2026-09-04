"""Minimal HTTP client for Relium's public collector API.

Only the endpoints the collector actually uses. The transport is injectable so
the same client code can be exercised against a real ASGI application in tests
without a socket, which keeps the tested path and the shipped path identical.

The Authorization header is constructed here and nowhere else, and no method
on this class returns or logs it.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from agent.collector.config import COLLECTOR_VERSION


class ReliumApiError(RuntimeError):
    """The API rejected a call. Carries status and a bounded reason only."""

    def __init__(self, message, *, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def _urllib_transport(timeout, ca_bundle=None):
    """Standard-library HTTP with TLS verification always on.

    There is deliberately no setting that disables verification. A customer
    behind a TLS-inspecting proxy or a private CA supplies the bundle instead.
    Proxies come from the usual HTTP_PROXY / HTTPS_PROXY / NO_PROXY variables,
    which urllib already honours, so no proxy option is invented here.
    """
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle \
        else ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    def send(method, url, body, headers):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout,
                                        context=context) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return exc.code, {}
        except urllib.error.URLError as exc:
            # Reason objects can carry the target; keep only the class name.
            raise ReliumApiError(
                f"could not reach the Relium API ({type(exc).__name__})") from None

    return send


class ReliumClient:
    """Talks to the Relium public API as an authenticated collector."""

    def __init__(self, config, *, transport=None):
        self._config = config
        self._send = transport or _urllib_transport(config.timeout_seconds,
                                                    config.ca_bundle)

    def _headers(self, idempotency_key=None):
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Accept": "application/json",
            "User-Agent": "relium-collector",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _call(self, method, path, body=None, *, idempotency_key=None,
              expected=(200,)):
        url = f"{self._config.api_url}{path}"
        status, payload = self._send(method, url, body, self._headers(idempotency_key))
        if status not in expected:
            reason = (payload or {}).get("message") or (payload or {}).get("status") \
                or (payload or {}).get("reason") or "unexpected response"
            raise ReliumApiError(f"{method} {path} returned HTTP {status}: {reason}",
                                 status=status, payload=payload)
        return status, payload

    # -- identity -----------------------------------------------------------

    def register(self):
        """Register this collector identity. Idempotent, and required.

        The API refuses a snapshot whose collector_id it does not know, so a
        collector that never registers can read requests and then fail at the
        last step - which is exactly how this was found.
        """
        _, payload = self._call(
            "POST", "/api/collectors",
            {"collector_id": self._config.collector_id,
             "environment": self._config.environment,
             "collector_version": COLLECTOR_VERSION,
             "adapter_type": self._config.adapter_type},
            idempotency_key=f"register-{self._config.collector_id}")
        return payload.get("collector")

    def report_verification(self, *, status, error_category=None):
        """Record a bounded connectivity result; never send driver text."""
        body = {
            "collector_id": self._config.collector_id,
            "environment": self._config.environment,
            "status": status,
        }
        if error_category:
            body["error_category"] = error_category
        _, payload = self._call(
            "POST", "/api/collectors/verification", body,
            idempotency_key=(f"verify-{self._config.collector_id}-{status}"))
        return payload

    # -- collection requests ------------------------------------------------

    def pending_requests(self, limit=1):
        _, payload = self._call(
            "GET",
            f"/api/collection-requests?environment={self._config.environment}"
            f"&limit={int(limit)}")
        return payload.get("requests") or []

    def get_request(self, request_id):
        _, payload = self._call("GET", f"/api/collection-requests/{request_id}")
        return payload.get("request")

    def acknowledge(self, request_id):
        _, payload = self._call(
            "POST", f"/api/collection-requests/{request_id}/acknowledge",
            {"collector_id": self._config.collector_id},
            idempotency_key=f"ack-{request_id}-{self._config.collector_id}")
        return payload

    def report_failure(self, request_id, reason):
        _, payload = self._call(
            "POST", f"/api/collection-requests/{request_id}/failure",
            {"reason": str(reason)[:256]},
            idempotency_key=f"fail-{request_id}-{self._config.collector_id}")
        return payload

    # -- snapshots ----------------------------------------------------------

    def submit_snapshot(self, snapshot, idempotency_key):
        status, payload = self._call(
            "POST", "/api/metadata-snapshots", snapshot,
            idempotency_key=idempotency_key,
            # 202 new, 200 exact replay, 409 conflicting replay. All three are
            # real answers the collector must report faithfully rather than
            # retry blindly.
            expected=(200, 202, 409))
        return status, payload
