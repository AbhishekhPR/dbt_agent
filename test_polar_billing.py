"""Polar billing: configuration, plan resolution, signatures, routes, webhooks.

FOUR LAYERS, AND THEY ANSWER DIFFERENT QUESTIONS
------------------------------------------------
  * Configuration and plan policy, which are pure. A partially configured
    deployment must refuse to start, and an unconfigured product must never
    map to a paid plan.

  * Signature verification, against vectors computed here with the same
    construction Polar's SDK uses. A tampered body, a wrong secret, a replayed
    timestamp and a missing header are all refused.

  * The served routes, against a fake store. This is where the tenancy
    assertions live: a caller cannot name a workspace, so a member of one
    workspace has no request shape that reaches another's billing.

  * The same behaviour against a REAL PostgreSQL when one is configured.
    Idempotency under duplicate delivery, refusal of an out-of-order delivery,
    and the refusal to move a subscription between tenants are properties of
    the database's constraints, and a fake cannot demonstrate them.

NO REAL CREDENTIAL, PRODUCT ID OR CHARGE APPEARS HERE. The Polar client is
replaced by a fake transport in every test; nothing in this file opens a socket
to Polar, and no test can create a checkout that could be paid.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ISSUER = "https://billing-test.clerk.accounts.test"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()

TENANT_A = "ten_" + "a" * 32
TENANT_B = "ten_" + "b" * 32

STARTER_PRODUCT = "prod_starter_0000000000000001"
PRO_PRODUCT = "prod_pro_0000000000000000002"
OTHER_PRODUCT = "prod_someone_elses_00000000003"

WEBHOOK_SECRET = "polar-webhook-secret-for-tests"  # pragma: allowlist secret -- test-only literal
ACCESS_TOKEN = "polar_oat_test_token_not_real"  # pragma: allowlist secret -- test-only literal


def _settings(**overrides):
    from agent.billing.config import PolarSettings

    values = {
        "access_token": ACCESS_TOKEN,
        "webhook_secret": WEBHOOK_SECRET,
        "starter_product_id": STARTER_PRODUCT,
        "pro_product_id": PRO_PRODUCT,
        "server": "sandbox",
    }
    values.update(overrides)
    return PolarSettings(**values)


# ------------------------------------------------------------ Clerk forging

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(document) -> str:
    return _b64url(json.dumps(document, separators=(",", ":")).encode("utf-8"))


class _Signer:
    def __init__(self, kid="billing-test-key"):
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537,
                                                    key_size=2048)

    def token(self, **claims):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = {
            "iss": ISSUER,
            "sub": "user_2default",
            "sid": "sess_2default",
            "exp": int((NOW + timedelta(minutes=10)).timestamp()),
            "iat": int(NOW.timestamp()),
        }
        payload.update({k: v for k, v in claims.items() if v is not None})
        header = {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(),
                                          hashes.SHA256())
        return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


class _StubJwks:
    def __init__(self, *signers):
        self._keys = {s.kid: s.private_key.public_key() for s in signers}

    def key_for(self, kid):
        from agent.api.clerk_identity import ClerkVerificationError

        key = self._keys.get(kid)
        if key is None:
            raise ClerkVerificationError("token key is not recognised")
        return key


# ------------------------------------------------------- Polar webhook forging

def sign_delivery(body: bytes, *, delivery_id="msg_test_0001", timestamp=None,
                  secret=WEBHOOK_SECRET):
    """Build the headers Polar would send, with the construction Polar uses.

    Written out rather than imported from the implementation, so a change to
    the implementation cannot make these tests agree with it by construction.
    """
    stamp = str(int(NOW_TS if timestamp is None else timestamp))
    key = base64.b64decode(base64.b64encode(secret.encode("utf-8")) + b"==")
    signed = f"{delivery_id}.{stamp}.".encode("utf-8") + body
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return {
        "webhook-id": delivery_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode("ascii"),
    }


def subscription_payload(*, event="subscription.active", tenant_id=TENANT_A,
                         product_id=STARTER_PRODUCT, status="active",
                         subscription_id="sub_0001", customer_id="cus_0001",
                         cancel_at_period_end=False, current_period_end=None,
                         modified_at="2026-08-27T12:00:00Z", past_due_at=None,
                         external_id=True, metadata_tenant=False):
    customer = {"id": customer_id, "email": "billing@example.test"}
    if external_id:
        customer["external_id"] = tenant_id
    if metadata_tenant:
        customer["metadata"] = {"relium_tenant_id": tenant_id}
    data = {
        "id": subscription_id,
        "status": status,
        "product_id": product_id,
        "customer_id": customer_id,
        "customer": customer,
        "cancel_at_period_end": cancel_at_period_end,
        "current_period_end": current_period_end or "2026-09-27T12:00:00Z",
        "past_due_at": past_due_at,
        "created_at": "2026-08-27T11:00:00Z",
        "modified_at": modified_at,
        "metadata": {"relium_tenant_id": tenant_id} if metadata_tenant else {},
    }
    return {"type": event, "data": data}


# ------------------------------------------------------------- fake Polar API

class FakePolarTransport:
    """Records every request and returns canned Polar responses.

    THE SUITE MUST NEVER REACH POLAR. This is injected in place of the urllib
    transport, so no test can create a real checkout or a real charge.
    """

    def __init__(self, *, checkout_url="https://polar.example.test/checkout/abc",
                 portal_url="https://polar.example.test/portal/abc",
                 status=200):
        self.requests = []
        self.checkout_url = checkout_url
        self.portal_url = portal_url
        self.status = status

    def __call__(self, *, method, url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        self.requests.append({"method": method, "url": url,
                              "headers": headers, "payload": payload})
        if self.status != 200:
            return self.status, b"{}"
        if url.endswith("/v1/checkouts/"):
            return 200, json.dumps(
                {"id": "checkout_0001", "url": self.checkout_url}).encode()
        if url.endswith("/v1/customer-sessions/"):
            return 200, json.dumps(
                {"token": "customer-token-never-disclosed",
                 "customer_portal_url": self.portal_url}).encode()
        raise AssertionError(f"unexpected Polar call: {url}")

    @property
    def last(self):
        return self.requests[-1]


def build_service(*, transport=None, settings=None, app_url="https://app.test",
                  now=NOW):
    from agent.billing.client import PolarClient
    from agent.billing.service import BillingService

    transport = transport or FakePolarTransport()
    resolved = settings or _settings()
    service = BillingService(resolved, PolarClient(resolved, transport=transport),
                             app_url=app_url, clock=lambda: now)
    return service, transport


class PolarClientTests(unittest.TestCase):
    def test_a_refusal_retains_only_safe_provider_diagnostics(self):
        from agent.billing.client import PolarAPIError, PolarClient

        def refused(**_kwargs):
            return 403, json.dumps({
                "error": "insufficient_scope",
                "error_description": (
                    "The request requires higher privileges than provided "
                    "by the access token."),
                "customer_id": "cus_must_not_survive",
            }).encode("utf-8")

        client = PolarClient(_settings(), transport=refused)
        with self.assertRaises(PolarAPIError) as caught:
            client.create_customer_session(external_customer_id=TENANT_A)

        error = caught.exception
        self.assertEqual(error.status_code, 403)
        self.assertEqual(error.provider_code, "insufficient_scope")
        self.assertEqual(
            error.provider_description,
            "The request requires higher privileges than provided by the "
            "access token.")
        rendered = repr(error.__dict__) + str(error)
        self.assertNotIn("cus_must_not_survive", rendered)
        self.assertNotIn(TENANT_A, rendered)
        self.assertNotIn(ACCESS_TOKEN, rendered)

    def test_malformed_or_oversized_diagnostics_are_discarded(self):
        from agent.billing.client import PolarAPIError, PolarClient

        for raw in (
                b"not-json",
                json.dumps({"error": "x" * 129,
                            "error_description": "y" * 513}).encode("utf-8"),
                json.dumps(["insufficient_scope"]).encode("utf-8")):
            with self.subTest(raw=raw[:20]):
                client = PolarClient(
                    _settings(), transport=lambda **_kwargs: (403, raw))
                with self.assertRaises(PolarAPIError) as caught:
                    client.create_customer_session(external_customer_id=TENANT_A)
                self.assertIsNone(caught.exception.provider_code)
                self.assertIsNone(caught.exception.provider_description)


# ------------------------------------------------------------------ fake store

class _FakeConnection:
    """Just enough of a psycopg connection for the webhook route's transaction.

    Snapshots the two tables the billing path writes and restores them if the
    block raises, which is what a ROLLBACK does to them. Nested blocks behave
    like SAVEPOINTs: an inner failure that is caught outside does not undo the
    outer block.
    """

    def __init__(self, store):
        self._store = store

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            billing = {k: dict(v) for k, v in self._store.billing.items()}
            deliveries = dict(self._store.deliveries)
            try:
                yield self
            except BaseException:
                self._store.billing = billing
                self._store.deliveries = deliveries
                raise

        return _tx()


class FakeStore:
    """The billing surface of the store, with the same semantics as 0018.

    Mirrors the PostgreSQL behaviour that route tests depend on: absence of a
    row means free, an unknown tenant is ignored, an older object is stale, and
    one subscription cannot belong to two tenants. The Postgres suite at the
    bottom of this file asserts the same outcomes against the real constraints,
    so a divergence between the two shows up as a failure rather than as a
    passing test that proves nothing.
    """

    def __init__(self, tenants=(TENANT_A, TENANT_B)):
        # The real store runs on an autocommit connection and gets its atomicity
        # from explicit `connection.transaction()` blocks. The webhook route
        # opens one around the delivery claim AND the write, so the fake has to
        # roll both back together or a route test would pass on semantics
        # Postgres does not actually provide.
        self.connection = _FakeConnection(self)
        self.tenants = {}
        for index, tenant_id in enumerate(tenants):
            self.tenants[f"org_{index}"] = {
                "tenant_id": tenant_id,
                "clerk_organization_id": f"org_{index}",
                "organization_name": f"Workspace {index}",
                "current_step": "ready",
                "completed_at": NOW,
            }
        self.billing = {}
        self.deliveries = {}

    # -- what the Clerk authenticator uses
    def tenant_by_clerk_organization(self, clerk_organization_id):
        return self.tenants.get(clerk_organization_id)

    def _tenant_ids(self):
        return {t["tenant_id"] for t in self.tenants.values()}

    # -- billing
    def billing_for_tenant(self, tenant_id):
        record = self.billing.get(tenant_id)
        return dict(record) if record else None

    def tenant_for_polar_customer(self, polar_customer_id):
        for tenant_id, record in self.billing.items():
            if record.get("polar_customer_id") == polar_customer_id:
                return tenant_id
        return None

    def upsert_billing_from_subscription(self, *, tenant_id, polar_customer_id,
                                         polar_subscription_id, polar_product_id,
                                         plan, subscription_status,
                                         current_period_end, cancel_at_period_end,
                                         past_due_at, subscription_modified_at):
        from agent.postgres_lifecycle_store import TenantBillingConflict

        if tenant_id not in self._tenant_ids():
            return "ignored"
        for other, record in self.billing.items():
            if other == tenant_id:
                continue
            if record.get("polar_subscription_id") == polar_subscription_id:
                raise TenantBillingConflict("subscription belongs to another workspace")
            if polar_customer_id and record.get("polar_customer_id") == polar_customer_id:
                raise TenantBillingConflict("customer belongs to another workspace")

        existing = self.billing.get(tenant_id)
        if existing is not None:
            previous = existing.get("subscription_modified_at")
            if (previous is not None and subscription_modified_at is not None
                    and subscription_modified_at < previous):
                return "stale"
        self.billing[tenant_id] = {
            "tenant_id": tenant_id,
            "polar_customer_id": polar_customer_id or (
                existing.get("polar_customer_id") if existing else None),
            "polar_subscription_id": polar_subscription_id,
            "polar_product_id": polar_product_id,
            "plan": plan,
            "subscription_status": subscription_status,
            "current_period_end": current_period_end,
            "cancel_at_period_end": bool(cancel_at_period_end),
            "past_due_at": past_due_at,
            "subscription_modified_at": subscription_modified_at,
        }
        return "applied"

    def record_billing_webhook_delivery(self, *, delivery_id, event_type,
                                        tenant_id=None):
        if delivery_id in self.deliveries:
            return False
        self.deliveries[delivery_id] = event_type
        return True


class FakePool:
    def __init__(self, store):
        self.store = store

    def acquire(self, timeout=30.0):
        from contextlib import contextmanager

        @contextmanager
        def _held():
            yield self.store

        return _held()

    def close(self):
        pass


# =============================================================== configuration

class PolarConfigurationTests(unittest.TestCase):
    def test_no_polar_variables_at_all_leaves_billing_unconfigured(self):
        from agent.billing.config import PolarSettings

        self.assertIsNone(PolarSettings.from_environ({}))

    def test_a_partially_configured_deployment_refuses_to_start(self):
        """Half a billing integration is worse than none: the customer reaches
        checkout and the webhook that grants their plan is never verified."""
        from agent.billing.config import PolarConfigurationError, PolarSettings

        with self.assertRaises(PolarConfigurationError) as caught:
            PolarSettings.from_environ({
                "POLAR_ACCESS_TOKEN": ACCESS_TOKEN,
                "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
            })
        message = str(caught.exception)
        self.assertIn("POLAR_WEBHOOK_SECRET", message)
        self.assertIn("POLAR_PRO_PRODUCT_ID", message)
        self.assertNotIn(ACCESS_TOKEN, message)

    def test_the_two_products_must_differ(self):
        from agent.billing.config import PolarConfigurationError, PolarSettings

        with self.assertRaises(PolarConfigurationError):
            PolarSettings.from_environ({
                "POLAR_ACCESS_TOKEN": ACCESS_TOKEN,
                "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
                "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
                "POLAR_PRO_PRODUCT_ID": STARTER_PRODUCT,
            })

    def test_a_credential_that_is_not_an_organization_token_is_refused(self):
        from agent.billing.config import PolarConfigurationError, PolarSettings

        with self.assertRaises(PolarConfigurationError):
            PolarSettings.from_environ({
                "POLAR_ACCESS_TOKEN": "sk_live_something_else",
                "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
                "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
                "POLAR_PRO_PRODUCT_ID": PRO_PRODUCT,
            })

    def test_sandbox_and_production_are_different_base_urls(self):
        from agent.billing.config import PolarSettings

        base = {
            "POLAR_ACCESS_TOKEN": ACCESS_TOKEN,
            "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
            "POLAR_PRO_PRODUCT_ID": PRO_PRODUCT,
        }
        production = PolarSettings.from_environ(base)
        sandbox = PolarSettings.from_environ({**base, "POLAR_SERVER": "sandbox"})
        self.assertEqual(production.api_base_url, "https://api.polar.sh")
        self.assertEqual(sandbox.api_base_url, "https://sandbox-api.polar.sh")
        self.assertFalse(production.is_sandbox)
        self.assertTrue(sandbox.is_sandbox)

    def test_production_is_the_default_so_sandbox_is_never_reached_by_accident(self):
        from agent.billing.config import PolarSettings

        settings = PolarSettings.from_environ({
            "POLAR_ACCESS_TOKEN": ACCESS_TOKEN,
            "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
            "POLAR_PRO_PRODUCT_ID": PRO_PRODUCT,
        })
        self.assertEqual(settings.server, "production")

    def test_an_unknown_server_name_is_refused(self):
        from agent.billing.config import PolarConfigurationError, PolarSettings

        with self.assertRaises(PolarConfigurationError):
            PolarSettings.from_environ({
                "POLAR_ACCESS_TOKEN": ACCESS_TOKEN,
                "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
                "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
                "POLAR_PRO_PRODUCT_ID": PRO_PRODUCT,
                "POLAR_SERVER": "staging",
            })

    def test_the_grace_period_cannot_outlive_polars_own_retry_schedule(self):
        from agent.billing.config import PolarConfigurationError, PolarSettings

        base = {
            "POLAR_ACCESS_TOKEN": ACCESS_TOKEN,
            "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "POLAR_STARTER_PRODUCT_ID": STARTER_PRODUCT,
            "POLAR_PRO_PRODUCT_ID": PRO_PRODUCT,
        }
        with self.assertRaises(PolarConfigurationError):
            PolarSettings.from_environ({**base, "POLAR_PAST_DUE_GRACE_DAYS": "60"})
        self.assertEqual(
            PolarSettings.from_environ({**base}).past_due_grace, timedelta(0))

    def test_secrets_are_absent_from_the_settings_repr(self):
        """A settings object reaching a log line must not carry the token."""
        text = repr(_settings())
        self.assertNotIn(ACCESS_TOKEN, text)
        self.assertNotIn(WEBHOOK_SECRET, text)


# ================================================================ plan policy

class PlanResolutionTests(unittest.TestCase):
    def _plan(self, product_id):
        from agent.billing.plans import plan_for_product

        return plan_for_product(product_id, starter_product_id=STARTER_PRODUCT,
                                pro_product_id=PRO_PRODUCT)

    def test_the_configured_starter_product_maps_to_starter(self):
        self.assertEqual(self._plan(STARTER_PRODUCT), "starter")

    def test_the_configured_pro_product_maps_to_pro(self):
        self.assertEqual(self._plan(PRO_PRODUCT), "pro")

    def test_an_unconfigured_product_never_maps_to_a_paid_plan(self):
        """A subscription to a product this deployment has not been told about
        is not an entitlement it can honour."""
        for product in (OTHER_PRODUCT, "", None, 12345, STARTER_PRODUCT.upper()):
            self.assertEqual(self._plan(product), "free", product)

    def test_a_plan_is_never_inferred_from_an_amount(self):
        """$149 charged for something else is not Starter."""
        from agent.billing.plans import plan_for_product

        self.assertEqual(
            plan_for_product(OTHER_PRODUCT, starter_product_id=STARTER_PRODUCT,
                             pro_product_id=PRO_PRODUCT),
            "free")

    def test_only_starter_and_pro_can_be_bought(self):
        from agent.billing.plans import PURCHASABLE_PLANS

        self.assertEqual(tuple(PURCHASABLE_PLANS), ("starter", "pro"))


class AccessSemanticsTests(unittest.TestCase):
    def _access(self, status, *, plan="pro", past_due_at=None,
                grace=timedelta(0), now=NOW):
        from agent.billing.plans import access_state

        return access_state(plan=plan, status=status, past_due_at=past_due_at,
                            now=now, past_due_grace=grace)

    def test_active_and_trialing_receive_paid_access(self):
        for status in ("active", "trialing"):
            self.assertEqual(self._access(status), ("pro", True), status)

    def test_a_cancellation_scheduled_for_period_end_keeps_access(self):
        """Polar keeps the subscription `active` until the period actually ends;
        the customer paid for that period. Requesting cancellation must not
        downgrade anybody."""
        self.assertEqual(self._access("active"), ("pro", True))

    def test_a_revoked_or_ended_subscription_loses_paid_access(self):
        for status in ("canceled", "unpaid", "incomplete", "incomplete_expired",
                       "paused"):
            self.assertEqual(self._access(status), ("free", False), status)

    def test_an_unknown_status_grants_nothing(self):
        """The vocabulary belongs to Polar and may grow. A status this code has
        never seen must fail closed."""
        for status in (None, "", "something_new", "ACTIVE", 1):
            self.assertEqual(self._access(status), ("free", False), status)

    def test_past_due_follows_polars_own_default_of_no_grace(self):
        self.assertEqual(
            self._access("past_due", past_due_at=NOW - timedelta(hours=1)),
            ("free", False))

    def test_past_due_keeps_access_inside_a_configured_grace_period(self):
        self.assertEqual(
            self._access("past_due", past_due_at=NOW - timedelta(days=1),
                         grace=timedelta(days=7)),
            ("pro", True))

    def test_past_due_loses_access_once_the_grace_period_has_elapsed(self):
        self.assertEqual(
            self._access("past_due", past_due_at=NOW - timedelta(days=8),
                         grace=timedelta(days=7)),
            ("free", False))

    def test_a_free_workspace_is_never_active(self):
        self.assertEqual(self._access("active", plan="free"), ("free", False))


class WorkspacePlanHelperTests(unittest.TestCase):
    """The single abstraction the rest of the application is meant to use."""

    def setUp(self):
        self.store = FakeStore()
        self.settings = _settings()

    def test_a_workspace_with_no_billing_row_is_free(self):
        from agent.billing.access import get_workspace_plan, workspace_has_paid_access

        self.assertEqual(get_workspace_plan(self.store, TENANT_A, self.settings),
                         "free")
        self.assertFalse(
            workspace_has_paid_access(self.store, TENANT_A, self.settings))

    def test_an_active_pro_subscription_grants_pro(self):
        from agent.billing.access import get_workspace_plan, workspace_has_paid_access

        self.store.billing[TENANT_A] = {
            "plan": "pro", "subscription_status": "active",
            "polar_customer_id": "cus_0001"}
        self.assertEqual(
            get_workspace_plan(self.store, TENANT_A, self.settings, now=NOW), "pro")
        self.assertTrue(workspace_has_paid_access(self.store, TENANT_A,
                                                  self.settings, now=NOW))

    def test_a_revoked_subscription_reports_free_even_though_the_row_says_pro(self):
        """The row is history. The helper answers entitlement."""
        from agent.billing.access import get_workspace_plan

        self.store.billing[TENANT_A] = {
            "plan": "pro", "subscription_status": "canceled",
            "polar_customer_id": "cus_0001"}
        self.assertEqual(
            get_workspace_plan(self.store, TENANT_A, self.settings, now=NOW), "free")

    def test_at_least_orders_the_plans(self):
        from agent.billing.plans import PLAN_PRO, PLAN_STARTER, at_least

        self.assertTrue(at_least(PLAN_PRO, PLAN_STARTER))
        self.assertFalse(at_least(PLAN_STARTER, PLAN_PRO))
        self.assertTrue(at_least(PLAN_STARTER, PLAN_STARTER))
        self.assertFalse(at_least("nonsense", PLAN_STARTER))


# =========================================================== webhook signature

class WebhookSignatureTests(unittest.TestCase):
    def setUp(self):
        self.body = json.dumps(subscription_payload()).encode("utf-8")

    def _verify(self, *, body=None, headers=None, now=NOW_TS,
                secret=WEBHOOK_SECRET):
        from agent.billing.signature import verify

        body = self.body if body is None else body
        return verify(secret=secret, body=body,
                      headers=headers if headers is not None else sign_delivery(body),
                      now=now)

    def test_a_correctly_signed_delivery_verifies_and_returns_its_id(self):
        self.assertEqual(self._verify(), "msg_test_0001")

    def test_a_tampered_body_is_refused(self):
        from agent.billing.signature import SignatureError

        headers = sign_delivery(self.body)
        tampered = self.body.replace(b'"starter', b'"pro')
        with self.assertRaises(SignatureError):
            self._verify(body=tampered + b" ", headers=headers)

    def test_a_signature_made_with_another_secret_is_refused(self):
        from agent.billing.signature import SignatureError

        headers = sign_delivery(self.body, secret="a-different-secret")
        with self.assertRaises(SignatureError):
            self._verify(headers=headers)

    def test_a_delivery_signed_for_another_message_id_is_refused(self):
        """The id is inside the signed content, so it cannot be swapped to
        defeat de-duplication."""
        from agent.billing.signature import SignatureError

        headers = sign_delivery(self.body, delivery_id="msg_test_0001")
        headers["webhook-id"] = "msg_test_0002"
        with self.assertRaises(SignatureError):
            self._verify(headers=headers)

    def test_a_replayed_old_delivery_is_refused(self):
        from agent.billing.signature import SignatureError

        headers = sign_delivery(self.body, timestamp=NOW_TS - 3600)
        with self.assertRaises(SignatureError):
            self._verify(headers=headers)

    def test_a_delivery_from_the_future_is_refused(self):
        from agent.billing.signature import SignatureError

        headers = sign_delivery(self.body, timestamp=NOW_TS + 3600)
        with self.assertRaises(SignatureError):
            self._verify(headers=headers)

    def test_every_required_header_is_required(self):
        from agent.billing.signature import SignatureError

        for missing in ("webhook-id", "webhook-timestamp", "webhook-signature"):
            headers = sign_delivery(self.body)
            headers.pop(missing)
            with self.assertRaises(SignatureError):
                self._verify(headers=headers)

    def test_one_matching_signature_among_several_is_accepted(self):
        """Standard Webhooks permits a space-separated list during rotation."""
        headers = sign_delivery(self.body)
        headers["webhook-signature"] = (
            "v1,AAAA " + headers["webhook-signature"] + " v0,ignored")
        self.assertEqual(self._verify(headers=headers), "msg_test_0001")

    def test_a_non_v1_signature_alone_is_refused(self):
        from agent.billing.signature import SignatureError

        headers = sign_delivery(self.body)
        headers["webhook-signature"] = headers["webhook-signature"].replace(
            "v1,", "v2,")
        with self.assertRaises(SignatureError):
            self._verify(headers=headers)

    def test_the_key_matches_the_construction_polars_sdk_uses(self):
        """Polar base64-encodes the secret; Standard Webhooks decodes it. The
        key is therefore the secret's own bytes, and this pins that."""
        from agent.billing.signature import verify

        key = WEBHOOK_SECRET.encode("utf-8")
        stamp = str(int(NOW_TS))
        signed = f"id_1.{stamp}.".encode("utf-8") + self.body
        digest = hmac.new(key, signed, hashlib.sha256).digest()
        headers = {
            "webhook-id": "id_1",
            "webhook-timestamp": stamp,
            "webhook-signature": "v1," + base64.b64encode(digest).decode("ascii"),
        }
        self.assertEqual(
            verify(secret=WEBHOOK_SECRET, body=self.body, headers=headers,
                   now=NOW_TS),
            "id_1")


