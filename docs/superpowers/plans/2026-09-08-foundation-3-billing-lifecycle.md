# Foundation 3 Billing Lifecycle Implementation Plan

**Goal:** Add authoritative, durable, fail-closed Polar billing reconciliation/revocation and checkout idempotency without changing webhook-driven entitlements.

**Architecture:** Extend the Polar client with strictly validated pagination/get/delete operations. Persist bounded observations and leased operations behind the PostgreSQL store. A lifecycle service obtains a refreshed owner context, reconciles authoritative provider identity, and proves immediate revocation with a second complete reconciliation.

**Tech Stack:** Python 3.10, PostgreSQL migrations, unittest, Starlette-compatible service/store boundaries.

---

### Task 1: Migration and store contract

- Add failing migration/schema tests for lifecycle controls, observations, operations, checkout intents, constraints, and deterministic active backfill.
- Add migration `0023_billing_lifecycle_safety.sql` and minimal store methods.
- Run migration and store tests; commit.

### Task 2: Polar read/revoke boundary

- Add failing client tests for complete pagination, filters, bounded shapes, statuses, DELETE, 404, and safe diagnostics.
- Implement client list/get/delete methods and response validators.
- Run Polar client tests; commit.

### Task 3: Reconciliation

- Add failing service tests for status classification, external/customer union, pagination, multiple subscriptions, identity conflict, actionable checkout, and provider failure categories.
- Implement `reconcile_workspace_billing` with refreshed owner authorization and durable observations; never modify `tenant_billing`.
- Run focused billing/auth/isolation tests; commit.

### Task 4: Immediate billing stop

- Add failing tests for checkout lock, leased retry, every nonterminal subscription, partial outcomes, 404 follow-up proof, final proof, and tenant isolation.
- Implement `revoke_workspace_subscriptions` with short transaction/provider/short transaction boundaries.
- Run focused tests; commit.

### Task 5: Checkout intent idempotency

- Add failing tests for concurrent claims, lost response recovery, zero/one/multiple metadata matches, actionable checkout/subscription refusal, and lifecycle lock.
- Integrate durable intents and provider reconciliation into checkout creation without changing webhook entitlement authority.
- Run billing/webhook regression tests; commit.

### Task 6: Verification and PR

- Run migration/backfill, billing, webhook, auth/ownership, tenant isolation/security, repository/GitHub, collector/CI, and relevant backend suites using an isolated local PostgreSQL instance where required.
- Run immutable secret scan and `git diff --check`.
- Request independent review, resolve relevant findings, re-run verification, push, open PR targeting main, and drive required CI green.
