# Production Pro Upgrade Design

## Objective

Make the Relium Starter-to-Pro upgrade reliable without creating a second
subscription, and prepare a production Polar cutover that cannot mix sandbox
and live billing objects. Starter remains $149/month and Pro is $249/month in
the dashboard, marketing site, Polar catalog, checkout/portal experience, and
tests.

## Observed production failure

For an active Starter workspace, the dashboard's **Change to Pro** button calls
`POST /api/billing/portal`. The backend resolves the tenant from the verified
Clerk organization claim, reads the tenant's stored Polar customer mapping, and
calls `POST /v1/customer-sessions/` using the tenant ID as
`external_customer_id`.

The production API returned HTTP 503 with:

```json
{"status":"unavailable","code":"billing_provider_unavailable"}
```

The underlying Polar sandbox request returned HTTP 403 with:

```json
{
  "error": "insufficient_scope",
  "error_description": "The request requires higher privileges than provided by the access token."
}
```

Its `WWW-Authenticate` response named the missing
`customer_sessions:write` scope. Production Railway configuration also had
`POLAR_SERVER=sandbox`. The configured products existed only in the selected
sandbox catalog: Starter was active at 14900 USD cents per month and Pro was
active at 24900 USD cents per month. The stored Starter row, Polar customer
external ID, subscription customer ID, tenant metadata, product ID, and active
status agreed, so customer resolution was not the failure.

The deployed dashboard and marketing site still showed $250 because their
production deployments predated the existing $249 source changes.

## Chosen architecture

### Subscription change

Starter-to-Pro remains a Polar-hosted subscription change:

1. The dashboard reads `GET /api/billing/subscription`.
2. An active Starter subscription makes **Change to Pro** call
   `POST /api/billing/portal`.
3. The backend resolves the tenant exclusively from the verified Clerk token.
4. The backend verifies that this tenant has a stored Polar customer, then asks
   Polar for a customer portal session using the server-owned tenant ID as
   `external_customer_id`.
5. The browser redirects to the returned hosted portal URL.
6. The customer selects Pro in Polar's portal, where Polar owns confirmation,
   proration, invoices, and payment handling.
7. Relium changes no entitlement on the browser return. Only a verified
   `subscription.*` webhook may write the new Polar product and internal plan.

There will be no direct subscription PATCH and no second checkout. Checkout
remains available only when the workspace has no live subscription.

### Provider failure behavior

A failed Polar checkout or portal call returns a stable Relium error and makes
no database write. The existing Starter row and entitlements remain unchanged.
The Polar client will retain a small allow-list of safe provider diagnostics
(`error` and `error_description`) for structured operator logging. It will not
log or return request bodies, tokens, signatures, portal URLs, customer IDs, or
arbitrary provider response fields. The customer-facing response remains the
stable Relium error contract.

This makes an error such as `insufficient_scope` visible during operations
without turning the public API into a provider-detail leak.

## Environment separation

Polar sandbox and production are different billing universes. Tokens, webhook
secrets, product IDs, customers, subscriptions, and webhook deliveries are not
portable between them.

At configuration load, a Railway deployment whose environment name is
`production` must reject `POLAR_SERVER=sandbox`. Non-production environments
may select sandbox explicitly. The existing validation that Starter and Pro
product IDs differ remains in force.

The current sandbox Starter customer and subscription are disposable QA state.
They will be deleted from Relium's production `tenant_billing` table during the
approved cutover procedure and will never be copied or translated into live
customer/subscription IDs. No temporary internal entitlement is needed for this
QA workspace. If continuity becomes a real requirement for a different live
customer, it needs a separate reviewed design; it is not included here.

## Billing preflight

A read-only/operator preflight command will validate one explicitly selected
Polar environment before any deployment variables are changed. It will:

- require all four Polar credentials/configuration values;
- require an explicit expected environment and reject an API host mismatch;
- fetch both configured products from that environment's API;
- require distinct, existing, unarchived products;
- require exactly one active fixed recurring USD monthly catalog price of
  14900 cents for Starter and 24900 cents for Pro;