# ================================================================ checkout

class CheckoutCreationTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.service, self.transport = build_service()

    def test_starter_sells_the_configured_starter_product(self):
        result = self.service.create_checkout(self.store, TENANT_A, "starter")
        self.assertEqual(self.transport.last["payload"]["products"],
                         [STARTER_PRODUCT])
        self.assertEqual(result["plan"], "starter")

    def test_pro_sells_the_configured_pro_product(self):
        self.service.create_checkout(self.store, TENANT_A, "pro")
        self.assertEqual(self.transport.last["payload"]["products"], [PRO_PRODUCT])

    def test_the_workspace_is_the_external_customer_and_is_in_the_metadata(self):
        """Two independent server-set paths from a future webhook back to this
        workspace."""
        self.service.create_checkout(self.store, TENANT_A, "pro")
        payload = self.transport.last["payload"]
        self.assertEqual(payload["external_customer_id"], TENANT_A)
        self.assertEqual(payload["metadata"]["relium_tenant_id"], TENANT_A)
        self.assertEqual(payload["customer_metadata"]["relium_tenant_id"], TENANT_A)

    def test_the_success_url_returns_to_the_configured_dashboard(self):
        self.service.create_checkout(self.store, TENANT_A, "starter")
        self.assertEqual(self.transport.last["payload"]["success_url"],
                         "https://app.test/settings?section=billing&billing=success")

    def test_an_unknown_plan_is_refused_before_any_polar_call(self):
        from agent.billing.service import BillingError

        for plan in ("free", "enterprise", "", None, {"plan": "pro"}, "PRO"):
            with self.assertRaises(BillingError) as caught:
                self.service.create_checkout(self.store, TENANT_A, plan)
            self.assertEqual(caught.exception.code, "unknown_plan")
        self.assertEqual(self.transport.requests, [])

    def test_the_access_token_goes_to_polar_and_nowhere_else(self):
        self.service.create_checkout(self.store, TENANT_A, "starter")
        request = self.transport.last
        self.assertTrue(request["url"].startswith("https://sandbox-api.polar.sh"))
        self.assertEqual(request["headers"]["Authorization"],
                         f"Bearer {ACCESS_TOKEN}")
        self.assertNotIn(ACCESS_TOKEN, json.dumps(request["payload"]))

    def test_a_workspace_that_already_subscribes_cannot_buy_a_second_time(self):
        """Polar checkout creates a NEW subscription. A Starter customer who
        reached checkout for Pro would be paying for both, and only one of them
        would be recorded here."""
        from agent.billing.service import BillingError

        self.store.billing[TENANT_A] = {
            "tenant_id": TENANT_A, "plan": "starter",
            "subscription_status": "active", "polar_customer_id": "cus_0001",
            "polar_subscription_id": "sub_0001", "cancel_at_period_end": False,
            "current_period_end": None, "past_due_at": None}
        with self.assertRaises(BillingError) as caught:
            self.service.create_checkout(self.store, TENANT_A, "pro")
        self.assertEqual(caught.exception.code, "subscription_exists")
        self.assertEqual(self.transport.requests, [])

    def test_a_workspace_whose_subscription_ended_can_buy_again(self):
        """The refusal is about a LIVE subscription, not about ever having had
        one. A customer whose plan was revoked must be able to come back."""
        self.store.billing[TENANT_A] = {
            "tenant_id": TENANT_A, "plan": "pro",
            "subscription_status": "canceled", "polar_customer_id": "cus_0001",
            "polar_subscription_id": "sub_0001", "cancel_at_period_end": False,
            "current_period_end": None, "past_due_at": None}
        result = self.service.create_checkout(self.store, TENANT_A, "pro")
        self.assertEqual(result["plan"], "pro")
        self.assertEqual(self.transport.last["payload"]["external_customer_id"],
                         TENANT_A)

    def _live(self, status, **overrides):
        record = {
            "tenant_id": TENANT_A, "plan": "starter",
            "subscription_status": status, "polar_customer_id": "cus_0001",
            "polar_subscription_id": "sub_0001", "cancel_at_period_end": False,
            "current_period_end": None, "past_due_at": None}
        record.update(overrides)
        self.store.billing[TENANT_A] = record

    def test_a_past_due_workspace_cannot_be_sold_a_second_subscription(self):
        """`past_due` grants no access, and is still a LIVE subscription.

        Polar is inside its 21-day retry schedule and the subscription returns
        to `active` the moment one of those retries succeeds. Selling another
        one here — which the entitlement check alone would have allowed, since
        the default grace is zero — leaves the customer paying twice, with the
        older subscription invisible to Relium and still charging.
        """
        from agent.billing.service import BillingError

        self._live("past_due", past_due_at=NOW - timedelta(days=1))
        with self.assertRaises(BillingError) as caught:
            self.service.create_checkout(self.store, TENANT_A, "pro")
        self.assertEqual(caught.exception.code, "subscription_exists")
        self.assertEqual(self.transport.requests, [])

    def test_a_paused_workspace_cannot_be_sold_a_second_subscription(self):
        """A paused subscription resumes and charges immediately."""
        from agent.billing.service import BillingError

        self._live("paused")
        with self.assertRaises(BillingError) as caught:
            self.service.create_checkout(self.store, TENANT_A, "starter")
        self.assertEqual(caught.exception.code, "subscription_exists")
        self.assertEqual(self.transport.requests, [])

    def test_a_terminal_subscription_does_not_block_buying_again(self):
        """The refusal must not strand a customer whose subscription ended.

        Polar is finished with every status here, so there is nothing left that
        could charge and nothing to update through the portal.
        """
        for status in ("canceled", "unpaid", "incomplete", "incomplete_expired"):
            with self.subTest(status=status):
                self.transport.requests.clear()
                self._live(status)
                result = self.service.create_checkout(self.store, TENANT_A, "pro")
                self.assertEqual(result["plan"], "pro")
                self.assertEqual(len(self.transport.requests), 1)

    def test_a_polar_failure_becomes_a_provider_error_without_leaking_detail(self):
        from agent.billing.service import BillingError

        service, _ = build_service(transport=FakePolarTransport(status=500))
        with self.assertRaises(BillingError) as caught:
            service.create_checkout(self.store, TENANT_A, "starter")
        self.assertEqual(caught.exception.code, "billing_provider_unavailable")

    def test_a_response_without_a_url_is_not_reported_as_success(self):
        from agent.billing.service import BillingError

        class _NoUrl(FakePolarTransport):
            def __call__(self, **kwargs):
                super().__call__(**kwargs)
                return 200, b'{"id": "checkout_1"}'

        service, _ = build_service(transport=_NoUrl())
        with self.assertRaises(BillingError):
            service.create_checkout(self.store, TENANT_A, "starter")


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.service, self.transport = build_service()

    def test_a_workspace_with_no_polar_customer_has_no_portal(self):
        from agent.billing.service import BillingError

        with self.assertRaises(BillingError) as caught:
            self.service.create_portal_session(self.store, TENANT_A)
        self.assertEqual(caught.exception.code, "no_billing_account")
        self.assertEqual(self.transport.requests, [])

    def test_the_portal_is_addressed_by_the_workspaces_own_external_id(self):
        self.store.billing[TENANT_A] = {"polar_customer_id": "cus_0001",
                                        "plan": "pro",
                                        "subscription_status": "active"}
        result = self.service.create_portal_session(self.store, TENANT_A)
        self.assertEqual(self.transport.last["payload"]["external_customer_id"],
                         TENANT_A)
        self.assertEqual(result, {"portal_url": "https://polar.example.test/portal/abc"})

    def test_the_customer_access_token_is_never_returned(self):
        self.store.billing[TENANT_A] = {"polar_customer_id": "cus_0001",
                                        "plan": "pro",
                                        "subscription_status": "active"}
        result = self.service.create_portal_session(self.store, TENANT_A)
        self.assertEqual(set(result), {"portal_url"})

    def test_a_provider_refusal_logs_the_safe_code_and_preserves_starter(self):
        from agent.billing.service import BillingError

        starter = {"polar_customer_id": "cus_0001", "plan": "starter",
                   "subscription_status": "active"}
        self.store.billing[TENANT_A] = dict(starter)

        def refused(**_kwargs):
            return 403, json.dumps({
                "error": "insufficient_scope",
                "error_description": "secret-looking provider prose",
                "customer_id": "cus_must_not_be_logged",
            }).encode("utf-8")

        service, _ = build_service(transport=refused)
        with self.assertLogs("agent.billing.service", level="ERROR") as logs:
            with self.assertRaises(BillingError) as caught:
                service.create_portal_session(self.store, TENANT_A)

        self.assertEqual(caught.exception.code, "billing_provider_unavailable")
        self.assertEqual(self.store.billing[TENANT_A], starter)
        record = logs.records[0]
        self.assertEqual(record.operation, "create_customer_session")
        self.assertEqual(record.http_status, 403)
        self.assertEqual(record.provider_code, "insufficient_scope")
        rendered = record.getMessage() + repr(record.__dict__)
        self.assertNotIn("secret-looking provider prose", rendered)
        self.assertNotIn("cus_must_not_be_logged", rendered)
        self.assertNotIn(TENANT_A, rendered)
        self.assertNotIn(ACCESS_TOKEN, rendered)


