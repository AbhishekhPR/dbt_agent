"""Read-only validation of a proposed Polar billing configuration.

Product reads never mutate Polar.  The optional customer-session probe creates
only a short-lived hosted portal session for an existing QA customer; it does
not change that customer's subscription or Relium's entitlement state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.billing.config import PolarConfigurationError, PolarSettings
from agent.billing.plans import PLAN_PRO, PLAN_STARTER, plan_for_product


MAX_RESPONSE_BYTES = 512 * 1024
EXPECTED_MONTHLY_USD_CENTS = {PLAN_STARTER: 14900, PLAN_PRO: 24900}


class PreflightError(RuntimeError):
    """A proposed billing configuration is not safe to use."""


class PolarHTTPError(RuntimeError):
    """A bounded, safe projection of a Polar API refusal."""

    def __init__(self, *, status_code=None, provider_code=None,
                 provider_description=None):
        super().__init__("Polar refused the preflight request.")
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_description = provider_description


class PolarReadClient:
    def __init__(self, settings, *, timeout=10.0):
        self._base = settings.api_base_url
        self._token = settings.access_token
        self._timeout = timeout

    def get_json(self, path):
        return self._request("GET", path)

    def post_json(self, path, payload):
        return self._request("POST", path, payload)

    def _request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(
            payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._base + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                **({} if body is None else {"Content-Type": "application/json"}),
                "User-Agent": "relium-billing-preflight",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(MAX_RESPONSE_BYTES)
            finally:
                error.close()
            code, description = _provider_diagnostic(raw)
            raise PolarHTTPError(
                status_code=error.code, provider_code=code,
                provider_description=description) from None
        except Exception:
            raise PolarHTTPError() from None
        if not 200 <= status < 300:
            code, description = _provider_diagnostic(raw)
            raise PolarHTTPError(
                status_code=status, provider_code=code,
                provider_description=description)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise PreflightError("Polar returned unreadable JSON.") from None
        if not isinstance(document, dict):
            raise PreflightError("Polar returned an unexpected response shape.")
        return document


def validate_catalog(settings, *, get_json):
    if settings.starter_product_id == settings.pro_product_id:
        raise PreflightError("Starter and Pro product IDs must differ.")

    result = {"server": settings.server}
    configured = {
        PLAN_STARTER: settings.starter_product_id,
        PLAN_PRO: settings.pro_product_id,
    }
    for plan, product_id in configured.items():
        try:
            product = get_json(f"/v1/products/{product_id}")
        except PolarHTTPError as error:
            raise PreflightError(_safe_http_failure("product read", error)) from None
        if product.get("id") != product_id:
            raise PreflightError(f"{plan} product ID did not match Polar's response.")
        if product.get("is_archived") is not False:
            raise PreflightError(f"{plan} product is archived or has unknown state.")
        if (product.get("is_recurring") is not True
                or product.get("recurring_interval") != "month"
                or product.get("recurring_interval_count") != 1):
            raise PreflightError(
                f"{plan} product must recur exactly once per month.")
        mapped = plan_for_product(
            product_id,
            starter_product_id=settings.starter_product_id,
            pro_product_id=settings.pro_product_id)
        if mapped != plan:
            raise PreflightError(f"{plan} product does not map back to {plan}.")

        prices = [
            price for price in product.get("prices", [])
            if isinstance(price, dict)
            and price.get("source") == "catalog"
            and price.get("amount_type") == "fixed"
            and price.get("price_currency") == "usd"
            and price.get("is_archived") is False
        ]
        expected = EXPECTED_MONTHLY_USD_CENTS[plan]
        if len(prices) != 1 or prices[0].get("price_amount") != expected:
            raise PreflightError(
                f"{plan} must have exactly one active fixed monthly USD catalog "
                f"price at {expected} cents.")
        name = product.get("name")
        if not isinstance(name, str) or not name:
            raise PreflightError(f"{plan} product has no usable name.")
        result[plan] = {
            "id": product_id,
            "name": name,
            "monthly_usd_cents": expected,
        }
    return result


def run_preflight(settings, *, expected_server, customer_external_id=None,
                  get_json=None, post_json=None):
    if settings.server != expected_server:
        raise PreflightError(
            f"Billing preflight expected {expected_server}, but POLAR_SERVER "
            f"selected {settings.server}.")
    client = None
    if get_json is None or (customer_external_id and post_json is None):
        client = PolarReadClient(settings)
    get_json = get_json or client.get_json
    post_json = post_json or (client.post_json if client else None)

    result = validate_catalog(settings, get_json=get_json)
    print(f"server={result['server']}")
    for plan in (PLAN_STARTER, PLAN_PRO):
        entry = result[plan]
        print(
            f"{plan}=ok id={entry['id']} cadence=month currency=usd "
            f"cents={entry['monthly_usd_cents']}")

    if customer_external_id:
        try:
            session = post_json(
                "/v1/customer-sessions/",
                {"external_customer_id": customer_external_id})
        except PolarHTTPError as error:
            raise PreflightError(
                _safe_http_failure("customer session", error)) from None
        if not isinstance(session, dict) or not isinstance(
                session.get("customer_portal_url"), str):
            raise PreflightError(
                "Customer-session probe returned no hosted portal URL.")
        result["customer_session"] = "ok"
        print("customer_session=ok")
    else:
        result["customer_session"] = "skipped"
        print("customer_session=skipped")
    print("webhook_secret=present")
    print("billing_preflight=ok")
    return result


def _provider_diagnostic(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    return (_bounded_text(value.get("error"), 128),
            _bounded_text(value.get("error_description"), 512))


def _bounded_text(value, limit):
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value


def _safe_http_failure(operation, error):
    status = str(error.status_code) if error.status_code is not None else "unknown"
    code = error.provider_code or "unknown"
    return f"Polar {operation} failed: HTTP {status}, provider code {code}."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-server", required=True,
                        choices=("sandbox", "production"))
    parser.add_argument("--customer-external-id")
    args = parser.parse_args(argv)
    try:
        settings = PolarSettings.from_environ(os.environ)
        if settings is None:
            raise PreflightError("Polar billing is not configured.")
        run_preflight(
            settings,
            expected_server=args.expected_server,
            customer_external_id=args.customer_external_id)
    except (PolarConfigurationError, PreflightError) as error:
        print(f"billing_preflight=failed {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
