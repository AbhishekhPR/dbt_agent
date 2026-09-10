"""The Polar API boundary.

Two calls, and deliberately no more:

    POST /v1/checkouts/          create a checkout session for one product
    POST /v1/customer-sessions/  mint a customer portal session

Both are documented in Polar's current API (2026-04) and both are addressed by
Relium's own external customer id, which is the tenant id. Everything else a
customer can do to their subscription — payment method, invoices, cancellation,
plan management — happens in Polar's hosted portal, so there is nothing here to
rebuild.

Injectable ``transport`` for the same reason agent/github_app/client.py has one:
the tests exercise the real request construction and the real response handling
without a network, and the suite can never make a real charge.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

#: Bound on a Polar response. These are small JSON objects; anything larger is
#: not a checkout session and is not going to be parsed into one.
MAX_RESPONSE_BYTES = 512 * 1024
MAX_LIST_PAGES = 1000
_POLAR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")

logger = logging.getLogger(__name__)

#: Why a call ended without an HTTP status. These are NOT interchangeable, and
#: collapsing them is what made a provider fault unreadable in production:
#:
#:   timeout       the socket deadline expired
#:   unreachable   DNS, connect or TLS failed -- no deadline was involved
#:   inconsistent  Polar answered, but the listing could not be proved complete
#:
#: All three fail closed. Only the first is a timeout, and only the first is
#: evidence about the network.
FAILURE_KINDS = frozenset({"timeout", "unreachable", "inconsistent"})

#: How a listing failed to prove itself complete. Recorded as a fixed enum so a
#: log can be aggregated on it and a caller can branch on it without parsing an
#: error message.
INCONSISTENCY_SUBTYPES = frozenset({
    "malformed_envelope",      # pagination block absent or not the documented shape
    "unstable_pagination",     # total_count/max_page moved between page reads
    "duplicate_object",        # the same id arrived on two pages
    "incomplete_pagination",   # final page reached, item count != total_count
})


class PolarAPIError(RuntimeError):
    """Polar refused or could not answer.

    ``status_code`` is Polar's, when there was one. The message is ours: a
    provider error body can quote request fields back, and this error is
    rendered to a customer.
    """

    def __init__(self, message, *, status_code=None, operation=None,
                 provider_code=None, provider_description=None,
                 failure_kind=None, inconsistency_subtype=None):
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation
        self.provider_code = provider_code
        self.provider_description = provider_description
        #: One of FAILURE_KINDS when no HTTP status was obtained, else None.
        self.failure_kind = failure_kind if failure_kind in FAILURE_KINDS else None
        #: One of INCONSISTENCY_SUBTYPES when failure_kind is "inconsistent".
        self.inconsistency_subtype = (
            inconsistency_subtype
            if inconsistency_subtype in INCONSISTENCY_SUBTYPES else None)

    @property
    def retryable(self) -> bool:
        # A call that never reached a status is retryable: nothing was decided,
        # so another attempt can still decide it. This says only that retrying
        # MAY help -- never that the caller may proceed without an answer.
        if self.failure_kind is not None:
            return True
        return self.status_code == 429 or (
            isinstance(self.status_code, int) and 500 <= self.status_code <= 599)


def _transport_failure_kind(cause):
    """Classify a transport exception WITHOUT reading its text.

    A urllib error's string can contain the full request URL, so the decision
    is made from types alone. `urllib` raises the socket deadline either
    directly (a read that stalled) or wrapped in URLError (a connect that
    stalled), so both shapes are unwrapped.

    Note that `urllib` applies ONE socket timeout to the connect and to each
    read, and exposes no way to set or observe them separately. A connect
    timeout and a read timeout are therefore indistinguishable here; both are
    reported as "timeout", and neither is guessed at.
    """
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, (socket.timeout, TimeoutError)):
            return "timeout"
        cause = cause.reason if isinstance(cause, urllib.error.URLError) else None
    return "unreachable"


class PolarClient:
    def __init__(self, settings, *, transport=None, timeout=10.0):
        self._settings = settings
        self._transport = transport or _urllib_transport
        self._timeout = timeout

    # -- checkout ---------------------------------------------------------

    def create_checkout_session(self, *, product_id, external_customer_id,
                                success_url, metadata=None,
                                customer_metadata=None, customer_email=None):
        """Create a checkout session bound to one Relium workspace.

        ``external_customer_id`` is the Relium tenant id. Polar links the
        resulting customer — and therefore every future subscription webhook —
        to it, creating the customer if this is the workspace's first purchase.
        That is the association the whole integration rests on, and it is set
        here by the server. Nothing about it comes from the browser.

        ``metadata`` is copied onto the checkout and, through it, onto the
        subscription; ``customer_metadata`` onto the customer. Both carry the
        tenant id as a second, independent path back to the workspace, so a
        payload that somehow lacks the external id is still resolvable.
        """
        payload = {
            "products": [product_id],
            "external_customer_id": external_customer_id,
            "success_url": success_url,
        }
        if metadata:
            payload["metadata"] = metadata
        if customer_metadata:
            payload["customer_metadata"] = customer_metadata
        if customer_email:
            payload["customer_email"] = customer_email
        return self._post("/v1/checkouts/", payload, operation="create_checkout")

    # -- customer portal ---------------------------------------------------

    def create_customer_session(self, *, external_customer_id, return_url=None):
        """Mint a customer portal session for one workspace's Polar customer.

        Addressed by external customer id, never by a customer id supplied by a
        caller: a Polar customer id is guessable-shaped and, if it were accepted
        from a request, would be a portal into someone else's billing.
        """
        payload = {"external_customer_id": external_customer_id}
        if return_url:
            payload["return_url"] = return_url
        return self._post("/v1/customer-sessions/", payload,
                          operation="create_customer_session")

    # -- lifecycle reads and immediate revocation ------------------------

    def list_subscriptions(self, *, external_customer_id=None,
                           customer_id=None):
        return self._list("/v1/subscriptions/",
                          external_customer_id=external_customer_id,
                          customer_id=customer_id,
                          operation="list_subscriptions")

    def list_checkouts(self, *, external_customer_id=None, customer_id=None):
        return self._list("/v1/checkouts/",
                          external_customer_id=external_customer_id,
                          customer_id=customer_id,
                          operation="list_checkouts")

    def get_subscription(self, subscription_id):
        return self._get(
            f"/v1/subscriptions/{_validated_id(subscription_id)}",
            operation="get_subscription",
            route_template="/v1/subscriptions/{id}")

    def revoke_subscription(self, subscription_id):
        return self._delete(
            f"/v1/subscriptions/{_validated_id(subscription_id)}",
            operation="revoke_subscription",
            route_template="/v1/subscriptions/{id}")

    def _list(self, path, *, external_customer_id, customer_id, operation):
        if bool(external_customer_id) == bool(customer_id):
            raise ValueError("exactly one customer identity is required")
        key = "external_customer_id" if external_customer_id else "customer_id"
        identity = external_customer_id or customer_id
        if not isinstance(identity, str) or not identity or len(identity) > 255:
            raise ValueError("invalid customer identity")
        items = []
        seen_ids = set()
        expected_pagination = None
        page = 1
        while True:
            query = urllib.parse.urlencode({key: identity, "page": page, "limit": 100})
            document = self._get(f"{path}?{query}", operation=operation)
            page_items = document.get("items")
            pagination = document.get("pagination")
            # Every integrity failure below records the SAME diagnostic set, so
            # one log line says which check fired and on which page, with the
            # counts that made it fire. None of it identifies anybody.
            observed = dict(operation=operation, route_template=path,
                            identity_kind=key, page=page)
            if (not isinstance(page_items, list) or not isinstance(pagination, dict)
                    or not isinstance(pagination.get("total_count"), int)
                    or isinstance(pagination.get("total_count"), bool)
                    or not isinstance(pagination.get("max_page"), int)
                    or isinstance(pagination.get("max_page"), bool)
                    or pagination["total_count"] < 0
                    or pagination["max_page"] < 1
                    or pagination["max_page"] > MAX_LIST_PAGES
                    or any(not isinstance(item, dict) for item in page_items)):
                raise self._integrity_error(
                    "Polar returned an unexpected response.",
                    subtype="malformed_envelope",
                    observed_items=len(items), **observed)
            observed.update(expected_total=pagination["total_count"],
                            expected_max_page=pagination["max_page"])
            current_pagination = (pagination["total_count"], pagination["max_page"])
            if expected_pagination is None:
                expected_pagination = current_pagination
            elif current_pagination != expected_pagination:
                raise self._integrity_error(
                    "Polar returned unstable pagination.",
                    subtype="unstable_pagination",
                    observed_items=len(items), **observed)
            for item in page_items:
                identifier = item.get("id")
                if (not isinstance(identifier, str) or not identifier
                        or len(identifier) > 255 or identifier in seen_ids):
                    raise self._integrity_error(
                        "Polar returned ambiguous pagination.",
                        subtype="duplicate_object",
                        observed_items=len(items), **observed)
                seen_ids.add(identifier)
            items.extend(page_items)
            if page >= pagination["max_page"]:
                if len(items) != pagination["total_count"]:
                    raise self._integrity_error(
                        "Polar returned incomplete pagination.",
                        subtype="incomplete_pagination",
                        observed_items=len(items), **observed)
                return items
            page += 1

    # -- transport ---------------------------------------------------------

    def _post(self, path, payload, *, operation, route_template=None):
        return self._request("POST", path, payload=payload, operation=operation,
                             route_template=route_template)

    def _get(self, path, *, operation, route_template=None):
        return self._request("GET", path, operation=operation,
                             route_template=route_template)

    def _delete(self, path, *, operation, route_template=None):
        return self._request("DELETE", path, operation=operation,
                             route_template=route_template)

    def _observe(self, *, operation, method, route_template, started,
                 status=None, error=None):
        """One structured record per provider call. Never a secret or a body."""
        fields = {
            "operation": operation,
            "http_method": method,
            "route_template": route_template,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "timeout_seconds": self._timeout,
            "http_status": status,
            "outcome": "ok" if error is None else (error.failure_kind or "refused"),
            "retryable": None if error is None else error.retryable,
        }
        if error is None:
            logger.info("polar_provider_call", extra=fields)
        else:
            logger.warning("polar_provider_call_failed", extra=fields)

    def _integrity_error(self, message, *, operation, route_template, subtype,
                         identity_kind=None, page=None, expected_total=None,
                         expected_max_page=None, observed_items=None):
        """Polar answered, but the answer could not be trusted as complete.

        ``subtype`` is a fixed enum, never the message: the message is prose and
        prose drifts. Everything recorded here is a COUNT or an enum -- no
        provider object id, no customer id, no tenant id, no body. `identity_kind`
        names WHICH query was in flight, never the value it carried, because
        "the external_customer_id listing disagreed with the customer_id
        listing" is the shape of a whole class of bug and was previously
        invisible.
        """
        error = PolarAPIError(message, operation=operation,
                              failure_kind="inconsistent",
                              inconsistency_subtype=subtype)
        logger.warning("polar_provider_state_inconsistent", extra={
            "operation": operation,
            "route_template": route_template,
            "outcome": "inconsistent",
            "inconsistency_subtype": subtype,
            "identity_kind": identity_kind,
            "page": page,
            "expected_total": expected_total,
            "expected_max_page": expected_max_page,
            "observed_items": observed_items,
            "retryable": True,
        })
        return error

    def _request(self, method, path, *, payload=None, operation,
                 route_template=None):
        url = f"{self._settings.api_base_url}{path}"
        # Never the URL. The query string carries the tenant id and an
        # id-addressed path carries a provider object id, so a caller that has
        # one supplies a template and the concrete path is not logged.
        route_template = route_template or path.split("?", 1)[0]
        body = (json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if payload is not None else None)
        headers = {
            "Authorization": f"Bearer {self._settings.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "relium-billing",
        }
        started = time.monotonic()
        try:
            status, raw = self._transport(
                method=method, url=url, headers=headers, body=body,
                timeout=self._timeout)
        except PolarAPIError as error:
            self._observe(operation=operation, method=method,
                          route_template=route_template, started=started,
                          status=error.status_code, error=error)
            raise
        except Exception as cause:
            # The cause is deliberately dropped rather than chained: a urllib
            # error's string can contain the full request URL, and this error is
            # rendered into a customer-facing response. Its TYPE is still read,
            # because "the deadline expired" and "the host did not resolve" are
            # different faults and used to be reported as the same one.
            error = PolarAPIError(
                "Polar could not be reached.", operation=operation,
                failure_kind=_transport_failure_kind(cause))
            self._observe(operation=operation, method=method,
                          route_template=route_template, started=started,
                          error=error)
            raise error from None

        def refused(error):
            """Record the fault, then hand it back to be raised."""
            self._observe(operation=operation, method=method,
                          route_template=route_template, started=started,
                          status=status, error=error)
            return error

        if status is None or not 200 <= status < 300:
            provider_code, provider_description = _provider_diagnostic(raw)
            raise refused(PolarAPIError(
                "Polar refused the request.", status_code=status,
                operation=operation, provider_code=provider_code,
                provider_description=provider_description))
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise refused(PolarAPIError(
                "Polar returned an unreadable response.",
                status_code=status, operation=operation)) from None
        if not isinstance(document, dict):
            raise refused(PolarAPIError(
                "Polar returned an unexpected response.",
                status_code=status, operation=operation))
        self._observe(operation=operation, method=method,
                      route_template=route_template, started=started,
                      status=status)
        return document


def _validated_id(value):
    if not isinstance(value, str) or not _POLAR_ID.fullmatch(value):
        raise ValueError("invalid Polar identifier")
    return value


def _urllib_transport(*, method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        # A 4xx is an answer, not a transport failure: the status is what tells
        # a caller whether retrying could ever help. The body is read and
        # discarded so the connection closes cleanly, and is never surfaced.
        try:
            raw = error.read(MAX_RESPONSE_BYTES)
        finally:
            error.close()
        return error.code, raw


def _provider_diagnostic(raw):
    """Allow-listed, bounded diagnostics from a Polar error response."""
    if not isinstance(raw, bytes):
        return None, None
    try:
        value = json.loads(raw[:MAX_RESPONSE_BYTES].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    return (_bounded_text(value.get("error"), 128),
            _bounded_text(value.get("error_description"), 512))


def _bounded_text(value, limit):
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value
