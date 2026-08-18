# Onboarding production-readiness audit

State of the first-run onboarding backend at the end of Phase 3. Nothing here
is hidden or softened: everything that still prevents production deployment is
listed, including the items that are somebody else's decision rather than a
coding task.

---

## 1. The gap this phase found

`build_application` never constructed the Clerk verifier, the installation
binder, the identity linker or the repository service. Every onboarding route
was **served and answering 503 in production**, while the entire test suite
passed — because the tests assembled the application themselves.

Fixed, and `test_onboarding_bootstrap.py` now asserts what the **real**
bootstrap produces from an environment. Verified load-bearing: neutering the
wiring makes it fail with the exact message describing the outage.

The lesson is worth keeping: a test that builds its own application proves the
application works, not that the product ships it.

---

## 2. Remaining placeholders, dev adapters, synthetic data

### Backend — none

`grep` for `dev.?adapter|synthetic|placeholder|fixture|TODO|FIXME|stub` across
every onboarding module returns nothing. No dev adapter, no seeded tenant, no
fake repository. GitHub and Clerk are scripted **in tests only**, through
injected collaborators; the production path constructs real clients.

### Frontend — one, correctly gated

`relium-app-onboarding` still ships `src/onboarding/devAdapter.js`. It requires
`import.meta.env.DEV` **and** an explicit opt-in flag, and Rollup drops it from
production builds entirely. Verified again this phase: the production bundle
contains no fixture strings and no credentials.

---

## 3. BLOCKER: the frontend cannot consume this backend yet

The frontend's declared contract and the served routes disagree. This is the
largest remaining item and it is **not** a backend defect — the backend shape
is deliberately different, and better:

| Frontend expects | Backend serves | Why the backend differs |
|---|---|---|
| `POST /api/tenants` | `PUT /api/tenants` | Identity comes from the token, so the operation is idempotent by construction |
| `GET /api/tenants/{id}/github-installation` | `github` section of `/api/onboarding/state` | — |
| `GET /api/tenants/{id}/repositories` | `GET /api/onboarding/repositories` | **No tenant id in any path.** A tenant id in a URL is a spoofing surface; it now comes only from the verified token |
| `PUT /api/tenants/{id}/repositories/{repo}/configuration` | `PUT /api/onboarding/dbt` | Same reason |
| `github.installed` (boolean) | `github.status` (`not_connected` / `connected` / `suspended`) | A suspended installation is present but not working — a boolean cannot say that |
| camelCase fields | snake_case fields | Matches the rest of the API |

Also missing on the frontend side: the GitHub **identity link** step
(`POST /api/onboarding/github/identity`), which is mandatory before an
installation can be bound, and the `organization` step for a session with no
active Clerk organization.

**This is Phase 4.** `relium-app-onboarding` is read-only in this phase and was
not touched.

---

## 4. Required configuration

### Backend environment variables

| Variable | Required for | Effect if absent |
|---|---|---|
| `RELIUM_GITHUB_APP_ID` | everything | server does not start |
| `RELIUM_GITHUB_WEBHOOK_SECRET` | everything | server does not start |
| `RELIUM_GITHUB_PRIVATE_KEY` *or* `_PATH` | everything | server does not start |
| `RELIUM_STORAGE_ROOT` | everything | server does not start |
| `RELIUM_DATABASE_URL` | all onboarding | **onboarding routes absent entirely** |
| `RELIUM_CLERK_ISSUER` | authentication | routes served, authenticate nobody (503) |
| `RELIUM_SESSION_ENCRYPTION_KEY` | installation binding | binding and repository service disabled |
| `RELIUM_GITHUB_CLIENT_ID` / `_SECRET` | GitHub identity link | no identity can be proved → no installation can bind |
| `RELIUM_PUBLIC_URL` | OAuth callback, CI variables | link callback and `RELIUM_API_URL` unset |
| `RELIUM_DASHBOARD_URL` | GitHub return redirects | redirects go to a relative path |
| `RELIUM_CORS_ALLOWED_ORIGINS` | browser access | preflight fails; the SPA cannot call the API |
| `RELIUM_CLERK_AUTHORIZED_PARTIES` | recommended | `azp` unchecked — a token minted for another frontend is replayable |

Each degradation is logged at boot with a warning naming the variable.

### Frontend environment variables

| Variable | Purpose |
|---|---|
| `VITE_CLERK_PUBLISHABLE_KEY` | Clerk in the browser. Publishable only — never a secret key |
| `VITE_RELIUM_API_URL` | API origin |

`VITE_RELIUM_GITHUB_APP_SLUG` is **no longer needed**: the install URL now
comes from the backend, derived from `GET /app`.

### Clerk configuration

- Organizations **enabled** — the tenancy model is one Clerk organization to one
  Relium tenant, and a personal account cannot own a workspace.
- Organization selection reachable during sign-in, or the frontend must offer
  it: a session with no active organization gets `current_step: "organization"`.
