# Optional Slack publication sink design

## Scope

Add the smallest production-safe Slack output to the GitHub App runtime. The
sink is disabled by default, remains secondary to GitHub delivery, and sends
only BLOCK reviews unless WARN delivery is explicitly enabled. ALLOW and
neutral/skipped reviews do not alert. This work does not alter the legacy CLI
Slack helpers or send a real message during validation.

## Architecture

`PullRequestReviewRunner` remains the publication coordinator. It completes
the sticky comment and check run first, then invokes an injected
`SlackPublicationSink`. The sink uses the existing stable publication identity
(`repository id + immutable head SHA + enforcement mode`) and stores a `slack`
step in the repository-scoped publication journal.

The server constructs the sink from optional environment-backed settings and
injects it into the runner. With no Slack webhook configured, the sink is a
no-op and GitHub behavior is unchanged. Configuration secrets are excluded
from object representations and logs.

## Publication state and idempotency

The journal accepts `comment`, `check`, and `slack` steps. Before the first
network request, the Slack step is persisted as `started` with only safe
metadata. A completed or skipped step is reused on delivery replay. A
previously started step is treated as indeterminate and is not blindly sent
again, preserving the at-most-one behavior for duplicate deliveries when an
incoming webhook cannot be reconciled remotely.

Terminal states are `complete`, `skipped`, `failed`, or `indeterminate`.
Failures store only a bounded error category, never exception text or the
webhook URL. GitHub publication remains successful regardless of the Slack
terminal state.

## Message contract and redaction

The payload contains only:

- pull request number;
- repository full name;
- one affected model, or a safe fallback;
- health;
- material finding count;
- one affected KPI when available;
- the GitHub pull request link.

No finding evidence, SQL, manifest content, environment values, credentials,
or arbitrary exception text is included. All text sourced from review data is
collapsed to one line, length-bounded, and Slack-control characters are
escaped.

## Retry and failure handling

The transport uses a finite attempt count with exponential delay capped by
configuration. HTTP 429, HTTP 5xx, timeout, and connection errors are
retryable. Other HTTP responses fail immediately. Retry logs contain only the
publication identity, attempt number, and safe error category.

Slack exceptions are caught after the GitHub comment and check are durable.
They are represented in the runner response and journal but never propagate
through the GitHub delivery path.

## Configuration

- `RELIUM_SLACK_WEBHOOK_URL`: optional; absence disables Slack.
- `RELIUM_SLACK_NOTIFY_WARN`: optional boolean, default false.
- `RELIUM_SLACK_MAX_RETRIES`: optional non-negative integer, default 2.
- `RELIUM_SLACK_RETRY_BASE_SECONDS`: optional positive number, default 1.

BLOCK alerts are enabled whenever the webhook is configured. WARN alerts
require the explicit boolean. ALLOW remains silent.

## Validation

Unit and fake-receiver tests cover disabled-by-default behavior, BLOCK message
shape, configurable WARN, silent ALLOW, redaction, stable identity, duplicate
delivery, persisted state, bounded retry, and Slack failure after successful
GitHub publication. Existing comment/check pagination, crash recovery,
publication journaling, and full backend tests must remain green.

Real delivery is performed only when secure credentials identify an existing
dedicated Relium test channel. Otherwise the evidence verdict is
`BLOCKED BY CREDENTIALS` while fake-receiver E2E remains authoritative.
