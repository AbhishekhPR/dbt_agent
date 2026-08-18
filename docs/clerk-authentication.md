# Clerk authentication — operator and architecture notes

Phase 1 of the onboarding backend. Covers how a human is authenticated by
Clerk, what that does and does not authorize, how a Clerk organization becomes
a Relium tenant, and how the JWKS fetch behaves in production.

---

## 1. What Clerk answers, and what it does not

| Question | Answered by |
|---|---|
| Who is this human? | Clerk |
| Are they signed in? | Clerk, verified server-side against its JWKS |
| Which Relium tenant is this? | Relium, from the Clerk organization in the verified token |
| May they read a repository's reviews? | **Not Clerk** |
| May they approve a governance exception? | **GitHub**, re-verified live |

A Clerk session is identity. It is not authority over a customer's repository,
because Clerk knows nothing about that repository.

This is enforced, not merely intended. Human capabilities declare which
identity providers may hold them:

```
DASHBOARD_READ          human, {"github"}
GOVERNANCE_WRITE        human, {"github"}
COLLECTION_REQUEST_READ human, {"github"}   + token scopes
ONBOARDING_READ         human, {"clerk"}
ONBOARDING_WRITE        human, {"clerk"}
```

`ClerkPrincipal.may_govern` is `False` and `github_permission` is `None`,
permanently. A Clerk session presented to a dashboard or governance route is
refused by the capability check before any permission is consulted.

### Machine principals are untouched

Identity providers are a property of **human** principals only.
`COLLECTOR_INGEST`, `PIPELINE_INGEST` and `CI_MANIFEST_INGEST` are machine-only
(`human=False`) and carry an **empty** `human_identities` set — naming a
provider on them would suggest a human could hold one if they used the right
identity provider. They cannot: the `human` flag refuses every human first.

The machine branch of `authorize()` is unchanged, and
`test_machine_token_regression.py` pins collector, CI and operator-read token
capabilities exactly as they were, both as policy and against the served routes.

---

## 2. The identity chain

```
  Clerk session (browser)
    -> Authorization: Bearer <clerk session token>
      -> backend verifies signature against Clerk's JWKS, plus iss/exp/nbf/azp
        -> Clerk user id (sub) and active organization id
          -> Relium tenant, looked up by the organization id FROM THE TOKEN
            -> durable onboarding state for that tenant
```

Every arrow is server-side. The browser contributes the token and nothing else.
`user_id` and `organization_id` are never read from a request body, path or
query — the same rule `agent/api/auth.py` already applies to service tokens.

---

## 3. Clerk organization → Relium tenant

```
  Clerk User ─── member of ───► Clerk Organization  org_abc
                                        │
                                 (1:1, UNIQUE)
                                        ▼
                                 Relium Tenant     ten_xyz
```

| Rule | Why |
|---|---|
| `tenants.clerk_organization_id` is UNIQUE | Makes workspace creation idempotent, including under concurrency |
| Identity is the **immutable Clerk organization id** | It survives renames; a display name does not |
| `organization_name` is display only — editable, non-unique, never joined on | Two customers may legitimately share a name |
| A GitHub organization name is **never** matched against a Clerk organization | Anyone can create a GitHub org with a chosen name; matching by name is an account-takeover primitive |
| The tenant id is Relium's own (`ten_` + 32 hex), not the Clerk id | Our resource ids should not change if the identity provider ever does, and should not disclose which Clerk org a tenant is |

**A Clerk Organization is not a GitHub Organization.** They are different
objects, in different systems, with unrelated identifiers, created at different
times by different people. The GitHub account/installation is a separate
integration object, bound to the tenant in a later phase by a single-use
`state` value — never by name.

### Future multi-workspace evolution

The current assumption is **one active Clerk organization ↔ one Relium
tenant**. That can be relaxed later *without changing tenant identity
semantics*, because the identity is the pair `(clerk_organization_id →
tenant_id)` and not "the user's workspace":

- **A user in several Clerk organizations** already works. Each organization
  resolves to its own tenant; the active organization on the session selects
  which. Switching organizations in Clerk yields a new token with a different
  `org_id`, and the same endpoints then answer for the other tenant. No schema
  change.
- **Several workspaces inside one Clerk organization** would need a new
  identifier, not a redefinition of this one. The `UNIQUE` constraint would move
  to `(clerk_organization_id, workspace_key)` with an added column, and existing
  rows would take the default key. Tenant ids already issued stay valid and keep
  meaning the same thing.