# ============================================================ webhook handling

class WebhookApplicationTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.service, _ = build_service()

    def _apply(self, document):
        return self.service.apply_subscription_event(
            self.store, document["type"], document["data"])

    def test_a_starter_subscription_maps_the_workspace_to_starter(self):
        self.assertEqual(self._apply(subscription_payload()), "applied")
        self.assertEqual(self.store.billing[TENANT_A]["plan"], "starter")

    def test_a_pro_subscription_maps_the_workspace_to_pro(self):
        self.assertEqual(
            self._apply(subscription_payload(product_id=PRO_PRODUCT)), "applied")
        self.assertEqual(self.store.billing[TENANT_A]["plan"], "pro")

    def test_an_unknown_product_is_recorded_but_grants_no_paid_plan(self):
        from agent.billing.access import workspace_has_paid_access

        self.assertEqual(
            self._apply(subscription_payload(product_id=OTHER_PRODUCT)), "applied")
        record = self.store.billing[TENANT_A]
        self.assertEqual(record["plan"], "free")
        self.assertEqual(record["polar_product_id"], OTHER_PRODUCT)
        self.assertFalse(workspace_has_paid_access(self.store, TENANT_A,
                                                   self.service.settings, now=NOW))

    def test_the_workspace_is_resolved_from_metadata_when_no_external_id_is_set(self):
        self.assertEqual(
            self._apply(subscription_payload(external_id=False,
                                             metadata_tenant=True)),
            "applied")
        self.assertIn(TENANT_A, self.store.billing)

    def test_a_delivery_naming_no_workspace_is_ignored_rather_than_guessed(self):
        """An email address is present on every payload and is never consulted."""
        self.assertEqual(
            self._apply(subscription_payload(external_id=False)), "ignored")
        self.assertEqual(self.store.billing, {})

    def test_a_delivery_naming_an_unknown_workspace_creates_nothing(self):
        unknown = "ten_" + "f" * 32
        self.assertEqual(
            self._apply(subscription_payload(tenant_id=unknown)), "ignored")
        self.assertEqual(self.store.billing, {})

    def test_a_value_that_is_not_a_relium_tenant_id_is_not_treated_as_one(self):
        for forged in ("../ten_x", "ten_notlongenough", "org_2acme", 42, None):
            self.assertEqual(
                self._apply(subscription_payload(external_id=True,
                                                 tenant_id=forged)),
                "ignored", forged)

    def test_applying_the_same_event_twice_leaves_the_same_state(self):
        document = subscription_payload()
        self.assertEqual(self._apply(document), "applied")
        first = dict(self.store.billing[TENANT_A])
        self.assertEqual(self._apply(document), "applied")
        self.assertEqual(self.store.billing[TENANT_A], first)

    def test_an_out_of_order_delivery_does_not_restore_an_ended_plan(self):
        """A retried `subscription.created` arriving after `subscription.revoked`
        must not hand the plan back."""
        self._apply(subscription_payload(
            event="subscription.revoked", status="canceled",
            modified_at="2026-08-27T13:00:00Z"))
        self.assertEqual(
            self._apply(subscription_payload(
                event="subscription.created", status="active",
                modified_at="2026-08-27T12:00:00Z")),
            "stale")
        self.assertEqual(self.store.billing[TENANT_A]["subscription_status"],
                         "canceled")

    def test_a_cancellation_scheduled_for_period_end_is_recorded_without_downgrade(self):
        self._apply(subscription_payload(
            event="subscription.canceled", status="active",
            cancel_at_period_end=True))
        view = self.service.view_for_record(self.store.billing_for_tenant(TENANT_A))
        self.assertEqual(view["plan"], "starter")
        self.assertTrue(view["is_active"])
        self.assertTrue(view["cancel_at_period_end"])

    def test_revocation_removes_paid_access(self):
        self._apply(subscription_payload())
        self._apply(subscription_payload(
            event="subscription.revoked", status="canceled",
            modified_at="2026-08-27T14:00:00Z"))
        view = self.service.view_for_record(self.store.billing_for_tenant(TENANT_A))
        self.assertEqual(view["plan"], "free")
        self.assertFalse(view["is_active"])

    def test_one_subscription_cannot_be_moved_to_another_workspace(self):
        from agent.postgres_lifecycle_store import TenantBillingConflict

        self._apply(subscription_payload(tenant_id=TENANT_A))
        with self.assertRaises(TenantBillingConflict):
            self._apply(subscription_payload(tenant_id=TENANT_B))
        self.assertNotIn(TENANT_B, self.store.billing)

    def test_the_polar_customer_id_is_recorded_on_the_workspace(self):
        self._apply(subscription_payload(customer_id="cus_9999"))
        self.assertEqual(self.store.billing[TENANT_A]["polar_customer_id"],
                         "cus_9999")


