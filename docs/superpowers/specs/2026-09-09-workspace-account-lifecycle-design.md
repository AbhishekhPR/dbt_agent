# Workspace and Account Lifecycle Design

Date: 2026-09-09

## Purpose

Provide production-safe, retryable controls for deleting a workspace, deleting
an account, leaving a workspace, disconnecting GitHub, and revoking collector
access. The design composes Foundations 1–4 without replacing their authority:

- Clerk membership projection establishes workspace roles and owner counts.
- `tenant_operational_roots` establishes tenant ownership of legacy data.
- Polar reconciliation and revocation establish that billing is terminal.
- Workspace credential revocation stops CI, collectors, dashboard sessions,
  queued work, and credential recreation races.

The legacy `delete_tenant(organization_id)` method is explicitly outside this
design and must never be called by lifecycle code.

## Trust and authorization

All user and tenant identity comes from a verified Clerk session. Request
bodies may contain confirmation text and subordinate resource identifiers, but
never an authoritative tenant, user, role, or ownership claim.

Workspace deletion and GitHub installation uninstall require a freshly
synchronized owner. Repository/installation disconnect and collector
revocation require admin or owner. A member or admin may leave; an owner may
leave only when the refreshed authoritative owner count is greater than one.
Account deletion obtains every organization membership from Clerk and blocks
before mutation if the user is sole owner anywhere.

Destructive operations require Clerk's signed factor-verification-age claim to
show recent strict verification. Impersonated sessions are rejected. Workspace
confirmation is byte-for-byte equal to the persisted workspace name; account
confirmation is exactly `DELETE MY ACCOUNT`.

## Durable operations

One unfinished workspace lifecycle operation is allowed per tenant and one
unfinished account lifecycle operation per Clerk user. Operations use leases
and monotonically increasing generations so concurrent workers cannot commit
stale results. Provider calls occur outside database transactions; every call
is preceded by a durable claim and followed by a short result transaction.

Workspace phases are:

1. `requested`
2. `frozen`
3. `billing_reconciliation`
4. `billing_revoked`
5. `github_access_revocation`
6. `credentials_revoked`
7. `artifact_purge`
8. `artifacts_purged`
9. `database_purge`
10. `clerk_organization_deletion`
11. `local_finalize`
12. `completed`

Each unfinished operation also has a disposition of `running`, `retryable`, or
`blocked`, plus a bounded non-secret failure category. Provider responses,
credentials, token digests, SQL, evidence, manifests, and webhook bodies are
never recorded.

## Workspace freeze

The first destructive mutation locks the tenant and atomically changes its
lifecycle controls to frozen/deleting, blocks billing checkout creation, and
starts credential revocation. Every creation/admission path checks the same
locked lifecycle row: checkout, tokens, GitHub/repository binding, reviews,
collector submissions and requests, queued publications, and dashboard
sessions. Verified Polar webhooks remain accepted so terminal billing evidence
can still be recorded. Lifecycle status remains accessible to the initiating
Clerk identity while normal product writes are frozen.

## Workspace deletion

Deletion advances only when:

- Foundation 2 inventory is complete and consistent;
- Foundation 3 has discovered every associated Polar checkout/subscription,
  revoked all potentially billable subscriptions, and re-read a terminal,
  non-actionable provider state;
- every tenant GitHub installation is absent at GitHub (an unambiguous 404 is
  already absent; timeout, authorization failure, rate limit, 5xx, or uncertain
  state blocks advancement);
- Foundation 4 credential revocation is complete with no claimed work left.

Repository storage is selected only through Foundation 2 numeric repository
ownership. Purging is symlink-safe and idempotent. The database purge removes
all mapped legacy descendants and roots plus tenant-owned records in explicit
foreign-key order. The operation, tenant, membership authority, and minimum
lifecycle evidence remain until Clerk organization deletion succeeds.

Clerk organization deletion is the last external operation. After verified
success or verified absence, one final local transaction writes a dissociated
receipt and removes the remaining tenant and operation data. The receipt has no
tenant, user, Clerk, GitHub, Polar, repository, organization, or customer
identifier; it contains only operation kind, timestamps, aggregate counts,
terminal proof booleans, and customer-remediation flags.

## Account deletion and leaving

Account deletion lists authoritative Clerk memberships and takes stable
organization snapshots before any mutation. If the user is sole owner of any
organization, the request returns `409 sole_owner` without leaving anything.
Otherwise each shared membership is removed through Clerk, attributable Relium
dashboard sessions and GitHub OAuth credentials are revoked, and optional user
attribution in retained shared-workspace records is replaced with an unlinkable
per-deletion actor value. Shared workspace billing, repositories, reviews,
evidence, and configuration are never deleted. Clerk user deletion is last,
followed by a dissociated receipt.

Leaving the active workspace performs the same stable owner-count check, calls
Clerk for the current membership, then invalidates the local projection. It
does not change shared workspace operational or billing data.

## GitHub disconnection

Four operations remain distinct:

- Personal identity disconnect revokes only the current user's stored GitHub
  OAuth credential and attributable dashboard sessions.
- Repository disconnect marks the tenant repository disconnected, revokes its
  CI/collector access, and cancels actionable collection work while retaining
  history and Foundation 2 ownership.
- Installation disconnect applies repository disconnection to the bound
  installation but leaves the GitHub App installed.
- Uninstall performs local revocation first, invokes GitHub's authenticated-app
  installation deletion, and verifies absence before recording completion.

Historical GitHub comments/checks and Slack publications are external records
and are accurately reported as retained.

## Collector controls

Per-token and revoke-all actions resolve the active tenant server-side and
reuse coordinated Foundation 4 transactions. Token digest, collector identity,
repository pointers, and pending requests change together. Responses contain
only counts and explicit instructions to rotate/drop the customer warehouse
role, remove collector configuration, and remove `RELIUM_CI_TOKEN` from GitHub
Actions where applicable.

## Failure and retry behavior

All steps are idempotent. Network ambiguity blocks progression. A provider 404
is success only for an endpoint whose contract unambiguously means the target
is absent. A process crash leaves a durable phase and an expiring lease. The
customer endpoint and an operator CLI may resume the same operation; neither
may skip prerequisites or change its tenant/user scope. No cleanup compensates
for a failed billing, ownership, GitHub, credential, or Clerk proof.

## UI

The Settings Danger Zone is wired only after backend semantics pass review. It
renders server-derived capabilities and blockers, uses Clerk reverification,
requires exact typed confirmations, polls durable operation status, and clearly
separates local GitHub disconnect from provider uninstall and customer-side
secret/warehouse cleanup.
