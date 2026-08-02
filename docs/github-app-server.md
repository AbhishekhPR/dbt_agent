# Relium GitHub App server

## Framework decision and implementation plan

The HTTP boundary uses Starlette with Uvicorn. Starlette provides raw ASGI request
streaming, an in-process test client, and explicit lifespan hooks without the
request-schema and dependency-injection surface of FastAPI. Uvicorn supplies the
production ASGI process and graceful signal handling. HTTPX is used only by the
in-process Starlette test client. The selected version ranges resolve for Python
3.10, which is the repository workflow version.

The implementation keeps business logic framework-neutral:

- `settings.py` validates immutable startup configuration without exposing secrets.
- `jobs.py` owns the immutable raw webhook job, bounded worker queue, retry
  classification, capped backoff, and graceful lifecycle.
- `service.py` processes one job by invoking the existing `GitHubAppAdapter`; it
  does not recreate signature, event, authentication, client, idempotency, review,
  comment, or check-run behavior.
- `http_app.py` owns raw-body limits, required headers, synchronous signature
  verification, supported-event validation, enqueue responses, and health state.
- `server.py` composes Phase 1 objects and runs Uvicorn only from an explicit
  module entrypoint.

The request thread verifies the exact raw bytes, validates the existing webhook
parser, and performs only a non-blocking queue insertion. A fixed number of worker
threads invoke the adapter. Retryable failures are limited to network transport
errors, GitHub 429/5xx responses, and explicitly temporary service errors. Invalid
signatures, payloads, configuration, credentials, GitHub 401/403 responses, and
deterministic review failures are not retried.

Security boundaries are explicit: raw bodies and signature headers never enter
logs; jobs contain no credentials; errors use fixed categories; queues and workers
are bounded; settings load only at startup; and every unit test injects fake
transports or adapters rather than contacting GitHub.

## Runtime architecture

GitHub sends `POST /github/webhook`. The ASGI boundary streams the request into a
bounded byte buffer, validates the three GitHub headers, verifies the SHA-256 HMAC
over those exact bytes, and uses the existing webhook parser to reject malformed or
unsupported pull-request payloads. Supported deliveries are copied into an
immutable credential-free job and inserted without waiting. A fixed worker pool
then invokes the existing adapter, which obtains the installation token and runs
the existing idempotent pull-request review. `GET /healthz` reports readiness only
after the worker queue has started.

## Configuration

Set these environment variables before startup:

- `RELIUM_GITHUB_APP_ID`: positive numeric GitHub App ID.
- `RELIUM_GITHUB_WEBHOOK_SECRET`: webhook secret configured in GitHub.
- `RELIUM_GITHUB_PRIVATE_KEY_PATH`: readable PEM private-key file.
- `RELIUM_STORAGE_ROOT`: dedicated local directory for repository state and
  delivery claims.
- `RELIUM_WORKER_COUNT`: fixed worker count; default `2`.
- `RELIUM_QUEUE_CAPACITY`: maximum waiting jobs; default `100`.
- `RELIUM_MAX_RETRIES`: retry count after the initial attempt; default `3`.
- `RELIUM_RETRY_BASE_SECONDS`: exponential backoff base; default `1`.
- `RELIUM_HOST`: bind address; default `0.0.0.0`.
- `RELIUM_PORT`: bind port; default `8000`.
- `RELIUM_MAX_BODY_BYTES`: webhook body limit; default `2097152`.
- `RELIUM_SLACK_WEBHOOK_URL`: optional Slack or Slack Gov HTTPS incoming
  webhook. If absent, Slack output is disabled.
- `RELIUM_SLACK_NOTIFY_WARN`: send WARN reviews as well as BLOCK; default
  `false`.
- `RELIUM_SLACK_MAX_RETRIES`: retry count after the initial Slack attempt,
  from `0` through `5`; default `2`.
- `RELIUM_SLACK_RETRY_BASE_SECONDS`: Slack exponential backoff base greater
  than `0` and no more than `10`; default `1`. Each delay is capped at ten
  seconds.

Settings are loaded only by the explicit server entrypoint. Secrets and private-key
contents are excluded from settings representations, responses, and operational
logs.

