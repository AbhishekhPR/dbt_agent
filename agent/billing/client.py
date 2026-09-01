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
import urllib.error
import urllib.request

#: Bound on a Polar response. These are small JSON objects; anything larger is
#: not a checkout session and is not going to be parsed into one.
MAX_RESPONSE_BYTES = 512 * 1024


class PolarAPIError(RuntimeError):
    """Polar refused or could not answer.

    ``status_code`` is Polar's, when there was one. The message is ours: a
    provider error body can quote request fields back, and this error is
    rendered to a customer.
    """

    def __init__(self, message, *, status_code=None, operation=None):
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or (
            isinstance(self.status_code, int) and 500 <= self.status_code <= 599)


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

    # -- transport ---------------------------------------------------------

    def _post(self, path, payload, *, operation):
        url = f"{self._settings.api_base_url}{path}"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._settings.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "relium-billing",
        }
        try:
            status, raw = self._transport(
                method="POST", url=url, headers=headers, body=body,
                timeout=self._timeout)
        except PolarAPIError:
            raise
        except Exception:
            # The cause is deliberately dropped rather than chained: a urllib
            # error's string can contain the full request URL, and this error is
            # rendered into a customer-facing response.
            raise PolarAPIError("Polar could not be reached.",
                                operation=operation) from None

        if status is None or not 200 <= status < 300:
            raise PolarAPIError("Polar refused the request.",
                                status_code=status, operation=operation)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise PolarAPIError("Polar returned an unreadable response.",
                                status_code=status, operation=operation) from None
        if not isinstance(document, dict):
            raise PolarAPIError("Polar returned an unexpected response.",
                                status_code=status, operation=operation)
        return document


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
            error.read(MAX_RESPONSE_BYTES)
        finally:
            error.close()
        return error.code, b"{}"
