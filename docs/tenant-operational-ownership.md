# Tenant operational ownership

`tenant_operational_roots` is the authoritative bridge between a Clerk-backed
workspace (`tenants`) and the legacy operational root (`organizations`). One
organization can belong to at most one tenant because `organization_id` is the
bridge primary key. Both parent foreign keys use `ON DELETE RESTRICT` so future
lifecycle code must explicitly account for the relationship.

Operational descendants are derived through their existing foreign keys:

```
tenants -> tenant_operational_roots -> organizations -> repositories
                                                     -> environments
                                                     -> reviews / attempts
                                                     -> manifests / evidence
                                                     -> collectors / snapshots
                                                     -> deployments / incidents
                                                     -> audit / delivery records
```

Tenant-native membership, onboarding, billing, GitHub installation, repository
selection, and detection records continue to use their existing `tenant_id`
relationships. `clerk_github_identities` is user-owned, OAuth authorization
state is ephemeral/shared, and migration history is deployment-owned; these
are explicitly excluded from workspace inventory.

## Establishment and backfill

New roots may be established by a repository selection only when the numeric
GitHub repository and installation are already bound to the tenant and the
legacy organization row is created in that same transaction. An existing
unmapped organization is never claimed by this path.

Migration 0021 establishes the bridge and consistency constraints under short
lock/statement timeouts. Migration 0022 performs the data scan only after the
DDL transaction has committed and released its locks. Startup migration runners
serialize per database with a PostgreSQL advisory lock.

The backfill and reconciliation command recognize one production backfill
proof: every repository under a legacy organization has exactly one CI token
whose opaque token identifier is already projected onto one tenant repository,
whose installation is bound to that same tenant, and all repositories resolve
to that single tenant. Partial, duplicate, mixed-tenant, or unmatched roots
remain unmapped. Display names, email addresses, GitHub usernames, billing
records, and onboarding answers are never ownership evidence.

The recorded source repository/token pair is historical establishment
provenance. A later guarded token rotation does not rewrite or invalidate that
proof while the source records and their tenant/root relationships still
exist.

The GitHub repository and dbt-detection projections gain `NOT VALID` composite
foreign keys to their tenant installation binding. Historical inconsistencies
remain readable for reconciliation, while PostgreSQL rejects new inconsistent
writes.

## Operator audit

The command is read-only by default and refuses to run until migration 0022 is
already applied; constructing the audit connection never applies migrations:

```
relium tenant-ownership-audit --json
relium tenant-ownership-audit --json --storage-root /path/to/storage
```

It reports mapped, unmapped, ambiguous, reconcilable, cross-tenant, orphan, and
optional numeric repository-directory counts. Cross-tenant diagnostics include
at most 100 safe identifiers per inconsistency kind and disclose when examples
were truncated. It never returns credentials, SQL, webhook bodies, manifests,
warehouse payloads, or evidence contents.

An operator may explicitly insert only the command's complete single-tenant CI
proofs in a serializable transaction:

```
relium tenant-ownership-audit --apply-unambiguous --json
```

Retries are idempotent. Unmapped and ambiguous roots are left unchanged and
must later block destructive lifecycle actions rather than being guessed.

`inventory_for_current_workspace` is an internal, read-only primitive. It
requires a refreshed Foundation 1 admin-or-owner authorization context and
derives the tenant ID from that server-side context. It is not exposed as a
browser-facing endpoint. A tenant with no operational records is complete; a
tenant whose projected CI token identifies an unmapped root is incomplete, and
a projection pointing at another tenant's mapped root is inconsistent.
