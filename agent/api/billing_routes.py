"""Billing: checkout, status, customer portal, and the Polar webhook.

    POST /api/billing/checkout          start a Polar checkout for this workspace
    GET  /api/billing/subscription      what this workspace is entitled to
    POST /api/billing/portal            open Polar's hosted customer portal
    POST /api/billing/webhooks/polar    Polar -> Relium, signature verified

###################################################################
# THREE ROUTES ARE CLERK-AUTHENTICATED. ONE IS PUBLIC AND SIGNED. #
###################################################################

The first three resolve the tenant exactly the way onboarding does — through the
shared ``ClerkAuthenticator``, from the organization id inside a server-verified
Clerk token — so a workspace id never appears in a path, a query or a body.
There is no request shape in which a member of workspace A can create a
checkout, read a status, or open a portal for workspace B, because there is
nowhere in the request to say which workspace is meant.

The webhook is unauthenticated in the session sense and authenticated in the
only sense that matters for a server-to-server callback: nothing is parsed,
looked up or written until the Standard Webhooks signature over the RAW body
verifies against POLAR_WEBHOOK_SECRET.

###################################################################
# THE BACKEND IS AUTHORITATIVE. THE RETURN URL IS NOT.            #
###################################################################

Nothing in this module grants a plan in response to a browser. `POST /checkout`
creates a session and returns a URL; the plan changes when — and only when — a
verified webhook says Polar changed it. A customer who edits the success URL,
replays it, or types it by hand reaches a page that re-reads this same
database.
"""
from __future__ import annotations

import json
import logging

from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from agent.billing.service import BillingError
from agent.billing.signature import SignatureError, verify
from agent.postgres_lifecycle_store import TenantBillingConflict

logger = logging.getLogger(__name__)

#: A billing request body carries one short field.
MAX_BODY_BYTES = 16 * 1024

#: A Polar webhook body is one subscription object with the full product and its
#: benefits embedded. Bounded generously rather than tightly: a 413 here is not a
#: safe failure — Polar would retry, the retries would fail identically, and a
#: paying customer would stay on the free plan for a reason nothing in the UI
#: could explain.
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024

#: Codes rendered as 409 rather than 500: the request was well formed and the
#: caller is who they say they are, but the workspace is not in a state where it
#: can succeed.
CONFLICT_CODES = frozenset({"no_billing_account", "subscription_exists"})

#: Event families this endpoint acts on. Every `subscription.*` event Polar
#: documents carries the full Subscription object, so they are applied through
#: one path rather than through ten near-identical branches that could disagree
#: about what `status` means. The names Polar currently emits are
#: subscription.created, .active, .updated, .canceled, .uncanceled, .past_due,
#: .revoked, .paused, .resumed and .cycled; matching the prefix means a new one
#: is handled correctly on the day it appears rather than silently dropped.
#:
#: Everything else — orders, refunds, benefit grants, product and organization
#: events — is acknowledged and ignored. Relium's entitlement is derived from
#: the subscription object alone, so an order event would be a second, redundant
#: source of truth for the same fact.
SUBSCRIPTION_EVENT_PREFIX = "subscription."


