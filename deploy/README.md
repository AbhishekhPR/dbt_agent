# Controlled pilot deployment — Railway

Four resources, one design partner, one repository under review.

| Service | Repository | Start command | Public | Volume |
|---|---|---|---|---|
| `relium-api` | `dbt_agent` | `python -m agent.github_app.server` | yes | **yes** |
| `relium-worker` | `dbt_agent` | `python -m agent.worker.lifecycle_worker` | no | no |
| `relium-app` | `relium-app` | `node server.mjs` | yes | no |
| `relium-postgres` | Railway plugin | — | no | managed |

Config-as-code lives in `deploy/railway/api.json` and `deploy/railway/worker.json`;
point each Railway service at its own file. The frontend uses `railway.json` at
the root of its repository.

## Who owns the storage root

`RELIUM_STORAGE_ROOT` is **API-only**. Everything under it is reached through
`RepositoryStorage`, which is constructed in exactly one place — `build_application`
in `agent/github_app/server.py`. The lifecycle worker coordinates entirely through
PostgreSQL (`outbox_events`, `delivery_journal`) and requires no filesystem
configuration at all.

| Artifact | Path | Written by | Read by |
|---|---|---|---|
| Webhook delivery claim | `<root>/<repo_id>/deliveries/<delivery_id>` | API | API |
| Verified job + lease/retry | `<root>/<repo_id>/jobs/<delivery_id>.json` | API | API |
| Publication journal | `<root>/<repo_id>/publications/<id>.json` | API | API |
| Repository state cache | `<root>/<repo_id>/state/<key>.json` | API | API |
| Quarantined corrupt files | `<root>/<repo_id>/**/corrupt/` | API | operator |

So **attach one volume to `relium-api` and none to the worker**. Do not mount a
second volume at the same path on the worker: it would look identical and share
nothing, which is worse than having none, because the failure is silent.

`test_deployment_configuration.py::StorageRootOwnershipTests` fails the build if
`RepositoryStorage` is ever constructed outside the API, so this table cannot
quietly stop being true.

### Pilot limitation

**Single replica only, for `relium-api`.** The delivery claim is a file created
with `O_EXCL`, which coordinates within one filesystem and one host. Two API
replicas would each accept the same GitHub redelivery and publish twice.
`numReplicas` is pinned to 1 in `deploy/railway/api.json`.

**Long-term:** move the delivery claim, job store and publication journal into
PostgreSQL — `event_receipts` already carries `(organization_id, repository_id,
delivery_id)` semantics — after which the volume disappears and the API scales
horizontally. Out of scope for this pilot.

## Environment variables

### `relium-api`

| Variable | Required | Notes |
|---|---|---|
| `RELIUM_DATABASE_URL` | yes | `${{Postgres.DATABASE_URL}}`. PostgreSQL only; there is no SQLite fallback. |
| `RELIUM_STORAGE_ROOT` | yes | Volume mount path, e.g. `/data/relium`. |
| `RELIUM_GITHUB_APP_ID` | yes | |
| `RELIUM_GITHUB_WEBHOOK_SECRET` | yes | Must match the App's webhook secret. |
| `RELIUM_GITHUB_PRIVATE_KEY` | yes\* | The PEM itself. Literal `\n` is accepted. |
| `RELIUM_GITHUB_PRIVATE_KEY_PATH` | no\* | File alternative, for local and E2E. |
| `RELIUM_GITHUB_CLIENT_ID` | yes | Dashboard sign-in. |
| `RELIUM_GITHUB_CLIENT_SECRET` | yes | Dashboard sign-in. |
| `RELIUM_SESSION_ENCRYPTION_KEY` | yes | base64 of 32 random bytes. |
| `RELIUM_PUBLIC_URL` | yes | API origin, e.g. `https://api.relium.example.com`. |
| `RELIUM_DASHBOARD_URL` | yes | Dashboard origin, e.g. `https://app.relium.example.com`. |
| `RELIUM_DASHBOARD_ORGANIZATION` | yes | GitHub owner this deployment serves. |
| `RELIUM_DASHBOARD_REPOSITORY` | yes | GitHub repository this deployment serves. |
| `RELIUM_CORS_ALLOWED_ORIGINS` | yes | Exactly `RELIUM_DASHBOARD_URL`. `*` is rejected. |
| `RELIUM_SECURE_COOKIES` | yes | `true`. |
| `RELIUM_PORT` | yes | `${{PORT}}`. |
| `RELIUM_HOST` | no | Defaults to `0.0.0.0`. |
| `RELIUM_SLACK_WEBHOOK_URL` | no | Optional secondary output. |

