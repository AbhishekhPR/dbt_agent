"""Regression coverage for the operator-only Polar billing preflight."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from agent.billing.config import PolarSettings


STARTER_PRODUCT = "prod_starter"
PRO_PRODUCT = "prod_pro"


def settings(server="sandbox"):
    return PolarSettings(
        access_token="polar_oat_not_real",
        webhook_secret="whsec_not_real",
        starter_product_id=STARTER_PRODUCT,
        pro_product_id=PRO_PRODUCT,
        server=server,
    )


def product(product_id, name, cents, **overrides):
    value = {
        "id": product_id,
        "name": name,
        "is_archived": False,
        "prices": [{
            "id": "price_" + product_id,
            "source": "catalog",
            "amount_type": "fixed",
            "type": "recurring",
            "recurring_interval": "month",
            "price_currency": "usd",
            "price_amount": cents,
            "is_archived": False,
        }],
    }
    value.update(overrides)
    return value


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.products = {
            STARTER_PRODUCT: product(
                STARTER_PRODUCT, "Relium Starter", 14900),
            PRO_PRODUCT: product(PRO_PRODUCT, "Relium Pro", 24900),
        }

    def get_json(self, path):
        return self.products[path.rsplit("/", 1)[-1]]

    def test_accepts_exact_unarchived_monthly_catalog_prices(self):
        from scripts.polar_billing_preflight import validate_catalog

        result = validate_catalog(settings(), get_json=self.get_json)

        self.assertEqual(result, {
            "server": "sandbox",
            "starter": {
                "id": STARTER_PRODUCT, "name": "Relium Starter",
                "monthly_usd_cents": 14900,
            },
            "pro": {
                "id": PRO_PRODUCT, "name": "Relium Pro",
                "monthly_usd_cents": 24900,
            },
        })

    def test_rejects_wrong_amount_cadence_archive_and_duplicate_ids(self):
        from scripts.polar_billing_preflight import PreflightError, validate_catalog

        cases = {
            "wrong amount": product(PRO_PRODUCT, "Relium Pro", 25000),
            "annual": product(PRO_PRODUCT, "Relium Pro", 24900,
                              prices=[{
                                  "source": "catalog", "amount_type": "fixed",
                                  "type": "recurring",
                                  "recurring_interval": "year",
                                  "price_currency": "usd", "price_amount": 24900,
                                  "is_archived": False,
                              }]),
            "archived": product(PRO_PRODUCT, "Relium Pro", 24900,
                                is_archived=True),
        }
        for label, invalid in cases.items():
            with self.subTest(label=label):
                products = dict(self.products)
                products[PRO_PRODUCT] = invalid
                with self.assertRaises(PreflightError):
                    validate_catalog(
                        settings(),
                        get_json=lambda path, products=products:
                            products[path.rsplit("/", 1)[-1]])

        duplicate = settings()
        object.__setattr__(duplicate, "pro_product_id", STARTER_PRODUCT)
        with self.assertRaises(PreflightError):
            validate_catalog(duplicate, get_json=self.get_json)

    def test_rejects_an_expected_environment_mismatch(self):
        from scripts.polar_billing_preflight import PreflightError, run_preflight

        with self.assertRaisesRegex(PreflightError, "expected production"):
            run_preflight(settings("sandbox"), expected_server="production",
                          get_json=self.get_json)


class PortalCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.products = {
            STARTER_PRODUCT: product(
                STARTER_PRODUCT, "Relium Starter", 14900),
            PRO_PRODUCT: product(PRO_PRODUCT, "Relium Pro", 24900),
        }
        self.portal_url = "https://sandbox.polar.sh/portal/secret-token"
        self.posts = []

    def get_json(self, path):
        return self.products[path.rsplit("/", 1)[-1]]

    def post_json(self, path, payload):
        self.posts.append((path, payload))
        return {"customer_portal_url": self.portal_url}

    def test_optional_customer_check_proves_scope_without_printing_portal_url(self):
        from scripts.polar_billing_preflight import run_preflight

        output = io.StringIO()
        with redirect_stdout(output):
            result = run_preflight(
                settings(), expected_server="sandbox",
                customer_external_id="ten_qa_customer",
                get_json=self.get_json, post_json=self.post_json)

        self.assertEqual(self.posts, [
            ("/v1/customer-sessions/",
             {"external_customer_id": "ten_qa_customer"}),
        ])
        self.assertEqual(result["customer_session"], "ok")
        rendered = output.getvalue()
        self.assertIn("customer_session=ok", rendered)
        self.assertNotIn(self.portal_url, rendered)
        self.assertNotIn("ten_qa_customer", rendered)

    def test_a_scope_refusal_reports_only_status_and_safe_provider_code(self):
        from scripts.polar_billing_preflight import (
            PolarHTTPError, PreflightError, run_preflight,
        )

        def refused(_path, _payload):
            raise PolarHTTPError(
                status_code=403, provider_code="insufficient_scope",
                provider_description="sensitive provider prose")

        with self.assertRaises(PreflightError) as caught:
            run_preflight(
                settings(), expected_server="sandbox",
                customer_external_id="ten_qa_customer",
                get_json=self.get_json, post_json=refused)

        message = str(caught.exception)
        self.assertIn("HTTP 403", message)
        self.assertIn("insufficient_scope", message)
        self.assertNotIn("sensitive provider prose", message)
        self.assertNotIn("ten_qa_customer", message)


if __name__ == "__main__":
    unittest.main()
