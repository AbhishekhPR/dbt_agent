# Docker smoke deployment

This topology exercises the existing production contracts with four services:
PostgreSQL, the Relium API, the lifecycle worker, and the production frontend
static server. The API and lifecycle worker use the same backend image and the
same `RELIUM_DATABASE_URL`, but remain separate processes.

Run Compose from the backend repository and use a unique project name so the
network, database volume, and API storage volume remain isolated:

```powershell
docker compose -p relium-smoke -f compose.smoke.yaml up -d postgres api web
```

Before running, set these values in the current shell:

- `RELIUM_SMOKE_POSTGRES_PASSWORD`: a generated local-only password containing
  URL-safe characters.
- `RELIUM_SMOKE_WEBHOOK_SECRET`: a generated local-only value.
- `RELIUM_SMOKE_PRIVATE_KEY_FILE`: absolute path to an ephemeral unencrypted RSA
  PEM file. Compose mounts it at runtime; it is never copied into an image.

The default host ports are PostgreSQL `55443`, API `8199`, and web `5280`.
They can be overridden with the corresponding `RELIUM_SMOKE_*_PORT` variables.

## Controlled outbox proof

Keep `worker` stopped initially. Issue a tenant-scoped token with the existing
operator CLI, retain the last output line only in memory, and submit a supported
deployment event to `POST /api/deployments/events` with an idempotency key. A
`created` event for a new deployment transactionally produces one
`deployment.reviewed` outbox event in `PENDING` state.

Query that row through `psql` in the PostgreSQL container, start the worker with
the normal service command, and query again until it is `COMPLETED`:

```powershell
docker compose -p relium-smoke -f compose.smoke.yaml up -d worker
```

The completed row preserves `lease_owner`, `attempts`, `completed_at`, and
`last_error`. The matching `worker:lifecycle` audit event proves the registered
handler ran; the dead-letter table must remain empty. Do not update these rows
manually.

For persistence, restart `api` and `worker` without removing the PostgreSQL
volume, then verify the deployment, audit entry, and completed outbox event are
unchanged and no additional attempt was recorded.

The publisher fail-closed check is a separate one-shot run of the worker image
with `RELIUM_WORKER_REQUIRE_PUBLISHER=true` and both GitHub App configuration
variables removed. It must exit with status 2 before polling.

Stop the smoke services without deleting their volumes:

```powershell
docker compose -p relium-smoke -f compose.smoke.yaml down
```

Use `down --volumes` only when intentionally discarding the isolated smoke-test
state. Never point this file at the existing Relium or customer databases.