Slack is a secondary output. GitHub comment/check publication completes first,
and Slack state is then recorded in the same repository-scoped publication
journal. Slack failure cannot roll back or fail completed GitHub publication.
ALLOW and neutral results remain silent. Payloads contain a bounded repository,
model, health, finding count, optional KPI, and GitHub review link; they never
contain SQL or finding evidence.

## GitHub App configuration

Grant only the permissions used by Phase 1:

- repository contents: read;
- pull requests: write, for pull-request subject comments;
- issues: write, for the sticky review comment;
- checks: write, for the Relium check run;
- metadata: read.

Installation tokens inherit this minimal permission set. The server rejects a
token before publication if GitHub returns a missing, downgraded, or additional
permission.

Subscribe to the **Pull request** webhook event. Configure the webhook URL as
`https://<service-host>/github/webhook`, use JSON content type, and set the same
webhook secret supplied to the server.

## Local startup and health

Create a throwaway development key and secret outside version control, export the
required variables, then run:

```bash
python -m agent.github_app.server
```

The module does not start a server when imported. Check readiness with:

```bash
curl --fail http://127.0.0.1:8000/healthz
```

## Local signed webhook test

Save a representative GitHub pull-request payload as `webhook.json`. Calculate the
signature without printing the secret:

```bash
signature="$(python - <<'PY'
import hashlib
import hmac
import os
from pathlib import Path

body = Path("webhook.json").read_bytes()
digest = hmac.new(
    os.environ["RELIUM_GITHUB_WEBHOOK_SECRET"].encode(),
    body,
    hashlib.sha256,
).hexdigest()
print("sha256=" + digest)
PY
)"
curl --fail-with-body \
  -X POST http://127.0.0.1:8000/github/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: ${signature}" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: local-delivery-1" \
  --data-binary @webhook.json
```

For GitHub delivery to a developer machine, use any HTTPS tunnel that preserves
request bodies and headers byte-for-byte. The server does not depend on a specific
tunnel vendor. Never place the webhook secret or private key in tunnel arguments or
public configuration.

## Queue, retry, storage, and shutdown

The queue and worker count are fixed at startup. A full or stopped queue returns
`503`; accepted work returns `202` before any GitHub API call. Workers isolate job
failures so one delivery cannot terminate a worker.

Network failures, GitHub 429 responses, GitHub 5xx responses, and explicitly
temporary service failures use capped exponential backoff. GitHub 401/403/404,
invalid signatures or payloads, repository configuration failures, invalid app
credentials, and deterministic Relium failures are not retried. Retry logs contain
only delivery metadata and a fixed error category.

The existing filesystem storage records repository-scoped delivery claims with
restrictive file modes. Use durable local storage and back it up according to the
deployment recovery policy. This phase deliberately does not coordinate delivery
claims across hosts or processes.

SIGTERM and SIGINT are handled by Uvicorn. Shutdown stops accepting jobs, queues
sentinels behind accepted work, and waits only for the configured bounded shutdown
period. Worker threads are bounded and do not prevent forced process exit after the
grace period.

## Security limitations

- Run exactly one application process for this phase. Multiple Uvicorn workers
  would create independent in-memory queues and are outside the delivery guarantees.
- Terminate TLS at a trusted ingress and restrict direct access to the application.
- The in-memory queue is not durable; GitHub redelivery is the recovery mechanism
  for work lost during process failure.
- Installation tokens are intentionally not cached and are never written to jobs,
  storage, responses, or logs.
- The service does not clone repositories, execute repository code, run dbt, access
  warehouses, or accept arbitrary callback URLs.
- Keep private-key and storage permissions restricted to the service account.

## Production deployment checklist

- Install the pinned-compatible ranges from `requirements.txt` with Python 3.10 or
  a tested newer version.
- Mount the GitHub private key read-only and storage on a durable writable volume.
- Supply every required environment variable through the deployment secret store.
- Configure the GitHub App permissions and Pull request webhook event above.
- Run one process with `python -m agent.github_app.server`; do not enable multiple
  Uvicorn workers in this phase.
- Configure TLS, request timeouts, a two-megabyte-or-smaller ingress body limit, and
  health checks against `/healthz`.
- Send SIGTERM during rollout and allow the shutdown grace period before force kill.
- Alert on queue-unavailable responses, worker startup failures, exhausted retries,
  and repeated safe error categories.
- Verify a signed test delivery and confirm the Relium-owned comment and check run
  appear without secrets in application logs.