# ================================================================ served routes

def build_app(store, *, service=None, verifier_signer=None):
    from starlette.testclient import TestClient

    from agent.api.clerk_identity import ClerkSettings, ClerkVerifier
    from agent.github_app.http_app import create_http_app

    signer = verifier_signer or _Signer()
    verifier = ClerkVerifier(
        ClerkSettings(issuer=ISSUER, jwks_url=f"{ISSUER}/jwks"),
        jwks=_StubJwks(signer), clock=lambda: NOW)
    app = create_http_app(
        webhook_secret="github-webhook-secret-for-tests",
        job_queue=_StubQueue(),
        max_body_bytes=1024 * 1024,
        shutdown_timeout_seconds=1.0,
        clock=lambda: NOW_TS,
        store_pool=FakePool(store),
        clerk_verifier=verifier,
        billing_service=service,
    )
    return TestClient(app), signer


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


class BillingRouteTests(unittest.TestCase):
    """The tenancy boundary, over the real application and the real routes."""

    def setUp(self):
        self.store = FakeStore()
        self.service, self.transport = build_service()
        self.client, self.signer = build_app(self.store, service=self.service)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _auth(self, organization="org_0"):
        return {"Authorization": f"Bearer {self.signer.token(org_id=organization)}"}

    # -- authentication ---------------------------------------------------

    def test_checkout_without_a_credential_is_unauthorized(self):
        response = self.client.post("/api/billing/checkout", json={"plan": "pro"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    def test_status_and_portal_without_a_credential_are_unauthorized(self):
        self.assertEqual(self.client.get("/api/billing/subscription").status_code, 401)
        self.assertEqual(self.client.post("/api/billing/portal").status_code, 401)

    def test_a_forged_token_is_unauthorized(self):
        forged = _Signer(kid="not-our-clerk").token(org_id="org_0")
        response = self.client.post(
            "/api/billing/checkout", json={"plan": "pro"},
            headers={"Authorization": f"Bearer {forged}"})
        self.assertEqual(response.status_code, 401)

    def test_a_session_with_no_organization_cannot_reach_billing(self):
        response = self.client.get(
            "/api/billing/subscription",
            headers={"Authorization": f"Bearer {self.signer.token()}"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "clerk_organization_required")

    # -- input trust ------------------------------------------------------

    def test_an_invalid_plan_is_refused(self):
        response = self.client.post("/api/billing/checkout",
                                    json={"plan": "enterprise"},
                                    headers=self._auth())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "unknown_plan")
        self.assertEqual(self.transport.requests, [])

    def test_a_caller_cannot_supply_a_product_id_amount_or_customer(self):
        """Everything but `plan` is ignored, so there is no request that buys
        something Relium did not put on sale."""
        response = self.client.post("/api/billing/checkout", headers=self._auth(),
                                    json={
                                        "plan": "starter",
                                        "product_id": OTHER_PRODUCT,
                                        "products": [OTHER_PRODUCT],
                                        "amount": 1,
                                        "customer_id": "cus_attacker",
                                        "external_customer_id": TENANT_B,
                                        "tenant_id": TENANT_B,
                                        "success_url": "https://evil.test/",
                                    })
        self.assertEqual(response.status_code, 200)
        payload = self.transport.last["payload"]
        self.assertEqual(payload["products"], [STARTER_PRODUCT])
        self.assertEqual(payload["external_customer_id"], TENANT_A)
        self.assertNotIn("amount", payload)
        self.assertNotIn("customer_id", payload)
        self.assertTrue(payload["success_url"].startswith("https://app.test/"))

    def test_a_second_checkout_is_refused_as_a_conflict_not_an_error(self):
        self.store.billing[TENANT_A] = {
            "tenant_id": TENANT_A, "plan": "starter",
            "subscription_status": "active", "polar_customer_id": "cus_0001",
            "polar_subscription_id": "sub_0001", "cancel_at_period_end": False,
            "current_period_end": None, "past_due_at": None}
        response = self.client.post("/api/billing/checkout",
                                    json={"plan": "pro"}, headers=self._auth())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "subscription_exists")
        self.assertEqual(self.transport.requests, [])

    def test_the_checkout_response_carries_only_the_url_and_the_plan(self):
        response = self.client.post("/api/billing/checkout",
                                    json={"plan": "pro"}, headers=self._auth())
        body = response.json()
        self.assertEqual(body["checkout_url"], "https://polar.example.test/checkout/abc")
        self.assertEqual(body["plan"], "pro")
        self.assertNotIn("access_token", body)
        self.assertNotIn(ACCESS_TOKEN, json.dumps(body))

    # -- cross-workspace isolation ---------------------------------------

    def test_a_member_of_one_workspace_bills_only_their_own(self):
        """There is no field in the request that names a workspace, so the
        checkout is bound to the tenant in the token and to nothing else."""
        self.client.post("/api/billing/checkout", json={"plan": "pro"},
                         headers=self._auth("org_0"))
        self.assertEqual(self.transport.last["payload"]["external_customer_id"],
                         TENANT_A)
        self.client.post("/api/billing/checkout", json={"plan": "pro"},
                         headers=self._auth("org_1"))
        self.assertEqual(self.transport.last["payload"]["external_customer_id"],
                         TENANT_B)

    def test_billing_status_is_scoped_to_the_callers_own_workspace(self):
        self.store.billing[TENANT_B] = {
            "tenant_id": TENANT_B, "plan": "pro", "subscription_status": "active",
            "polar_customer_id": "cus_b", "polar_subscription_id": "sub_b",
            "cancel_at_period_end": False, "current_period_end": None,
            "past_due_at": None}
        mine = self.client.get("/api/billing/subscription",
                               headers=self._auth("org_0")).json()
        self.assertEqual(mine["plan"], "free")
        self.assertFalse(mine["is_active"])
        theirs = self.client.get("/api/billing/subscription",
                                 headers=self._auth("org_1")).json()
        self.assertEqual(theirs["plan"], "pro")

    def test_the_portal_cannot_be_opened_for_another_workspaces_customer(self):
        self.store.billing[TENANT_B] = {
            "tenant_id": TENANT_B, "plan": "pro", "subscription_status": "active",
            "polar_customer_id": "cus_b"}
        response = self.client.post(
            "/api/billing/portal", headers=self._auth("org_0"),
            json={"customer_id": "cus_b", "external_customer_id": TENANT_B,
                  "tenant_id": TENANT_B})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_billing_account")
        self.assertEqual(self.transport.requests, [])

    def test_the_portal_uses_the_callers_own_external_id(self):
        self.store.billing[TENANT_A] = {
            "tenant_id": TENANT_A, "plan": "pro", "subscription_status": "active",
            "polar_customer_id": "cus_a"}
        response = self.client.post("/api/billing/portal",
                                    headers=self._auth("org_0"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.last["payload"]["external_customer_id"],
                         TENANT_A)

    # -- status disclosure -------------------------------------------------

    def test_the_status_response_discloses_no_polar_identifiers(self):
        self.store.billing[TENANT_A] = {
            "tenant_id": TENANT_A, "plan": "pro", "subscription_status": "active",
            "polar_customer_id": "cus_a", "polar_subscription_id": "sub_a",
            "polar_product_id": PRO_PRODUCT, "cancel_at_period_end": False,
            "current_period_end": NOW + timedelta(days=20), "past_due_at": None}
        body = self.client.get("/api/billing/subscription",
                               headers=self._auth("org_0")).json()
        self.assertEqual(set(body) - {"request_id"}, {
            "plan", "status", "is_active", "cancel_at_period_end",
            "current_period_end", "has_billing_account", "entitlements",
            "repository_count"})
        text = json.dumps(body)
        for secret in ("cus_a", "sub_a", PRO_PRODUCT):
            self.assertNotIn(secret, text)

        # The entitlement object is capabilities and nothing else. It is the
        # newest thing in this response and therefore the likeliest place for
        # an identifier to be added by accident later.
        self.assertEqual(set(body["entitlements"]), {
            "repository_limit", "member_limit", "history_retention_days",
            "warehouse_evidence", "runtime_evidence", "custom_review_policies",
            "merge_blocking", "governance_controls"})
        for value in body["entitlements"].values():
            self.assertIsInstance(value, (bool, int, type(None)))

    # -- the return URL grants nothing ------------------------------------

    def test_returning_from_checkout_does_not_grant_a_plan(self):
        """The success URL is a place to wait. Requesting the status with any
        query string at all still reports what the database says."""
        for query in ("?billing=success", "?billing=success&plan=pro",
                      "?checkout_id=checkout_0001"):
            body = self.client.get(f"/api/billing/subscription{query}",
                                   headers=self._auth("org_0")).json()
            self.assertEqual(body["plan"], "free", query)
            self.assertFalse(body["is_active"], query)

    # -- unconfigured deployment ------------------------------------------

    def test_billing_routes_are_served_and_answer_503_without_polar(self):
        client, signer = build_app(FakeStore(), service=None)
        with client:
            for method, path in (("post", "/api/billing/checkout"),
                                 ("get", "/api/billing/subscription"),
                                 ("post", "/api/billing/portal"),
                                 ("post", "/api/billing/webhooks/polar")):
                response = getattr(client, method)(
                    path, headers={"Authorization": f"Bearer {signer.token(org_id='org_0')}"})
                self.assertEqual(response.status_code, 503, path)
                self.assertEqual(response.json()["code"], "billing_not_configured")


class WebhookRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.service, self.transport = build_service()
        self.client, _ = build_app(self.store, service=self.service)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _post(self, document, *, delivery_id="msg_0001", headers=None,
              timestamp=None, secret=WEBHOOK_SECRET):
        body = json.dumps(document).encode("utf-8")
        signed = headers if headers is not None else sign_delivery(
            body, delivery_id=delivery_id, timestamp=timestamp, secret=secret)
        return self.client.post("/api/billing/webhooks/polar", content=body,
                                headers={**signed, "Content-Type": "application/json"})

    def test_a_valid_delivery_is_accepted_and_applied(self):
        response = self._post(subscription_payload())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "applied")
        self.assertEqual(self.store.billing[TENANT_A]["plan"], "starter")

    def test_an_unsigned_delivery_is_refused_and_changes_nothing(self):
        response = self.client.post(
            "/api/billing/webhooks/polar",
            content=json.dumps(subscription_payload()).encode(),
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.billing, {})
        self.assertEqual(self.store.deliveries, {})

    def test_an_invalid_signature_is_refused_and_changes_nothing(self):
        response = self._post(subscription_payload(), secret="not-the-secret")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.billing, {})
        self.assertEqual(self.store.deliveries, {})

    def test_a_signature_over_a_different_body_is_refused(self):
        """The body actually delivered is the body that must have been signed."""
        signed_body = json.dumps(subscription_payload()).encode()
        headers = sign_delivery(signed_body)
        forged = json.dumps(subscription_payload(product_id=PRO_PRODUCT)).encode()
        response = self.client.post("/api/billing/webhooks/polar", content=forged,
                                    headers=headers)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.billing, {})

    def test_a_replayed_delivery_is_a_no_op(self):
        document = subscription_payload()
        self.assertEqual(self._post(document).json()["status"], "applied")
        before = dict(self.store.billing[TENANT_A])
        second = self._post(document)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(self.store.billing[TENANT_A], before)

    def test_a_replayed_delivery_cannot_undo_a_later_one(self):
        """The classic corruption: an old `active` replayed after a revocation."""
        self._post(subscription_payload(), delivery_id="msg_0001")
        self._post(subscription_payload(event="subscription.revoked",
                                        status="canceled",
                                        modified_at="2026-08-27T14:00:00Z"),
                   delivery_id="msg_0002")
        self._post(subscription_payload(), delivery_id="msg_0001")
        self.assertEqual(self.store.billing[TENANT_A]["subscription_status"],
                         "canceled")

    def test_a_failed_write_leaves_the_delivery_unclaimed_so_a_retry_works(self):
        """A transient database failure must not consume the delivery.

        The store runs on an autocommit connection, so claiming the delivery in
        one statement and writing in the next would COMMIT the claim before the
        write was attempted. A failure would then answer 503, Polar would retry,
        and the retry would find the delivery already recorded and skip it as a
        duplicate — losing the subscription change permanently and leaving a
        paying customer on the wrong plan with nothing left to retry.

        The claim and the write share one transaction, so a failure rolls back
        both and Polar's retry gets a clean attempt.
        """
        document = subscription_payload()
        calls = []
        real = self.store.upsert_billing_from_subscription

        def failing_once(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("database went away mid-write")
            return real(**kwargs)

        self.store.upsert_billing_from_subscription = failing_once

        first = self._post(document, delivery_id="msg_retry")
        self.assertEqual(first.status_code, 503)
        # The rollback took the claim with it.
        self.assertEqual(self.store.deliveries, {})
        self.assertEqual(self.store.billing, {})

        second = self._post(document, delivery_id="msg_retry")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["status"], "applied")
        self.assertEqual(self.store.billing[TENANT_A]["plan"], "starter")

    def test_a_cross_tenant_conflict_still_consumes_the_delivery(self):
        """The one failure a retry cannot fix must NOT be retried forever.

        A subscription owned by another workspace conflicts identically on every
        delivery. The write is rolled back, so the claim is re-made on its own
        to stop Polar retrying a delivery that can never succeed.
        """
        self.store.billing[TENANT_B] = {
            "tenant_id": TENANT_B, "plan": "starter",
            "subscription_status": "active", "polar_customer_id": "cus_other",
            "polar_subscription_id": "sub_0001", "cancel_at_period_end": False,
            "current_period_end": None, "past_due_at": None,
            "subscription_modified_at": None}

        response = self._post(subscription_payload(), delivery_id="msg_conflict")
        self.assertEqual(response.status_code, 409)
        self.assertIn("msg_conflict", self.store.deliveries)
        # TENANT_A was never given the other workspace's subscription.
        self.assertNotIn(TENANT_A, self.store.billing)

        replay = self._post(subscription_payload(), delivery_id="msg_conflict")
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["status"], "duplicate")

    def test_an_expired_timestamp_is_refused(self):
        response = self._post(subscription_payload(), timestamp=NOW_TS - 3600)
        self.assertEqual(response.status_code, 401)

    def test_a_non_subscription_event_is_acknowledged_and_ignored(self):
        for event in ("order.paid", "customer.updated", "benefit_grant.revoked"):
            response = self._post({"type": event, "data": {"id": "x"}},
                                  delivery_id=f"msg_{event}")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "ignored")
        self.assertEqual(self.store.billing, {})

    def test_an_unparseable_body_with_a_valid_signature_is_a_bad_request(self):
        body = b"not json at all"
        headers = sign_delivery(body)
        response = self.client.post("/api/billing/webhooks/polar", content=body,
                                    headers=headers)
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_product_delivery_never_grants_paid_access(self):
        self._post(subscription_payload(product_id=OTHER_PRODUCT))
        self.assertEqual(self.store.billing[TENANT_A]["plan"], "free")

    def test_the_webhook_secret_is_not_echoed_in_any_response(self):
        for response in (self._post(subscription_payload(), secret="wrong"),
                         self._post(subscription_payload(),
                                    delivery_id="msg_ok")):
            self.assertNotIn(WEBHOOK_SECRET, response.text)