def create_billing_routes(*, store_pool, clerk_authenticator, service=None):
    """Build the billing route table.

    ``service`` is None when this deployment has no Polar configuration. The
    routes are still registered and answer 503 — an endpoint that vanishes on
    misconfiguration is indistinguishable from one that was never deployed, and
    the API contract test requires the served table to be stable.
    """

    # -- Clerk-authenticated handlers -------------------------------------

    def create_checkout(request, body, store, principal):
        plan = body.get("plan")
        if not isinstance(plan, str):
            # Not a validation error about a missing field: an unusable plan and
            # an unknown plan are the same refusal, and giving them one code
            # keeps the frontend from having to distinguish them.
            raise BillingError("unknown_plan")
        # Anything else in the body — a product id, an amount, a customer id, a
        # workspace id — is ignored. It is not read, so it cannot be trusted.
        return 200, service.create_checkout(store, principal.tenant_id, plan)

    def get_subscription(request, body, store, principal):
        view = service.subscription_view(store, principal.tenant_id)
        return 200, {
            "plan": view["plan"],
            "status": view["status"],
            "is_active": view["is_active"],
            "cancel_at_period_end": view["cancel_at_period_end"],
            "current_period_end": _isoformat(view["current_period_end"]),
            "has_billing_account": view["has_billing_account"],
        }

    def create_portal(request, body, store, principal):
        return 200, service.create_portal_session(store, principal.tenant_id)

    # -- the webhook ------------------------------------------------------

    async def polar_webhook(request):
        request_id = _request_id(request)
        if service is None:
            return _json({"status": "unavailable",
                          "code": "billing_not_configured"}, 503, request_id)

        raw = await _read_bounded_body(request, MAX_WEBHOOK_BODY_BYTES)
        if raw is None:
            return _json({"status": "payload_too_large"}, 413, request_id)

        # ###########################################################
        # # NOTHING BELOW THIS LINE RUNS ON AN UNVERIFIED DELIVERY. #
        # ###########################################################
        try:
            delivery_id = verify(secret=service.settings.webhook_secret,
                                 body=raw, headers=request.headers,
                                 now=service.now().timestamp())
        except SignatureError as error:
            # The reason is recorded for an operator and never returned: naming
            # the failed check would make this an oracle for forging a
            # signature. No header, body or secret is logged.
            logger.info("billing_webhook_refused",
                        extra={"operation": "polar_webhook",
                               "reason": str(error),
                               "error_category": "webhook_signature"})
            return _json({"status": "unauthorized"}, 401, request_id)

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _json({"status": "invalid_request"}, 400, request_id)
        if not isinstance(document, dict):
            return _json({"status": "invalid_request"}, 400, request_id)

        event_type = document.get("type")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
            return _json({"status": "invalid_request"}, 400, request_id)

        def work():
            with store_pool.acquire() as store:
                # ###################################################
                # # THE CLAIM AND THE WRITE COMMIT TOGETHER.        #
                # ###################################################
                #
                # The delivery is claimed FIRST, so a replay loses the claim and
                # a duplicate cannot reach the write path at all, whatever the
                # payload says. But the store connection is autocommit, so
                # claiming in one statement and writing in the next would commit
                # the claim on its own: a transient database failure during the
                # write would answer 503, Polar would retry, and the retry would
                # find the delivery already claimed and skip it as a duplicate.
                # The subscription change would be lost permanently, leaving a
                # paying customer on the wrong plan with nothing left to retry.
                #
                # One transaction makes the pair atomic: either the delivery is
                # recorded AND applied, or neither happened and Polar's retry
                # gets a clean attempt.
                try:
                    with store.connection.transaction():
                        if not store.record_billing_webhook_delivery(
                                delivery_id=delivery_id, event_type=event_type):
                            return "duplicate"
                        if not event_type.startswith(SUBSCRIPTION_EVENT_PREFIX):
                            return "ignored"
                        return service.apply_subscription_event(
                            store, event_type, document.get("data"))
                except TenantBillingConflict:
                    # The one failure a retry cannot fix: the subscription
                    # belongs to another workspace, and it will still belong to
                    # them next time. The rollback above took the claim with it,
                    # so it is re-made here on its own — otherwise Polar would
                    # retry this delivery on its full schedule for a conflict
                    # that is permanent by construction.
                    store.record_billing_webhook_delivery(
                        delivery_id=delivery_id, event_type=event_type)
                    raise

        try:
            outcome = await run_in_threadpool(work)
        except TenantBillingConflict:
            # A subscription that another workspace already owns. Acknowledged
            # so Polar stops retrying a delivery that will never succeed, and
            # logged loudly because it should not be possible.
            logger.error("billing_subscription_ownership_conflict",
                         extra={"operation": event_type,
                                "error_category": "billing_integrity"})
            return _json({"status": "conflict"}, 409, request_id)
        except Exception:
            logger.error("billing_webhook_persist_failed",
                         extra={"operation": event_type,
                                "error_category": "database"})
            # 503 so Polar retries. Every write on this path is idempotent, so a
            # retry after a partial failure converges rather than duplicating.
            return _json({"status": "unavailable"}, 503, request_id)

        return _json({"status": outcome}, 202, request_id)

    # -- plumbing ---------------------------------------------------------

    def handler(fn, *, write):
        async def wrapped(request):
            request_id = _request_id(request)
            if service is None:
                return _json({"status": "unavailable",
                              "code": "billing_not_configured"}, 503, request_id)
            try:
                body = await _read_json(request) if write else None

                def work():
                    with store_pool.acquire() as store:
                        # require_tenant: billing belongs to a workspace, and
                        # there is no billing question that can be answered
                        # before one exists.
                        principal = clerk_authenticator.principal(
                            request, store, write=write, require_tenant=True)
                        return fn(request, body, store, principal)

                status, payload = await run_in_threadpool(work)
                return _json(payload, status, request_id)
            except BillingError as exc:
                status = 409 if exc.code in CONFLICT_CODES else (
                    503 if exc.code in ("billing_not_configured",
                                        "billing_provider_unavailable") else 422)
                return _json({"status": _status_word(status), "code": exc.code},
                             status, request_id)
            except Exception as exc:
                mapped = clerk_authenticator.map_error(exc, request_id)
                if mapped is not None:
                    return mapped
                logger.error("billing_request_failed",
                             extra={"error_category": "internal",
                                    "route_template": request.url.path})
                return _json({"status": "unavailable"}, 500, request_id)

        return wrapped

    return [
        Route("/api/billing/checkout", handler(create_checkout, write=True),
              methods=["POST"]),
        Route("/api/billing/subscription", handler(get_subscription, write=False),
              methods=["GET"]),
        Route("/api/billing/portal", handler(create_portal, write=True),
              methods=["POST"]),
        Route("/api/billing/webhooks/polar", polar_webhook, methods=["POST"]),
    ]


def _status_word(status):
    if status == 409:
        return "conflict"
    if status == 503:
        return "unavailable"
    return "invalid_request"


def _isoformat(value):
    from agent.api.validation import isoformat

    return isoformat(value)


async def _read_bounded_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return None
        body.extend(chunk)
    return bytes(body)


async def _read_json(request):
    from agent.api.onboarding_routes import _BadRequest

    raw = await _read_bounded_body(request, MAX_BODY_BYTES)
    if raw is None:
        raise _BadRequest("request body is too large")
    if not raw:
        return {}
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _BadRequest("request body must be valid JSON") from None
    if not isinstance(document, dict):
        raise _BadRequest("request body must be a JSON object")
    return document


# Imported late: onboarding_routes owns the JSON plumbing and the Clerk
# authenticator, and imports nothing from here.
from agent.api.onboarding_routes import _json, _request_id  # noqa: E402
