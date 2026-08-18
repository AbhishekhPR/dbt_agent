# Tenant ↔ GitHub App installation binding

Phase 2 of the onboarding backend. How Relium answers, securely:

> Which verified GitHub App installations belong to this authenticated Relium
> tenant?

---

## 1. The threat

After someone installs the GitHub App, GitHub redirects their browser to the
App's Setup URL with `?installation_id=…`. **GitHub's own documentation warns
that this query parameter can be spoofed.** Anyone can type a number into a URL
and send it to a victim, or visit it themselves.

If Relium bound on that number, an attacker could attach someone else's
installation — and therefore someone else's repositories — to their own
workspace. The browser redirect is UX. It is not proof.

## 2. Three independent verifications

A row in `tenant_github_installations` requires all three, from three different
sources, none of them the browser:

| # | Question | Source |
|---|---|---|
| 1 | Who started this flow? | Single-use server-side state |
| 2 | What is this installation? | `GET /app/installations/{id}` as the **App** |
| 3 | Is this human really associated with it? | `GET /user/installations` as the **person** |

**Fact 3 is the one that makes the other two into a binding.** Without it, 1
and 2 together still permit the attack: a legitimate user starts a real flow,
then substitutes a victim's installation id into the redirect. The state is
valid, the installation is real and is ours — and the binding would be wrong.
An attacker can name any installation; they cannot make GitHub list one they
have no access to under their own token.

**When fact 3 cannot be established, nothing is bound.** The flow stops and
reports `github_identity_required`. It is never completed by falling back to
the browser's number.

## 3. Installation state

Opaque, high-entropy, hashed at rest, tenant-bound, user-bound, single-use,
short-lived — the same pattern as `oauth_states` in migration 0009.

| Property | How |
|---|---|
| Entropy | `secrets.token_urlsafe(32)` |
| At rest | Only `sha256(value)`; the value exists in the URL and the browser and nowhere else |
| Expiry | 10 minutes, matching `OAUTH_STATE_LIFETIME` |
| Single use | A conditional `UPDATE`, so the check and the claim are one atomic statement |
| Tenant-bound | `tenant_id` on the row, read back at consume time — **never in the state value** |
| User-bound | `clerk_user_id` on the row; a Clerk session present at the redirect must match |
| Purpose-bound | An identity-link state cannot be spent on an installation |

Deliberately **not a JWT**: a signed token would put the tenant id in something
the browser holds, and would need out-of-band revocation to be single-use. An
opaque row is single-use by an `UPDATE`.

```sql
UPDATE github_installation_states SET consumed_at = %s
WHERE state_hash = %s AND purpose = %s
  AND consumed_at IS NULL AND expires_at > %s
RETURNING …
```

The guard is in the `WHERE`. A read-then-write would let two concurrent
redirects — a double-click, a retry, a deliberate replay — both observe an
unconsumed state and both proceed. Here PostgreSQL serialises on the row and
exactly one caller gets a row back. Expiry is inside the same guard, so an
expired state cannot be claimed by racing a cleanup job that has not run.

## 4. GitHub human verification

Clerk is the Relium login identity. It knows nothing about GitHub, so it cannot
answer "is this person associated with this installation".

`clerk_github_identities` holds a **verified** link, established only by
completing GitHub OAuth as that account, reusing `agent/api/github_identity.py`
unchanged. It stores the immutable numeric `github_user_id` and the user
credential, AES-256-GCM encrypted with the Clerk user id as associated data, so
a row lifted into another user's record fails to decrypt.

**Nothing is inferred.** Not from a Clerk email, a Clerk organization name, a
GitHub organization name, or an installation account login — all mutable,
unverified, or attacker-chosen.

This credential is **separate from the dashboard session** in
`agent/api/sessions.py`, deliberately. That one is scoped to a configured
repository and re-verifies a repository permission; this one exists before any
repository is known. Neither grants the other's authority, and this one grants
no governance capability at all. **No existing GitHub authorization check was
weakened, relaxed or shared.**

### The three principals

| Principal | Proves | Cannot |
|---|---|---|
| Clerk session | Relium login identity, tenant | Say anything about GitHub |
| GitHub user credential | This human's GitHub authority | Act as the App |
| GitHub App installation | Relium's machine repository access | Say who a person is |

