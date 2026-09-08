# Foundation 4 Credential Revocation Implementation Plan

**Goal:** Atomically revoke all authoritative tenant-owned Relium credentials and actionable local work, with durable retry-safe results and explicit external remediation.

**Architecture:** Stack on Foundation 3's lifecycle controls. Resolve owner and operational roots server-side, lock the tenant lifecycle row, and perform digest destruction, pointer cleanup, collector/session revocation, and pending-work cancellation in one PostgreSQL transaction. Gate all affected authentication/issuance paths on lifecycle state.

**Tech Stack:** Python 3.10, PostgreSQL migrations, unittest, existing service-token/session/collector APIs.

---

### Task 1: Migration and constraints

- Add failing tests for revocation operation schema, nullable destroyed digests, pointer consistency, cancellation states, and session provenance.
- Add migration `0024_workspace_credential_revocation.sql` with safe legacy-compatible constraints.
- Run migration tests; commit.

### Task 2: Atomic revocation primitive

- Add failing tests covering all token scopes, exact tenant roots, pointer cleanup, collector identity revocation, requests/outbox cancellation, sessions, counts, idempotence, rollback, incomplete inventory, and cross-tenant isolation.
- Implement `revoke_workspace_credentials` with owner authorization and Foundation 2 inventory.
- Run focused PostgreSQL tests; commit.

### Task 3: Access and issuance gates

- Add failing tests that revoked/digestless credentials cannot authenticate and new CI/collector/session issuance/submission is rejected during revoking/revoked state, including concurrent submit versus revoke.
- Implement shared lifecycle guards at affected store/auth boundaries.
- Run collector/CI/session/repository regressions; commit.

### Task 4: Authenticated-user GitHub identity revocation

- Add failing tests for exact Clerk-user provenance, shared identity safety, legacy non-guessing, credential erasure, and session revocation.
- Implement the non-browser-ID `revoke_current_user_github_identity` service/store primitive.
- Run GitHub/auth/security tests; commit.

### Task 5: Verification and stacked PR

- Run all requested migration, auth, ownership, isolation/security, GitHub/repository, collector/CI, review/manifest/warehouse/billing, and relevant backend suites.
- Run immutable secret scan and `git diff --check`.
- Request independent review, fix relevant findings, re-run verification, push, and open a PR targeting the Foundation 3 branch. Drive all required CI green without merging or deploying.

