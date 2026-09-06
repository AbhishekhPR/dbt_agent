# Repository Selection Plan Policy Design

## Objective

Keep GitHub repository authorization and Relium repository entitlements visibly
and technically separate during onboarding. The dashboard must show every
repository returned by the tenant's verified GitHub App installation while
Relium independently controls whether an additional repository may be
connected: Free allows one, Starter allows three, and Pro is unlimited.

## Observed production cause

The QA tenant's active GitHub installation reported exactly one authorized
repository, so the single visible repository came from GitHub authorization,
not frontend truncation. At the same time the tenant had no verified live Polar
billing row, so the billing endpoint correctly resolved it to Free and the
repository step rendered the Free one-repository copy. These were two separate
facts presented too closely for the customer to distinguish.

The current frontend already renders the complete repository array returned by
`GET /api/onboarding/repositories`. The current backend also enforces the
repository limit transactionally in `select_tenant_repository`: it locks the
tenant row, counts existing tenant repositories, and refuses a new connection
that would exceed the effective plan. The repair must preserve both properties.

## Architecture

### Repository authorization

`RepositoryOnboardingService.list_repositories` remains the authorization
boundary. It obtains an installation token and returns only repositories GitHub
says that installation may access. The result is never expanded from local
database records and never reduced by a Relium plan.

`GET /api/onboarding/repositories` will continue returning that complete list.
Its response will additionally contain authorization metadata derived from the
same result and the tenant's verified installation binding:

- `authorized_count`: the number of repositories in the returned list;
- `github_installations`: active installation identifiers, account login, and
  account type needed to construct GitHub's installation-management link.

No repository outside the installation-token response is fabricated.

### Relium plan policy snapshot

The repository-list route will resolve the tenant's current entitlements before
building its response. It will return a separate `policy` object containing:

- the effective `plan` (`free`, `starter`, or `pro`);
- `repository_limit` (`1`, `3`, or `null` for unlimited);
- `connected_repository_count`, counted from `tenant_repositories`.

The effective plan and entitlement come from the existing billing access layer,
not from browser state. The route and the selection endpoint therefore use the
same server-side entitlement source. A repository refresh obtains repositories
and policy in one response, avoiding a race between separate billing and GitHub
requests.

The canonical entitlement catalog remains unchanged:

- Free: one repository;
- Starter: three repositories;
- Pro: unlimited repositories.

### Frontend behavior

The onboarding API translator will return `{ repositories, authorization,
policy }` rather than only an array. The parent onboarding component will store
that snapshot atomically and pass it to the repository step. Refreshing
repositories replaces both the authorization list and policy, so a Free to
Starter entitlement change enables newly permitted choices immediately without
remounting the component.

The repository step will always render every authorized repository. A row is
disabled only when either:

1. it has no detected dbt project, preserving current behavior; or
2. it is not already connected and `connected_repository_count` has reached a
   finite `repository_limit`.

Already-connected repositories remain selectable even when a workspace is at
or above its current limit. This matches the backend's idempotent re-selection
rule and prevents a downgrade from locking the customer out of repositories
they already connected.

When the plan limit blocks a row, the row remains visible and its disabled
reason identifies Relium's plan limit and the appropriate upgrade path. The
backend remains authoritative: direct requests and stale clients still pass
through the existing tenant-row lock, count, and insert-time limit check.

### Copy and GitHub management

Plan copy and GitHub authorization copy are separate elements:

- Free: `Relium Free includes up to 1 repository.`
- Starter: `Relium Starter includes up to 3 repositories.`
- Pro: no repository-count restriction is shown.
- Exactly one authorized repository: `Only 1 repository is currently
  authorized in GitHub. Manage GitHub access to make more repositories
  available in Relium.`

The GitHub message includes a `Manage GitHub access` action. Its URL is derived
only from the server-verified installation ID, account login, and account type
already returned by Relium:

- user installation: `https://github.com/settings/installations/{id}`;
- organization installation:
  `https://github.com/organizations/{login}/settings/installations/{id}`.

All URL path segments are encoded. No repository or installation identifier is
accepted from an editable form field.

## Failure behavior

If policy resolution fails, the repository route fails rather than labeling a
paid workspace Free. The frontend renders the existing actionable request
error and does not guess a plan or limit. If GitHub returns no repositories,
the existing no-authorized-repositories state remains and includes the same
management action when verified installation metadata is available.

If the policy changes between list and selection, the selection endpoint's
fresh transactional check wins. A `repository_limit_reached` response leaves
the repository visible and prompts the user to refresh or upgrade; it never
removes or reassigns a repository.

## Tests

Backend coverage will prove that the repository-list route:

- returns all GitHub-authorized repositories for Free, Starter, and Pro;
- reports policy snapshots of `1`, `3`, and `null` respectively;
- reports the connected count independently from the authorized count;
- exposes only verified installation metadata for GitHub management;
- continues refusing a second Free or fourth Starter repository inside the
  existing transaction while allowing Pro and idempotent re-selection.

Frontend coverage will prove:

- Free plus multiple authorized repositories shows every repository but allows
  only the remaining capacity;
- Starter plus multiple authorized repositories shows every repository and
  allows up to three;
- Pro plus multiple authorized repositories shows and enables all eligible
  repositories with no restriction copy;
- Starter plus one GitHub-authorized repository shows the GitHub authorization
  explanation and management action, not Free copy;
- a refreshed Free-to-Starter policy snapshot immediately enables repositories
  without remounting;
- an at-limit authorized repository remains visible but disabled with clear
  upgrade copy;
- management links use verified user or organization installation metadata.

Full relevant backend tests, frontend tests, production frontend build, diff
checks, and secret scans will run before opening the PRs. Nothing will be merged
or deployed automatically.
