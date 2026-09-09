# Workspace and Account Lifecycle Implementation Plan

Date: 2026-09-09

## Delivery sequence

1. Backend lifecycle engine PR.
2. Independent backend security/ownership review and remediation.
3. Frontend Danger Zone PR against the reviewed backend contract.
4. Full focused and regression verification for both PRs.

Neither PR is merged or deployed by this plan.

## Backend TDD tasks

### 1. Authentication and provider clients

- Add failing Clerk claim tests for factor verification age, missing/malformed
  claims, and impersonation.
- Preserve only the verified fields required by the recent-auth primitive.
- Add failing bounded Clerk client tests for membership enumeration, membership
  removal, organization deletion, user deletion, pagination mutation, 404, and
  provider failures; then implement those methods.
- Add failing GitHub client tests for installation deletion and terminal
  verification; then implement bounded app-authenticated calls.

### 2. Migration and operation store

- Add migration `0025_workspace_account_lifecycle.sql` with lifecycle controls,
  workspace/account operations, provider step results, and dissociated receipts.
- Test clean install, upgrade, constraints, one-active-operation uniqueness,
  tenant isolation, receipt dissociation, and migration idempotency.
- Implement lease/generation claims, status reads, retries, and safe failure
  recording in the PostgreSQL store.

### 3. Freeze admission control

- First add race tests covering checkout, credential/token issuance,
  operational-root binding, repository onboarding, review creation, collector
  submission/request/claim, outbox work, and dashboard-session recreation.
- Centralize locked lifecycle admission checks without changing active-workspace
  behavior.
- Keep verified billing webhook processing available while frozen.

### 4. Workspace lifecycle orchestration

- Add unit tests for every state transition, retry, stale lease, and provider
  failure category.
- Compose Foundation 2 inventory, Foundation 3 billing revocation, GitHub
  uninstall verification, Foundation 4 credential revocation, filesystem purge,
  ordered PostgreSQL purge, Clerk deletion, and final receipt transaction.
- Add crash-boundary and cross-tenant tests at every phase.
- Add a bounded operator resume/audit command that prints no secrets or payloads.

### 5. Account and leave orchestration

- Test authoritative membership enumeration, snapshots changing mid-read,
  sole-owner blocking, shared-workspace preservation, partial Clerk success,
  identity/session revocation, attribution dissociation, user deletion last,
  and retry after verified absence.
- Implement account operations and the active-workspace leave primitive.

### 6. GitHub and collector controls

- Test personal, repository, installation, and uninstall semantics separately.
- Test per-token and revoke-all collector paths, pointer integrity, pending-work
  cancellation, idempotency, and warehouse/GitHub remediation messages.
- Implement subordinate-ID ownership checks under the authenticated tenant.

### 7. HTTP contract

- Add lifecycle capability/status/request/retry routes, leave route, distinct
  GitHub disconnect routes, and collector revoke-all route.
- Test Clerk-only scope resolution, CSRF/reverification responses, typed
  confirmation, response redaction, concurrency, and status polling.
- Update API contract documentation.

## Backend review gate

Run migration, lifecycle, provider-failure, concurrency, tenant-isolation,
filesystem, auth, billing, GitHub, repository, review, collector, warehouse,
security, and complete relevant backend suites. Run secret detection and
`git diff --check`. An independent reviewer must inspect deletion order,
provider ambiguity, race closure, tenant scope, receipt contents, and logs.

## Frontend TDD tasks

- Add an authenticated lifecycle API client that always uses the Clerk session
  token and never sends tenant/user authority.
- Add Settings capability loading without coupling unrelated Settings reads to
  lifecycle availability.
- Build accessible Danger Zone panels and focused confirmation dialogs for
  collector revocation, four GitHub disconnect meanings, leave, workspace
  deletion, and account deletion.
- Wrap destructive requests in Clerk reverification and handle cancellation.
- Poll durable operations; render retryable versus blocked states and accurate
  external-record/customer-remediation language.
- Test role visibility, exact confirmation, sole-owner errors, provider
  blockers, workspace switching, successful completion/sign-out, and no secret
  rendering.

## Final verification

- Run all backend and frontend unit/integration/regression suites and production
  builds.
- Run security/secret scans and diff checks.
- Independently review the frontend and combined contract.
- Open focused PRs, remediate genuine CI/review findings, and stop with both PRs
  mergeable, clean, green, and unmerged.