Tested explicitly: a Clerk session is refused on every machine capability, a
service token is refused on every onboarding route, and neither the App
credential nor the Clerk session can stand in for the human check.

## 5. Endpoints

| Route | Auth | Does |
|---|---|---|
| `POST /api/onboarding/github/identity` | Clerk | Returns a GitHub OAuth URL to link a human identity |
| `POST /api/onboarding/github/install` | Clerk | Mints a state, returns the install URL |
| `GET /auth/github/link/callback` | state | Completes the OAuth link |
| `GET /github/setup` | state | Verifies and binds |

The install URL slug comes from **`GET /app` with the App JWT** — the App the
backend actually authenticates as. Not configuration, not the frontend, and
never `relium-e2e`. A test asserts the slug is the App's own and that
`relium-e2e` never appears.

`install` refuses up front when no GitHub identity is linked, so a customer is
told before installing the App rather than after.

## 6. Redirect / webhook race

The two arrival orders converge because facts and binding live in **separate
tables**:

- `github_installations` — what GitHub says. Tenant-agnostic, upsert-keyed on
  the installation id, writable by the webhook, which genuinely cannot know a
  tenant.
- `tenant_github_installations` — the binding. Written only after all three
  verifications.

| Scenario | Result |
|---|---|
| Webhook first, then redirect | Facts recorded, then bound. One row |
| Redirect first, then webhook | Bound, then facts refreshed. Binding untouched |
| Duplicate delivery | Upsert; no second row |
| Browser retry | State already consumed → refused; existing binding intact |
| Concurrent redirects | Exactly one binds |
| Re-running the same binding | Idempotent, `created=false`, no error |

**The webhook never binds a tenant.** A signature-verified delivery proves
GitHub sent it; it does not say which Relium customer installed the App.
Nothing in the payload identifies a tenant, and the fields that look like they
might — account login, sender — are names anyone can choose.

A repository-selection delivery for an installation Relium has never seen
creates nothing: an unverified installation must not spring into existence
because a selection changed.

## 7. Cross-tenant protection

`tenant_github_installations.github_installation_id` is the **PRIMARY KEY**, so
"each installation belongs to exactly one tenant" is enforced by the database,
not application logic. A second tenant claiming it raises
`TenantInstallationConflict` → 409. **Never re-pointed**, which would hand one
customer's repositories to another.

Two independent layers, verified: with the human check removed, the forged-id
tests fail — and the second one still fails on the *database* refusing to
re-point. Neither layer alone is relied on.

## 8. Failure codes

Stable and machine-readable; the frontend branches on these, never on prose.

| Code | Meaning |
|---|---|
| `installation_state_invalid` | Unknown, expired, consumed, tampered, wrong user, or wrong purpose — all identical, so refusal is not an oracle |
| `installation_unknown` | Not an installation of this App |
| `github_identity_required` | No verified GitHub identity linked |
| `github_identity_unusable` | Linked credential expired or undecryptable |
| `installation_not_authorized` | The human cannot see that installation — **the forged-id signature** |
| `installation_already_connected` | Bound to a different tenant |
| `github_unavailable` | GitHub unreachable |
| `workspace_required` | No Relium tenant yet |

The presented state and the installation id are **never** echoed into a
redirect, a log or a response.

## 9. Onboarding state

```json
"github": {
  "status": "connected",
  "installations": [{
    "installation_id": 48219371,
    "account_login": "acme-analytics",
    "account_type": "Organization",
    "account_id": 5001,
    "repository_selection": "selected",
    "status": "active",
    "connected_at": "2026-08-18T09:22:10Z"
  }],
  "identity": { "linked": true, "login": "alice" }
}
```

`status` is `not_connected`, `connected`, or `suspended` — the last because an
installation GitHub has suspended is present but not working, and reporting
"connected" would send the customer after the wrong problem. Deletion is soft:
history survives, but it leaves the connected set.

**No installation access token appears here, and none is stored.**

## 10. Not implemented (Phase 3+)

Repository listing and selection, dbt configuration, `relium.yml`, CI token
issuance, onboarding completion, frontend wiring.
