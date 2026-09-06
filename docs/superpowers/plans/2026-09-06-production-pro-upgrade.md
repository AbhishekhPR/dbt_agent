# Production Pro Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Starter-to-Pro work through Polar's hosted portal, diagnose provider failures safely, prevent production from using sandbox billing, validate both catalogs before cutover, and publish consistent $149/$249 prices.

**Architecture:** Keep the existing `/api/billing/portal` path and webhook-only entitlement writes. Extend the Polar transport error with allow-listed provider diagnostics, add a Railway production/sandbox configuration guard, and add a read-only catalog/customer-session preflight. Make focused price-only frontend and marketing branches, leaving all deployment and live configuration changes manual.

**Tech Stack:** Python 3.10+, `unittest`, Starlette, urllib, PostgreSQL; React 19, Vite, Vitest; static HTML and Node test runner; GitHub CLI.

---

## File map

- `agent/billing/client.py`: safely parse Polar error codes/descriptions.
- `agent/billing/config.py`: reject sandbox billing in a Railway production environment.
- `agent/billing/service.py`: include safe provider diagnostics in structured logs only.
- `scripts/polar_billing_preflight.py`: read-only catalog and optional portal-capability validation.
- `test_polar_billing.py`: transport, portal, webhook, entitlement, and production-guard regressions.
- `test_polar_billing_preflight.py`: isolated preflight behavior tests.
- `.env.example`, `deploy/README.md`: scope, separation, preflight, and cutover instructions.
- `relium-app/src/lib/billingApi.js` and its tests: `$249` dashboard catalog.
- `Relium-site/index.html`, `pricing.html`, and `scripts/public-files.test.mjs`: `$249` marketing catalog.

### Task 1: Create isolated implementation branches

- [ ] **Step 1: Read the worktree isolation instructions**

Run: `Get-Content -Raw C:\Users\Abhishekh\.codex\skills\using-git-worktrees\SKILL.md`

- [ ] **Step 2: Create backend worktree from current `origin/main`**

Run: `git worktree add C:\Users\Abhishekh\.worktrees\dbt-agent\fix-production-pro-upgrade -b fix/production-pro-upgrade origin/main`

Expected: clean worktree on `fix/production-pro-upgrade`.

- [ ] **Step 3: Bring the approved spec and this plan into the clean branch**

Run: `git checkout feat/plan-entitlements -- docs/superpowers/specs/2026-09-06-production-pro-upgrade-design.md docs/superpowers/plans/2026-09-06-production-pro-upgrade.md`

- [ ] **Step 4: Create a focused dashboard worktree from `origin/main`**

Run: `git worktree add C:\Users\Abhishekh\.worktrees\relium-app\fix-production-pro-price -b fix/production-pro-price origin/main`

Expected: no onboarding/review-setup changes in the branch.

### Task 2: Preserve safe Polar provider diagnostics

**Files:**
- Modify: `agent/billing/client.py`
- Modify: `agent/billing/service.py`
- Test: `test_polar_billing.py`

- [ ] **Step 1: Write failing transport tests**

Add tests that feed this response through `PolarClient`:

```python
body = json.dumps({
    "error": "insufficient_scope",
    "error_description": "The request requires higher privileges than provided by the access token.",
    "customer_id": "must-not-survive",
}).encode()
```

