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

## Operator attestation of pre-tenant legacy roots

Some legacy roots predate the tenant plane entirely. `organizations` and
`repositories` hold TEXT names and nothing else — no GitHub repository id, no
installation id, no owner id — so for those roots there is no provider-issued
identifier anywhere in production from which ownership could be derived. The
audit correctly reports them as `partial_mapping` or `unmapped`, and the
lifecycle correctly refuses to delete a workspace that owns them.

`--apply-unambiguous` cannot help: it inserts only complete single-tenant CI
proofs, and these roots have none.

```
relium tenant-ownership-attest \
  --organization-id AbhishekhPR \
  --tenant-id ten_c12a0bc74dd94756a861ddfccf56e71d \
  --reason "pre-tenant legacy operational data reviewed by operator" \
  --confirm AbhishekhPR
```

### This is an attestation, not a proof

`ci_token_binding` is **derived**: the database can re-check it at any time from
the token, the repository projection and the installation binding.
`operator_attested_legacy` cannot be re-checked from anything, because there is
nothing left to check. The two are therefore kept apart at every level:

* A distinct `mapping_basis` value, so no reader can confuse them.
* A CHECK constraint requiring an attested row to carry **no** provider
  provenance columns at all. The schema itself refuses to let an attestation
  dress up as provider-verified evidence.
* `attested_operational_roots` on both the operational inventory and the
  lifecycle access inventory, so a caller that satisfies the ownership gate can
  still see that ownership rests on a human's claim.

Nothing in this path weakens `ci_token_binding`. An existing derived mapping is
never rewritten, never downgraded, and never reassigned.

### What the command refuses

Every refusal happens inside the SERIALIZABLE transaction and rolls it back
untouched. None of them overwrites anything.

| Refusal | Meaning |
|---|---|
| `--confirm` mismatch | The confirmation must equal `--organization-id` exactly. `Relium-site` and `relium-site` are different rows. |
| `reason_required` | An attestation with no recorded justification is not an attestation. |
| `organization_not_found` / `tenant_not_found` | Both identifiers must already exist. Nothing is created. |
| `mapped_to_other_tenant` | Another workspace already owns this root. Never reassigned. |
| `already_mapped_authoritatively` | Derived evidence outranks an attestation and is not downgraded to one. |
| `provable_without_attestation` | The root is CI-provable. Use `--apply-unambiguous`; a weaker claim must not be recorded where a stronger one is available. |
| `cross_tenant_inconsistency` | Derived evidence exists and points at a different tenant. An attestation covers an *absence* of evidence, never a disagreement with it. |
| `ambiguous_candidate_tenants` | Derived evidence names more than one workspace. An attestation must not be the thing that picks a winner. |

Repeating an identical attestation is a no-op: it returns `already_attested`
and writes neither a second mapping nor a second audit record.

### What it does not do

* It does **not** fabricate `tenant_repositories` rows. Individual repositories
  remain unprojected; only the root is attributed.
* It does **not** claim provider verification, and the schema prevents it.
* It does **not** infer anything. Both identifiers are supplied exactly by the
  operator and matched exactly.

### The audit record

`tenant_operational_root_attestations` stores the basis, organization id,
tenant id, timestamp and the operator's non-secret reason. It is workspace-owned:
durable for the life of the tenant, and purged with it, because a completed
deletion retains only a dissociated receipt and a row naming a deleted tenant
would contradict that.

### Verification

```
# Before: confirm the root really is unprovable.
relium tenant-ownership-audit --json

# After: the root is mapped, and the provenance is visible as attested.
relium tenant-ownership-audit --json
```