- verify product-to-plan mapping locally;
- optionally create a customer portal session for an explicitly supplied QA
  external customer ID, proving `customer_sessions:write` and customer
  resolution without changing a subscription;
- report safe error codes and HTTP statuses, never credentials or customer
  portal URLs;
- require the webhook secret to be present, while documenting that a signed
  webhook delivery is the final entitlement verification.

The preflight is not allowed to create products, customers, subscriptions,
checkouts, or access tokens, and it never mutates Relium billing state.

## Entitlement invariants

- `tenant_billing.plan` and `polar_product_id` change only through a verified
  `subscription.*` webhook.
- Unknown or wrong-environment product IDs map to Free, never Starter or Pro.
- Invalid webhook signatures perform no lookup and no write.
- Provider failures perform no write and preserve the current entitlement.
- Browser success parameters and portal returns grant nothing.
- A verified live Pro product webhook maps to Pro and exposes Pro capabilities.

## Price sources

- Dashboard display: `relium-app/src/lib/billingApi.js`.
- Marketing homepage and pricing page: `Relium-site/index.html` and
  `Relium-site/pricing.html`.
- Charge amount: live Polar product catalog, verified by preflight.
- Backend: never accepts or sends a price supplied by the browser.

All assertions use Starter $149/month and Pro $249/month.

## Test strategy

Backend regression coverage will verify:

- a production Railway environment refuses sandbox Polar configuration;
- a non-production environment may use sandbox;
- safe provider error parsing records `insufficient_scope` without leaking
  arbitrary response content;
- an active Starter uses `/api/billing/portal`, resolved by its own tenant;
- portal/provider failure preserves the Starter billing row and entitlements;
- a successful portal response returns only its redirect URL;
- checkout remains forbidden for an existing live Starter subscription;
- an invalid signature cannot change Starter;
- a verified Pro subscription webhook changes Starter to Pro;
- the Pro product maps to Pro and an unknown product maps to Free;
- the preflight accepts 14900/24900 monthly products and rejects wrong amount,
  cadence, archive state, duplicate IDs, wrong environment, missing scope, and
  missing webhook configuration.

Frontend and marketing regression tests will assert $149/$249, the precise
portal endpoint for **Change to Pro**, redirect behavior, and surfaced stable
Relium error information. Full relevant backend, dashboard, marketing, build,
and secret-scan suites run before PR creation.

## Cutover sequence

1. In Polar sandbox, issue a replacement organization token containing the
   existing required read/checkout permissions plus `customer_sessions:write`.
2. Replace only the sandbox environment's token, then run preflight against the
   existing QA Starter external customer.
3. Exercise Starter-to-Pro in the sandbox portal and verify the signed sandbox
   webhook changes the QA row to Pro.
4. Create or verify live Polar Starter and Pro products at 14900 and 24900 USD
   cents per month.
5. Create a live organization token with every required scope and a separate
   live webhook secret/endpoint.
6. Run preflight with `--expected-server production` using the proposed live
   values before applying them to Railway.
7. Stop billing writes for the brief cutover window, remove the disposable
   sandbox `tenant_billing` row, and retain an audit record outside production
   billing state if required. Do not copy its Polar IDs.
8. Set production Railway to `POLAR_SERVER=production` with the live token,
   live webhook secret, and live product IDs, then deploy backend first.
9. Verify startup passed the production guard and perform a signed live webhook
   test that maps the live products correctly without granting an entitlement
   from an unsigned request.
10. Deploy the dashboard and marketing price changes.
11. Complete a real live Starter purchase, confirm the verified webhook grants
    Starter, open **Change to Pro**, change in Polar's portal, and confirm the
    verified webhook grants Pro.

No step in this implementation merges, deploys, mutates the live Polar catalog,
or changes Railway production variables.

## Deliverables

- A focused backend PR for provider diagnostics, production environment guard,
  billing preflight, documentation, and regression tests.
- A focused dashboard PR showing $249 and retaining the portal-based plan
  change.
- The focused marketing PR showing $249.
- A final report with exact evidence, scopes, preflight usage, test results,
  cutover order, production verification, PR links, and remaining external
  credential/catalog blockers.