- **Moving a tenant to a different Clerk organization** (a customer migrating
  Clerk instances) is an update to `clerk_organization_id` on a stable
  `tenant_id`. Because nothing downstream keys off the Clerk id — repositories,
  installations and configuration all reference `tenant_id` — this is a
  one-column change rather than a re-tenanting.

What must not change: the tenant id stays opaque and stable, and identity keeps
coming from an immutable identifier rather than a name.

---

## 4. The Workspace step, and the organization bootstrap

Clerk may have already created the organization before Relium onboarding
starts. The Workspace step therefore means:

> **Create or configure the Relium workspace associated with the active Clerk
> organization.**

It does **not** mean "create another Clerk Organization". Relium never creates
one: doing so would need a Clerk Secret Key in this backend, and would produce a
duplicate for a user Clerk may already have prompted.

`PUT /api/tenants` accordingly:

- derives `clerk_organization_id` from the verified session;
- **never** accepts it as request input (a body field of that name is ignored,
  and a test asserts it);
- creates or reuses the Relium tenant idempotently;
- accepts only mutable Relium metadata: `organization_name`, `role`,
  `team_size`.

### When no active Clerk organization exists

Two pre-tenant states, deliberately distinct, because the fix lives in a
different system for each:

| State | `current_step` | Meaning | Fixed in |
|---|---|---|---|
| No active Clerk organization | `organization` | Session has no `org_id` | **Clerk** |
| Organization but no tenant | `workspace` | First-run Relium setup | **Relium** |

`GET /api/onboarding/state` returns:

```json
{ "complete": false,
  "current_step": "organization",
  "code": "clerk_organization_required",
  "workspace": null, "github": null, "configuration": null }
```

`PUT /api/tenants` returns **409**, not 422 — the body may be perfectly valid;
it is the session state that cannot support a workspace:

```json
{ "status": "conflict", "code": "clerk_organization_required",
  "detail": "no active Clerk organization on this session" }
```

No tenant is created, and nothing is invented. The frontend matches on `code`
— never on prose — and sends the user to Clerk's organization
selection/creation flow.

### How the frontend gets a refreshed token afterwards

The organization id is a **claim inside the token**. A token minted before an
organization became active does not contain one, and will keep returning
`clerk_organization_required` until it is replaced. Activating an organization
in Clerk is therefore necessarily followed by minting a new token:

1. The user creates or selects an organization — via Clerk's
   `<OrganizationSwitcher>` / `<CreateOrganization>`, or Clerk's session-task
   flow if organization selection is configured as a required task.
2. The frontend makes it active: `await clerk.setActive({ organization })`.
   Clerk updates the session and the `org_id` claim.
3. The frontend obtains a **fresh** token, bypassing Clerk's short-lived cache:

   ```js
   const token = await session.getToken({ skipCache: true })
   ```

   The `skipCache` is the load-bearing part. Clerk caches the session token for
   about a minute; without it the browser re-sends the pre-organization token
   and the backend correctly refuses again, which looks like a bug in Relium.
4. The frontend retries `GET /api/onboarding/state` with the new token and gets
   `current_step: "workspace"`.

Relium performs no step of this. It reports the state and waits.

---

## 5. Configuration

No Clerk secret is required. Verification uses Clerk's **public** JWKS; no
Clerk Secret Key is read, stored or logged, and the frontend holds only the
publishable key.

| Variable | Required | Meaning |
|---|---|---|
| `RELIUM_CLERK_ISSUER` | yes, to enable Clerk | The Clerk instance, e.g. `https://<instance>.clerk.accounts.dev`. Must be https. Binds verification to one Clerk instance. |
| `RELIUM_CLERK_JWKS_URL` | no | Defaults to `{issuer}/.well-known/jwks.json`. Must be https. |
| `RELIUM_CLERK_AUTHORIZED_PARTIES` | recommended in production | Comma-separated frontend origins accepted in `azp`. Unset means unchecked. |
| `RELIUM_CLERK_AUDIENCE` | no | Comma-separated accepted `aud`. Clerk session tokens carry none by default. |
| `RELIUM_CLERK_LEEWAY_SECONDS` | no | Clock skew allowed on exp/nbf/iat. Default 5, maximum 300. |

A development Clerk instance and a production one differ by these values alone.
No instance hostname, publishable key or issuer is compiled in.

**When `RELIUM_CLERK_ISSUER` is unset**, Clerk is disabled. The deployment
starts normally, the onboarding routes are still served — a route that vanishes
when misconfigured is indistinguishable from one that was never deployed — and
they answer `503`, authenticating nobody.

