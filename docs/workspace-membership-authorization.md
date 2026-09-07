# Workspace membership authorization

Clerk organizations are the authoritative source for Relium workspace
membership. PostgreSQL stores a projection so every authorization decision has
durable membership provenance, an exact synchronization generation, and an
auditable ambiguity state.

The verified Clerk session still selects the active organization. Relium looks
up that organization's tenant server-side; request bodies, paths, and query
parameters cannot select a different tenant or user.

## Roles

- `owner`: an exact Clerk `owner`/`org:owner` role. For organizations with no
  explicit owner role, Clerk's active `created_by` membership is the owner.
- `admin`: an exact Clerk `admin`/`org:admin` role.
- `member`: every other verified active Clerk membership.

If no explicit owner and no active Clerk-confirmed creator exist, ownership is
ambiguous. Relium does not promote an administrator or consult onboarding,
GitHub, billing, repository, or email data. Normal product behavior continues;
sensitive owner/admin helpers fail closed.

The organization role retained on `ClerkPrincipal` is authenticated context,
not an authorization shortcut. Sensitive checks fetch the complete current
Clerk membership list and authorize from the generation persisted by that
fetch.

## Reconciliation

The migration performs no ownership backfill. Operators can inspect the
authoritative mapping without writing anything:

```console
python -m agent.workspace_membership_reconcile
```

After reviewing the report, explicitly apply complete snapshots:

```console
python -m agent.workspace_membership_reconcile --apply
```

Both commands require `RELIUM_DATABASE_URL` and the server-only
`RELIUM_CLERK_SECRET_KEY`. An incomplete, malformed, cross-organization, or
unavailable Clerk response is reported as failed and never authorizes from a
previous projection.