\* Exactly one key source is required. **The inline value wins** when both are
set — a stale path in configuration must not outrank the secret an operator
injected. A malformed or passphrase-protected key stops the process at startup.

All seven sign-in variables (`CLIENT_ID`, `CLIENT_SECRET`,
`SESSION_ENCRYPTION_KEY`, `PUBLIC_URL`, `DASHBOARD_URL`,
`DASHBOARD_ORGANIZATION`, `DASHBOARD_REPOSITORY`) must be present together, or
the `/auth` routes are not registered and the dashboard has no way in. A
half-configured login looks like a boundary and is not one.

Set the App's **Callback URL** to `${RELIUM_PUBLIC_URL}/auth/github/callback`
and its **Webhook URL** to `${RELIUM_PUBLIC_URL}/github/webhook`.

### `relium-worker`

| Variable | Required | Notes |
|---|---|---|
| `RELIUM_DATABASE_URL` | yes | Same database as the API. |
| `RELIUM_GITHUB_APP_ID` | yes | Needed to sign App JWTs for publication. |
| `RELIUM_GITHUB_PRIVATE_KEY` | yes | Same rule as the API. |
| `RELIUM_WORKER_REQUIRE_PUBLISHER` | yes | `true`. Without it a misconfigured worker starts, queues every request-changes review, and delivers none. |
| `RELIUM_GITHUB_INSTALLATION_ID` | no | Resolved per repository when unset. |
| `RELIUM_SLACK_WEBHOOK_URL` | no | |

**No `RELIUM_STORAGE_ROOT`.** The worker needs no filesystem state.

### `relium-app`

| Variable | Required | Notes |
|---|---|---|
| `VITE_RELIUM_API_URL` | yes | Build-time. The API origin. |
| `VITE_RELIUM_OWNER` | no | Display only. |
| `VITE_RELIUM_REPOSITORY` | no | Display only. |
| `VITE_RELIUM_ENVIRONMENT` | no | Display only. |
| `PORT` | yes | Supplied by Railway. |

**No credential of any kind is a frontend variable.** Vite inlines env at build
time, so anything here ends up in the public bundle. `VITE_RELIUM_API_TOKEN` no
longer exists; the dashboard authenticates with an HttpOnly session cookie.

## Health and failure behaviour

- **API** — healthcheck `GET /readyz`. It returns 503 until the database is
  reachable **and** migrations are current, so a deploy with a pending migration
  never takes traffic. Migrations apply automatically when the store connects.
- **Worker** — no HTTP surface. It exits non-zero when `RELIUM_DATABASE_URL` is
  absent or is not a PostgreSQL DSN, when the private key is malformed, and when
  `RELIUM_WORKER_REQUIRE_PUBLISHER` is set without a configured App.
- **Frontend** — `npm run build` is the Railway build command, so a Vite failure
  fails the deployment before anything is served. `GET /healthz` is answered by
  the static server independently of the bundle.

## Order of operations

1. Provision PostgreSQL.
2. Deploy `relium-api` with the volume attached. Wait for `/readyz` to be green —
   this applies migrations 0001–0009.
3. Deploy `relium-worker`.
4. Build and deploy `relium-app` with `VITE_RELIUM_API_URL` pointing at the API.
5. Set the App's Callback URL and Webhook URL.
6. Issue a collector token: `relium issue-collector-token --organization … --repository … --environment production`.