# ================================================================== secrets

class SecretContainmentTests(unittest.TestCase):
    """Nothing carrying a secret may reach a log record or a response body.

    A billing integration leaks its credential through a log line long before it
    leaks it through an endpoint, and a leaked organization access token is
    authority over every customer's subscription in the Polar organization.
    """

    def setUp(self):
        self.store = FakeStore()
        self.service, self.transport = build_service()
        self.client, self.signer = build_app(self.store, service=self.service)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _captured(self, fn):
        """Every log record emitted while `fn` runs, rendered as text.

        Both the message AND every `extra` field are rendered, because the
        server's JSON formatter emits allow-listed extras and a secret smuggled
        into one would reach production logs without appearing in the message.
        """
        import logging

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        root = logging.getLogger()
        previous, previous_level = root.handlers[:], root.level
        root.handlers = [handler]
        root.setLevel(logging.DEBUG)
        try:
            fn()
        finally:
            root.handlers, root.level = previous, previous_level
        return chr(10).join(
            record.getMessage() + " " + repr(record.__dict__) for record in records)

    def test_no_secret_reaches_a_log_record_on_a_failing_checkout(self):
        service, _ = build_service(transport=FakePolarTransport(status=500))
        client, signer = build_app(self.store, service=service)
        with client:
            text = self._captured(lambda: client.post(
                "/api/billing/checkout", json={"plan": "pro"},
                headers={"Authorization": f"Bearer {signer.token(org_id='org_0')}"}))
        self.assertNotIn(ACCESS_TOKEN, text)
        self.assertNotIn(WEBHOOK_SECRET, text)

    def test_no_secret_reaches_a_log_record_on_a_refused_webhook(self):
        body = json.dumps(subscription_payload()).encode()
        text = self._captured(lambda: self.client.post(
            "/api/billing/webhooks/polar", content=body,
            headers=sign_delivery(body, secret="the-wrong-secret")))
        self.assertNotIn(WEBHOOK_SECRET, text)
        self.assertNotIn(ACCESS_TOKEN, text)

    def test_no_secret_reaches_a_log_record_on_an_accepted_webhook(self):
        body = json.dumps(subscription_payload()).encode()
        text = self._captured(lambda: self.client.post(
            "/api/billing/webhooks/polar", content=body,
            headers=sign_delivery(body)))
        self.assertNotIn(WEBHOOK_SECRET, text)
        self.assertNotIn(ACCESS_TOKEN, text)

    def test_no_billing_response_body_carries_a_secret(self):
        self.store.billing[TENANT_A] = {
            "tenant_id": TENANT_A, "plan": "pro", "subscription_status": "active",
            "polar_customer_id": "cus_a", "polar_subscription_id": "sub_a",
            "polar_product_id": PRO_PRODUCT, "cancel_at_period_end": False,
            "current_period_end": None, "past_due_at": None}
        auth = {"Authorization": f"Bearer {self.signer.token(org_id='org_0')}"}
        bodies = [
            self.client.get("/api/billing/subscription", headers=auth).text,
            self.client.post("/api/billing/portal", headers=auth).text,
            self.client.post("/api/billing/checkout", json={"plan": "pro"},
                             headers=auth).text,
        ]
        for body in bodies:
            self.assertNotIn(ACCESS_TOKEN, body)
            self.assertNotIn(WEBHOOK_SECRET, body)
            # The customer access token minted alongside the portal URL.
            self.assertNotIn("customer-token-never-disclosed", body)

    def test_the_access_token_is_sent_only_to_the_configured_polar_host(self):
        auth = {"Authorization": f"Bearer {self.signer.token(org_id='org_0')}"}
        self.client.post("/api/billing/checkout", json={"plan": "pro"},
                         headers=auth)
        for request in self.transport.requests:
            self.assertTrue(
                request["url"].startswith("https://sandbox-api.polar.sh/"),
                request["url"])
            self.assertEqual(request["headers"]["Authorization"],
                             f"Bearer {ACCESS_TOKEN}")