---

## 6. Token verification

Each check has an attack behind it:

| Check | Without it |
|---|---|
| `alg` from our allow-list (RS256), never from the header | `alg: none` skips verification; `HS256` invites using the public key as an HMAC secret |
| `kid` must name a key fetched from Clerk | A token could supply its own verification key |
| Signature over the received bytes | Two different tokens could verify as one |
| `iss` exact match | Any other Clerk application's tokens would be accepted |
| `exp` required | An expired session never ends |
| `nbf` / `iat` when present | A not-yet-valid token is honoured early |
| `azp` against authorized parties | A token minted for another frontend is replayable here |
| `aud` when configured | — |

Refusals are uniform and non-specific. Naming which check failed would be an
oracle for forging tokens. No token, claim or segment appears in a log or a
response.

---

## 7. JWKS: caching, rotation, timeouts, bounds, SSRF

### The fetch target is configuration, never the token

`jwks_url` comes from `ClerkSettings`, read from the environment and required to
be https. **Nothing in a presented token can influence which host is
contacted** — not `iss`, not `kid`, not any header. The JOSE headers that name a
key location (`jku`, `x5u`, `x5c`, `jwk`) are not consulted at all; a test
greps the source to keep it that way. Honouring one would turn verification into
"trust whoever the token points at" and double as an SSRF primitive aimed at
whatever the backend can reach.

**Redirects are refused.** The configured URL passes an https check at load
time; following a redirect would let that host move the fetch to any other host
afterwards, including a link-local address such as `169.254.169.254`.

### Behaviour

| Concern | Behaviour | Default |
|---|---|---|
| Cache | Successful fetch reused | 600 s |
| Rotation | First unknown `kid` probes immediately | — |
| Rotation flood | Further unknown `kid`s rate-limited | 30 s cooldown |
| Retired keys | Key set replaced wholesale, never merged | — |
| Timeout | One socket timeout covering connect and read (urllib exposes one, not two) | 5 s |
| Response size | Bounded read; oversized body refused, not truncated | 256 KB |
| Failure backoff | Exponential from base, capped | 5 s → 300 s |
| Stale grace | Cached keys keep verifying while refreshes fail | 3600 s past expiry |
| Unusable JWKS entries | Skipped individually; non-RSA, non-`sig`, non-RS256, malformed, or < 2048-bit refused | — |

Two of these deserve their reasoning stated.

**Stale grace.** Signing keys are long-lived. Refusing every request during a
Clerk outage would sign every customer out at once, which is a worse outcome
than verifying against the last key set Clerk published. Verification stays
real — the signature is still checked against genuine Clerk keys — only their
freshness is relaxed, and only for a bounded window.

**Rotation cooldown measured from the last rotation probe.** Measuring it from
*any* fetch is a bug: the fetch that populated the cache would start the
cooldown, and the first unknown `kid` after it — the actual rotation — would be
refused for a whole TTL. It was caught by
`test_the_cooldown_eventually_allows_another_refresh`.

### Failure modes

| Situation | Response | Why |
|---|---|---|
| No/malformed/invalid/expired token | `401` | The credential is bad |
| Unknown `kid`, keys held and current | `401` | We have Clerk's keys; this is not one |
| JWKS unreachable, no keys ever held | `503` | We cannot judge the token — an outage, not a forgery |
| JWKS unreachable, keys within grace | normal | Verified against the last good set |
| Clerk not configured | `503` | Nothing can be verified |
| Authenticated, wrong capability | `403` | — |
| Verified session, no active organization | `409` + `code` | Session state, not a bad request |

---

## 8. Local testing

```bash
bash scripts/dev/onboarding_test_postgres.sh
export RELIUM_TEST_POSTGRES_DSN="postgresql://relium_validation:relium_validation_local_password@127.0.0.1:55461/relium_validation"
python -m unittest test_clerk_identity test_clerk_jwks test_onboarding_api test_onboarding_postgres test_machine_token_regression
```

The container is local-only and least-privileged, mirroring
`.github/workflows/test.yml` — the suite asserts the application role is not a
superuser, so a convenient-but-wrong test database is caught rather than
tolerated.

No test contacts Clerk or the network: signing keys are generated in-process
and the JWKS endpoint is a scripted stand-in, so every token is synthetic and
every failure mode — outage, rotation, oversized body, redirect — can be
produced deliberately.
