# Optional Slack sink evidence

## Result

Fake-receiver E2E: **PASS**

Real Slack delivery: **BLOCKED BY CREDENTIALS**

No Slack webhook credential or dedicated Relium test-channel configuration was
available. No external Slack request was attempted.

## Validated behavior

- Disabled by default when `RELIUM_SLACK_WEBHOOK_URL` is absent.
- BLOCK publishes one SQL-free message after GitHub comment and check completion.
- WARN publishes only when `RELIUM_SLACK_NOTIFY_WARN=true`.
- ALLOW, skipped, and neutral results publish no Slack message.
- Stable journal key: repository id, immutable head SHA, and enforcement mode.
- Concurrent redeliveries have one atomic Slack claim winner.
- ALLOW and unconfigured WARN decisions persist directly as skipped without a send intent.
- Duplicate GitHub delivery: one durable job and exactly one fake-receiver request.
- Publication journal reload preserves the Slack terminal state.
- A prior indeterminate Slack intent is not blindly resent.
- A stale `started` observation cannot overwrite a concurrently completed publication.
- Slack failure leaves the completed GitHub comment and check unchanged.
- Retry is bounded for HTTP 429, HTTP 5xx, timeout, and connection failure.
- Retry delay is capped at ten seconds and webhook configuration accepts only Slack HTTPS endpoints.

## Fake-receiver observations

| Case | Requests | Attempts | Result |
|---|---:|---:|---|
| BLOCK accepted | 1 | 1 | PASS |
| WARN enabled and accepted | 1 | 1 | PASS |
| ALLOW | 0 | 0 | PASS |
| WARN disabled | 0 | 0 | PASS |
| HTTP 500 then success | 2 | 2 | PASS |
| HTTP 429 then success | 2 | 2 | PASS |
| HTTP 400 | 1 | 1 | PASS |
| Timeout exhaustion | 0 | 3 bounded calls | PASS |
| Duplicate GitHub delivery | 1 | 1 | PASS |

## Payload and persistence audit

The tested BLOCK payload contains the pull request number, repository, one safe
model identifier, health, material finding count, an affected KPI when safely
available, and the GitHub review link. Injected raw SQL, finding evidence,
multiline values, arbitrary single-line SQL, environment values, exception
details, credentials, and webhook URLs are absent.

Persisted Slack state contains only the stable publication identity, state,
bounded attempt count, and allowlisted reason or error category. The webhook
credential is excluded from settings representations and logs.

## Validation totals

- Slack/GitHub focused suite: 49 passed.
- Full backend suite: 912 passed, 1 expected skip on Python 3.10 and current Python.
- CLI help: 19 of 19 commands passed.
- Compileall: passed.
- Locked production dependency audit: zero known vulnerabilities.
- `pip check`: no broken requirements.
- Bandit high-severity gate: zero findings.
- Verified secret gate: zero verified secrets.
- Tracked runtime-artifact audit: zero artifacts.
- Independent focused code review: zero remaining Critical or Important findings.

## Limitation

Slack incoming webhooks do not expose a remote publication identifier for
reconciliation. If a process stops after journal intent but before its local
completion record, the state becomes `indeterminate` and is not automatically
resent. This favors at-most-one optional notification over a possible duplicate;
GitHub remains the authoritative, recoverable publication channel.