# ===================================================== the real database

@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; billing idempotency "
                          "and ownership are database properties")
class BillingStorePostgresTests(unittest.TestCase):
    """The guarantees the fake store can only imitate.

    Duplicate delivery, out-of-order delivery and cross-tenant ownership are all
    enforced by constraints and by an ON CONFLICT predicate. A fake that
    reproduces them proves that the fake is correct, not that the schema is.
    """

    @classmethod
    def setUpClass(cls):
        from agent.api.pool import StorePool
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=2)

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def setUp(self):
        with self.pool.acquire() as store:
            store.connection.execute("DELETE FROM billing_webhook_deliveries")
            store.connection.execute("DELETE FROM tenant_billing")
            store.connection.execute("DELETE FROM tenants")
            for index, organization in enumerate(("org_a", "org_b")):
                store.upsert_tenant_for_clerk_organization(
                    organization, organization_name=f"Workspace {index}")

    def _tenant(self, organization):
        with self.pool.acquire() as store:
            return store.tenant_by_clerk_organization(organization)["tenant_id"]

    def _upsert(self, store, tenant_id, **overrides):
        record = {
            "tenant_id": tenant_id,
            "polar_customer_id": "cus_pg_1",
            "polar_subscription_id": "sub_pg_1",
            "polar_product_id": STARTER_PRODUCT,
            "plan": "starter",
            "subscription_status": "active",
            "current_period_end": NOW + timedelta(days=30),
            "cancel_at_period_end": False,
            "past_due_at": None,
            "subscription_modified_at": NOW,
        }
        record.update(overrides)
        return store.upsert_billing_from_subscription(**record)

    def test_a_tenant_with_no_row_reads_as_no_billing(self):
        tenant = self._tenant("org_a")
        with self.pool.acquire() as store:
            self.assertIsNone(store.billing_for_tenant(tenant))

    def test_the_first_write_applies_and_a_replay_is_idempotent(self):
        tenant = self._tenant("org_a")
        with self.pool.acquire() as store:
            self.assertEqual(self._upsert(store, tenant), "applied")
            self.assertEqual(self._upsert(store, tenant), "applied")
            record = store.billing_for_tenant(tenant)
        self.assertEqual(record["plan"], "starter")
        self.assertEqual(record["subscription_status"], "active")

    def test_an_older_object_is_refused_by_the_database(self):
        tenant = self._tenant("org_a")
        with self.pool.acquire() as store:
            self._upsert(store, tenant, subscription_status="canceled", plan="free",
                         subscription_modified_at=NOW + timedelta(hours=2))
            outcome = self._upsert(store, tenant, subscription_status="active",
                                   plan="pro", polar_product_id=PRO_PRODUCT,
                                   subscription_modified_at=NOW)
            record = store.billing_for_tenant(tenant)
        self.assertEqual(outcome, "stale")
        self.assertEqual(record["subscription_status"], "canceled")
        self.assertEqual(record["plan"], "free")

    def test_a_subscription_cannot_be_claimed_by_a_second_workspace(self):
        from agent.postgres_lifecycle_store import TenantBillingConflict

        first, second = self._tenant("org_a"), self._tenant("org_b")
        with self.pool.acquire() as store:
            self._upsert(store, first)
        with self.pool.acquire() as store:
            with self.assertRaises(TenantBillingConflict):
                self._upsert(store, second)
        with self.pool.acquire() as store:
            self.assertIsNone(store.billing_for_tenant(second))

    def test_a_webhook_naming_an_unknown_tenant_creates_nothing(self):
        with self.pool.acquire() as store:
            outcome = self._upsert(store, "ten_" + "f" * 32)
            self.assertEqual(outcome, "ignored")
            row = store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenant_billing").fetchone()
        self.assertEqual(row["c"], 0)

    def test_a_paid_plan_without_a_subscription_is_refused_by_the_schema(self):
        tenant = self._tenant("org_a")
        with self.pool.acquire() as store:
            with self.assertRaises(Exception):
                store.connection.execute(
                    "INSERT INTO tenant_billing (tenant_id, plan) VALUES (%s, 'pro')",
                    (tenant,))

    def test_a_delivery_is_claimed_once(self):
        with self.pool.acquire() as store:
            self.assertTrue(store.record_billing_webhook_delivery(
                delivery_id="msg_pg_1", event_type="subscription.active"))
            self.assertFalse(store.record_billing_webhook_delivery(
                delivery_id="msg_pg_1", event_type="subscription.active"))

    def test_the_polar_customer_maps_back_to_its_workspace(self):
        tenant = self._tenant("org_a")
        with self.pool.acquire() as store:
            self._upsert(store, tenant, polar_customer_id="cus_lookup")
            self.assertEqual(store.tenant_for_polar_customer("cus_lookup"), tenant)
            self.assertIsNone(store.tenant_for_polar_customer("cus_unknown"))

    def test_deleting_a_tenant_takes_its_billing_row_with_it(self):
        tenant = self._tenant("org_a")
        with self.pool.acquire() as store:
            self._upsert(store, tenant)
            store.connection.execute("DELETE FROM tenants WHERE tenant_id = %s",
                                     (tenant,))
            row = store.connection.execute(
                "SELECT COUNT(*) AS c FROM tenant_billing WHERE tenant_id = %s",
                (tenant,)).fetchone()
        self.assertEqual(row["c"], 0)


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")


if __name__ == "__main__":
    unittest.main()
