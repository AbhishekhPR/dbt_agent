# Foundations 3 and 4 Lifecycle Hardening Design

## Scope

Foundation 3 adds a fail-closed Polar reconciliation and immediate-revocation boundary plus durable checkout idempotency. Foundation 4, stacked on Foundation 3, atomically revokes tenant-owned credentials and pending local work. Neither foundation deletes customer data, Clerk resources, GitHub installations, or external historical publications.

## Authority and isolation

Sensitive operations accept a verified `ClerkPrincipal`, refresh Clerk membership through `WorkspaceMembershipAuthorizer.require_owner`, and use the returned tenant context. Operational roots come only from Foundation 2's authoritative inventory. No public API accepts tenant, customer, subscription, token, organization, or repository ownership claims.

## Foundation 3

Migration 0023 introduces a per-tenant lifecycle control row, durable billing operations, provider subscription/check-out observations, and checkout intents. Provider calls occur outside database transactions. Short transactions claim an operation generation or checkout intent; completion uses compare-and-set semantics so crashes and retries remain visible.

Polar enumeration uses the tenant ID as authoritative `external_customer_id`, unions the locally persisted customer ID only as a compatibility lookup, follows every page, validates bounded response shapes, and rejects identity conflicts. Potentially billable, terminal, scheduled-cancel, and unknown states are distinct. Multiple subscriptions are preserved and reported.

Immediate revocation blocks new checkout first, reconciles, revokes every non-terminal subscription, records each result, and reconciles again. It reports safe only after complete provider evidence proves every subscription terminal/absent and no actionable checkout remains. A DELETE 404 is insufficient without a complete follow-up listing.

Normal entitlements remain exclusively signed-webhook driven. Reconciliation never writes `tenant_billing` plan/status. Checkout creation first reconciles, then claims one durable intent. Its server-generated ID is sent in Polar metadata; provider listing recovers a lost create response and duplicate matches fail closed.

## Foundation 4

Migration 0024 adds durable workspace credential-revocation results, cancellation states for actionable collection/outbox work, dashboard-session Clerk provenance, and constraints/pointer cleanup support. Token digest destruction (`secret_hash = NULL`) and corresponding identity/pointer/request/session revocation occur in one tenant-scoped transaction after owner authorization and complete Foundation 2 ownership inventory.

Every machine-auth and issuance path consults lifecycle state. Existing mapped CI/collector submissions stop immediately. Unmapped legacy roots keep ordinary compatibility but cannot be bulk-revoked. User GitHub OAuth revocation is a separate authenticated-user primitive; shared identities are never guessed from GitHub login or repository ownership.

Results contain counts, safe failure categories, and customer remediation codes only. They never contain tokens, hashes, encrypted credentials, DSNs, SQL, manifests, evidence, webhook bodies, or provider bodies.

## Failure behavior

Provider timeout, authentication failure, rate limit, 5xx, malformed/truncated pagination, unknown status, identity conflict, incomplete ownership, or active claimed work produces a durable retryable/blocked result. No failure is converted into deletion readiness. All operations are idempotent and tenant-isolated.