- After activating an organization the frontend **must** mint a fresh token
  (`getToken({ skipCache: true })`). The organization id is a claim inside the
  token; a cached one still says there is none, and the refusal looks like a
  Relium bug. Asserted by `test_the_stale_token_still_fails_after_activation`.
- Production instance issuer set in `RELIUM_CLERK_ISSUER`. No Clerk secret key
  is needed anywhere — verification uses the public JWKS.

### GitHub App configuration

Current production permissions, unchanged by this work:

| Permission | Level | Needed for |
|---|---|---|
| Checks | Read and write | PR review (pre-existing) |
| Contents | **Read-only** | reading `dbt_project.yml` and `relium.yml` |
| Issues | Read and write | PR comments (pre-existing) |
| Metadata | Read-only | repository listing |
| Pull requests | Read and write | PR review (pre-existing) |

**Onboarding needs no new permission.** Repository listing uses
`GET /installation/repositories`, which Metadata covers, and dbt detection uses
Contents read.

Still required, and **not code**:

1. **Setup URL** on the App set to `{RELIUM_PUBLIC_URL}/github/setup`. Without
   it the browser never returns and no binding is ever created.
2. **Callback URL** including `{RELIUM_PUBLIC_URL}/auth/github/link/callback`.
3. **Request user authorization (OAuth) during installation** enabled, or the
   identity link must be completed separately before installing.
4. Webhook subscribed to `installation` and `installation_repositories` in
   addition to `pull_request`.

### PostgreSQL migrations

| Version | Adds |
|---|---|
| 0014 | `tenants`, `tenant_onboarding_state` |
| 0015 | installation states, Clerk↔GitHub identities, installation facts, the binding |
| 0016 | `tenant_repositories`, completion audit columns |

Applied automatically at store construction. Verified: empty → `[1..16]`,
13→14→15→16 each preserve prior data, re-apply is a no-op.

---

## 5. Deliberately disabled: Actions-secret writing

The CI token is shown once and the customer creates the repository secret.
Writing it directly would be better — the value would never enter a browser —
and the code path exists and is tested. It is **off**, for three reasons, only
one of which is a coding task:

1. **The App has no Secrets permission.** Adding it widens what a compromise of
   the App private key reaches, and **suspends every existing installation**
   until an owner accepts the new scope. That is a customer-visible migration.
2. **The approved permission set would refuse the token.**
   `agent/github_app/auth.py` validates every installation token against
   `REQUIRED_INSTALLATION_TOKEN_PERMISSIONS` and **fails closed on any
   unapproved permission** — a token carrying `secrets` would be rejected,
   breaking PR review, until that set is deliberately changed.
3. **GitHub requires a libsodium sealed box.** `cryptography` does not
   implement `crypto_box_seal`; this needs PyNaCl in the hash-pinned
   dependency set. A hand-rolled approximation is not acceptable — a subtly
   wrong sealed box yields a secret that decrypts to nothing and a CI failure
   nobody can diagnose.

Residual risk accepted meanwhile: the token exists in browser memory, a DOM
node, and the clipboard. Mitigated by showing it once, never persisting it
client-side, and the `ci` scope being narrow enough that a leaked token can
submit manifests and nothing else.

---

## 6. Everything that still prevents production deployment

| # | Blocker | Owner |
|---|---|---|
| 1 | Frontend does not match the served contract (§3) | Phase 4 |
| 2 | GitHub App Setup URL and Callback URL not configured | Operator |
| 3 | Webhook not subscribed to `installation` events | Operator |
| 4 | Production Clerk instance issuer + authorized parties | Operator |
| 5 | `RELIUM_SESSION_ENCRYPTION_KEY` provisioned in production | Operator |
| 6 | `app.relium.dev` origin created and in `RELIUM_CORS_ALLOWED_ORIGINS` | Operator |
| 7 | Migrations 0014–0016 applied to production PostgreSQL | Operator |
| 8 | Actions-secret writing (§5) — optional, needs review | Decision |

None is a defect in the backend. Items 2–7 are configuration and deployment;
item 1 is frontend work; item 8 is a security decision.

---

## 7. What is genuinely production-real now

- Clerk session tokens verified server-side against Clerk's JWKS, with
  hardened fetching (bounded body, backoff, stale-key grace, no redirects, no
  token-controlled fetch target).
- One Clerk organization ↔ one Relium tenant, idempotent under concurrency by
  database constraint.
- Installation binding requiring three independent verifications; a spoofed
  `installation_id` cannot bind.
- Repository authorization resolved tenant → installation → numeric GitHub id
  on every request, with non-disclosing 404s.
- dbt configuration validated by the backend's own
  `validate_repository_relative_path`, and the generated `relium.yml`
  round-tripped through the real `load_repository_config`.
- CI tokens issued by the existing `issue_ci_token`, hash-only at rest, shown
  once, revoked on re-issue.
- Completion idempotent and safe under concurrent requests.
- Machine-token capabilities unchanged and regression-tested.