Assert `PolarAPIError.provider_code == "insufficient_scope"`, its safe
description is retained, and arbitrary fields/customer identifiers do not
appear in `str(error)` or its attributes.

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m unittest test_polar_billing.PolarClientTests -v`

Expected: FAIL because the diagnostic attributes do not exist and HTTP bodies
are currently discarded.

- [ ] **Step 3: Implement bounded allow-listed error parsing**

Give `PolarAPIError` optional `provider_code` and `provider_description`
attributes. On non-2xx responses, decode a bounded JSON object and retain only
short string values from `error` and `error_description`; keep the public
exception message owned by Relium.

```python
def _provider_diagnostic(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    return (_bounded_text(value.get("error"), 128),
            _bounded_text(value.get("error_description"), 512))
```

Update `_urllib_transport` to return the bounded HTTP error body instead of
replacing it with `{}`.

- [ ] **Step 4: Add structured logging assertions**

Assert a portal failure logs `provider_code=insufficient_scope`, Polar status
403, and operation `create_customer_session`, while no token, customer ID,
external ID, or provider description is logged.

- [ ] **Step 5: Implement safe service logging and verify GREEN**

Add `provider_code` to the existing structured checkout/portal error records.
Run: `python -m unittest test_polar_billing -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add agent/billing/client.py agent/billing/service.py test_polar_billing.py
git commit -m "fix: retain safe Polar failure diagnostics"
```

### Task 3: Refuse sandbox billing in Railway production

**Files:**
- Modify: `agent/billing/config.py`
- Test: `test_polar_billing.py`

- [ ] **Step 1: Write failing configuration tests**

Add tests proving:

```python
env = complete_polar_env(POLAR_SERVER="sandbox",
                         RAILWAY_ENVIRONMENT_NAME="production")
with self.assertRaisesRegex(PolarConfigurationError,
                            "Railway production.*POLAR_SERVER=production"):
    PolarSettings.from_environ(env)
```

and that sandbox is accepted when Railway names `staging`, `development`, or no
environment.

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest test_polar_billing.PolarConfigurationTests -v`

Expected: FAIL because production currently logs a warning and accepts sandbox.

- [ ] **Step 3: Implement the guard**

After parsing `POLAR_SERVER`, reject only the exact production environment:

```python
railway_environment = _text(values.get("RAILWAY_ENVIRONMENT_NAME"))
if railway_environment == "production" and server != SERVER_PRODUCTION:
    raise PolarConfigurationError(
        "Railway production requires POLAR_SERVER=production; sandbox "
        "tokens, products, customers, subscriptions and webhooks are separate.")
```

- [ ] **Step 4: Run and verify GREEN**

Run: `python -m unittest test_polar_billing.PolarConfigurationTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add agent/billing/config.py test_polar_billing.py
git commit -m "fix: reject sandbox billing in production"
```

### Task 4: Add the read-only billing preflight

**Files:**
- Create: `scripts/polar_billing_preflight.py`
- Create: `test_polar_billing_preflight.py`

- [ ] **Step 1: Write failing product validation tests**

Cover one unarchived fixed recurring monthly USD price, exact cents, distinct
IDs, product existence, server selection, and webhook-secret presence. Use an
injected transport so tests open no sockets.

```python
result = validate_catalog(settings, get_json=fake_get)
self.assertEqual(result, {
    "server": "sandbox",
    "starter": {"id": STARTER_PRODUCT, "monthly_usd_cents": 14900},
    "pro": {"id": PRO_PRODUCT, "monthly_usd_cents": 24900},
})
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest test_polar_billing_preflight -v`

Expected: FAIL because the preflight module does not exist.

- [ ] **Step 3: Implement pure catalog validation and CLI**

The CLI accepts `--expected-server {sandbox,production}` and optional
`--customer-external-id`. It loads `PolarSettings`, requires selected and
expected environments to match, fetches `/v1/products/{id}`, and prints only
environment, plan, product ID, state, cadence, currency, cents, and check
outcome.

- [ ] **Step 4: Write failing portal-capability tests**

Assert an optional QA external ID calls `/v1/customer-sessions/`, treats a
2xx response as capability success without printing the portal URL, and reports
HTTP 403 plus `insufficient_scope` on failure.

- [ ] **Step 5: Run and verify RED, implement, then verify GREEN**

Run: `python -m unittest test_polar_billing_preflight -v`

Expected before implementation: FAIL on portal checks. Expected after minimal
implementation: PASS.

- [ ] **Step 6: Commit**

```powershell
git add scripts/polar_billing_preflight.py test_polar_billing_preflight.py
git commit -m "feat: add Polar billing preflight"
```

### Task 5: Strengthen Starter-to-Pro invariants

**Files:**
- Modify: `test_polar_billing.py`
- Modify if required by a failing test: `agent/api/billing_routes.py`
- Modify if required by a failing test: `agent/billing/service.py`

- [ ] **Step 1: Write the end-to-end in-process regression**

Start with a stored active Starter row. Assert checkout returns 409 without a
Polar call; portal returns a URL addressed by the tenant; a simulated 403 leaves
the row byte-for-byte unchanged; an unsigned Pro webhook returns 401 and leaves
Starter unchanged; a signed `subscription.updated` carrying the configured Pro
product returns 202 and changes the subscription view to Pro entitlements.

- [ ] **Step 2: Run and verify RED or existing behavior**

Run the named new test directly. If any segment already passes, retain it as
coverage; at least the combined transition/failure invariant must initially
fail because it does not yet exist as a regression.

- [ ] **Step 3: Add only the minimal implementation needed by failures**

Do not add direct subscription updates, a second checkout, or browser-driven
entitlement writes.

- [ ] **Step 4: Run and verify GREEN**

Run: `python -m unittest test_polar_billing -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add test_polar_billing.py agent/api/billing_routes.py agent/billing/service.py
git commit -m "test: cover Starter to Pro portal transition"
```

### Task 6: Document scopes and manual cutover

**Files:**
- Modify: `.env.example`
- Modify: `deploy/README.md`
- Add: approved spec and implementation plan under `docs/superpowers/`

- [ ] **Step 1: Correct Pro price and token scope documentation**

Document the Polar organization token capabilities required by Relium,
including product reads, checkout creation, and `customer_sessions:write`.
Correct `$250/month` to `$249/month`.

- [ ] **Step 2: Document preflight commands**

```powershell
python scripts/polar_billing_preflight.py --expected-server sandbox `
  --customer-external-id ten_0646bf05921047f68b15f8cbf6d378e8
python scripts/polar_billing_preflight.py --expected-server production
```

State that the first command creates only a short-lived portal session; the
second performs no customer/subscription mutation.

- [ ] **Step 3: Document exact cutover order**

Explicitly delete the disposable sandbox `tenant_billing` row before applying
live values; never retain its customer/subscription/product IDs. List the five
live variables: `POLAR_SERVER`, `POLAR_ACCESS_TOKEN`, `POLAR_WEBHOOK_SECRET`,
`POLAR_STARTER_PRODUCT_ID`, `POLAR_PRO_PRODUCT_ID`.

- [ ] **Step 4: Verify documentation**

Run: `rg -n '\$250|POLAR_SERVER|customer_sessions:write|polar_billing_preflight' .env.example deploy/README.md docs/superpowers`

Expected: no `$250`; environment and scope instructions are present.

- [ ] **Step 5: Commit**

```powershell
git add .env.example deploy/README.md docs/superpowers
git commit -m "docs: add live Polar cutover runbook"
```

### Task 7: Create a focused dashboard price PR

**Files:**
- Modify: `src/lib/billingApi.js`
- Modify: `src/lib/billingApi.test.js`
- Modify: `src/components/settings/Billing.plans.test.jsx`
- Modify: `src/components/settings/Billing.test.jsx`

- [ ] **Step 1: Write/update the failing price tests in the clean worktree**

Expect Starter `$149` and Pro `$249` in the catalog and rendered cards. Assert
an active Starter's **Change to Pro** calls `createPortalSession`, never
`createCheckout`.

- [ ] **Step 2: Run and verify RED**

Run: `npm run test:unit -- src/lib/billingApi.test.js src/components/settings/Billing.plans.test.jsx src/components/settings/Billing.test.jsx`

Expected: FAIL on `$250` in `origin/main`.

- [ ] **Step 3: Change only the Pro display price to `$249`**

```javascript
pro: { price: '$249', cadence: 'month' }
```

Update fixtures/expectations without changing request behavior.

- [ ] **Step 4: Run focused tests and full frontend verification**

Run: `npm test`

Run: `$env:VITE_RELIUM_API_URL='https://api.relium.test'; $env:VITE_CLERK_PUBLISHABLE_KEY='pk_test_placeholder'; npm run build`

Expected: all unit/script tests and the production build pass; bundle scan is
clean.

- [ ] **Step 5: Commit, push, and open a PR**

```powershell
git add src/lib/billingApi.js src/lib/billingApi.test.js src/components/settings/Billing.plans.test.jsx src/components/settings/Billing.test.jsx
git commit -m "fix: price Pro at 249 per month"
git push -u origin fix/production-pro-price
gh pr create --base main --head fix/production-pro-price --title "Fix Pro price at $249/month" --body "Changes the dashboard's single Pro display price from $250 to $249/month, keeps Starter at $149/month, and adds regression coverage for the rendered cards and portal-based Starter-to-Pro action. No billing identifier or charged amount is sent by the frontend."
```

### Task 8: Verify the focused marketing PR

**Files:**
- Existing branch: `Relium-site` `fix/pro-price-249`

- [ ] **Step 1: Run its complete test suite**

Run: `node --test scripts/public-files.test.mjs`

Expected: PASS with homepage and pricing page both asserting Starter `$149`,
Pro `$249`, and no `$250`.

- [ ] **Step 2: Inspect PR scope**

Run: `gh pr diff 4 --repo AbhishekhPR/Relium-site --name-only`

Expected: only pricing pages and their tests.

### Task 9: Full backend verification and PR

- [ ] **Step 1: Run focused billing tests**

Run: `python -m unittest test_polar_billing test_polar_billing_preflight -v`

- [ ] **Step 2: Run the complete backend suite**

Run the repository's canonical full suite from its CI configuration, including
PostgreSQL-backed tests when `RELIUM_TEST_POSTGRES_DSN` is available.

- [ ] **Step 3: Run static/import/secret checks**

Compile changed Python, inspect `git diff --check`, and run the repository's
secret scan. Confirm no live or sandbox credential is committed.

- [ ] **Step 4: Push and create the backend PR**

```powershell
git push -u origin fix/production-pro-upgrade
gh pr create --base main --head fix/production-pro-upgrade --title "Fix and guard the production Pro upgrade flow" --body "Preserves safe Polar failure diagnostics, refuses sandbox billing in Railway production, adds a read-only Polar catalog/customer-session preflight, and covers the webhook-only Starter-to-Pro entitlement transition. Includes the manual sandbox repair and live cutover runbook; it does not mutate Polar or Railway configuration."
```

- [ ] **Step 5: Inspect every PR without merging**

Use `gh pr view` and `gh pr diff --name-only` for the backend, dashboard, and
marketing PRs. Report checks and any external blocker. Do not merge or deploy.
