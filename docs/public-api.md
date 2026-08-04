# Relium public lifecycle and dashboard API

The served application exposes the GitHub App webhook, health probes, and the
public lifecycle and dashboard API. `docs/api-contract.json` is generated from
the live route table and is verified against it by `test_api_contract.py`, so
an endpoint cannot be documented without being served.

## Boundaries

- Every `/api/*` route authenticates a service token. The GitHub webhook keeps
  its own HMAC signature authentication and never accepts a service token.
- Handlers call a service layer, which calls `PostgresLifecycleStore`. No
  handler touches a connection, cursor or SQL string.
- The API has no SQLite, in-memory, filesystem or fake persistence fallback.
  Without `RELIUM_DATABASE_URL` the `/api` routes are not registered at all.
- Tenant scope is resolved from the token. `organization_id`, `repository_id`
  and `environment` in a request body or path are never trusted for
  authorization.

## Authentication

Tokens are presented as `Authorization: Bearer rlm_<token_id>.<secret>`.

- Only `sha256(secret)` is stored; the secret is unrecoverable from the database.
- Verification looks the row up by the non-secret `token_id`, then compares
  digests with `hmac.compare_digest`, so the comparison is constant time. A
  missing token performs the same comparison to avoid a timing signal.
- A token carries an organization, a repository, and optionally one
  environment. An environment-scoped token pins every request to it.
- Tokens support expiry (`expires_at`) and revocation (`revoked_at`).
- Token values never appear in logs, responses, evidence or error bodies.

## Tenant non-disclosure

A resource outside the caller's scope is indistinguishable from one that does
not exist: both return `404` with the same body. Cross-tenant reads therefore
leak neither data nor existence.

## Idempotency

Every write requires a stable event identity, supplied as an `Idempotency-Key`
header or an `idempotency_key` field.

- The key and a hash of the canonical payload are persisted in `event_receipts`
  in PostgreSQL, not in application memory.
- A faithful replay returns the original effective result with `replayed: true`
  and a `200`.
- Reusing a key with a different payload returns `409`.
- Concurrent duplicates resolve through the primary key on `event_receipts`, so
  exactly one request performs the work.

## Status codes

| Code | Meaning |
| --- | --- |
| 200 | Successful read, or a documented idempotent replay |
| 201 | Resource newly created |
| 202 | Accepted for durable asynchronous processing |
| 400 | Malformed body (unparseable JSON, empty body) |
| 409 | Documented state conflict, including idempotency-key reuse |
| 413 | Payload above the API body limit |
| 422 | Body or query failed validation |
| 401 | Missing or invalid credentials |
| 404 | Not found within authorized scope, including out-of-scope resources |
| 500 | Unexpected failure, with no internal detail disclosed |

Every response carries a correlation `request_id`, echoed in the `X-Request-Id`
header. A caller-supplied `X-Request-Id` is preserved.

## Acceptance semantics

`202` means the request was authenticated, validated, and the event *and* its
outbox work were durably committed in PostgreSQL. It never means "queued in
memory". A process restart therefore cannot lose accepted work, and no write
endpoint reports success before durable persistence.

RCA is queued through the transactional outbox; acceptance never implies a
completed analysis. An incident with insufficient evidence is reported as
`unattributed` rather than being attributed to whichever deployment happened to
be most recent. No language model selects a root cause or supplies evidence.

## Pagination

Collections accept `limit` (default 25, maximum 100) and `offset`. Ordering is
deterministic — `created_at DESC` with the primary key as a tiebreaker — so a
repeated request returns the same page. Out-of-range or non-integer values are
rejected with `422`.

## Readiness

`GET /readyz` reports PostgreSQL reachability, whether migrations are current,
configuration presence, and outbox counts by state. It performs reads only and
never mutates lifecycle data. It discloses no configuration values.
