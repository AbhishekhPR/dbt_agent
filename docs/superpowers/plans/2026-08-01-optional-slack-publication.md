# Optional Slack Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a disabled-by-default, durable, SQL-free Slack publication sink that runs only after successful GitHub publication.

**Architecture:** A focused `SlackPublicationSink` owns message construction, redaction, transport, and bounded retry. `PullRequestReviewRunner` invokes it after comment/check completion and the repository publication journal persists the stable Slack state. Server settings build and inject the sink without exposing the webhook credential.

**Tech Stack:** Python 3.10, stdlib `urllib`, filesystem publication journal, `unittest`, local `http.server` fake receiver.

---

### Task 1: Optional Slack settings

**Files:**
- Modify: `agent/github_app/settings.py`
- Modify: `.env.example`
- Test: `test_github_app_settings.py`

- [ ] **Step 1: Write failing settings tests**

Add assertions that defaults produce `slack_webhook_url=None`,
`slack_notify_warn=False`, `slack_max_retries=2`, and
`slack_retry_base_seconds=1.0`. Add explicit-value tests and invalid boolean,
retry, capped-delay, and Slack HTTPS endpoint cases. Assert the webhook value
is absent from `repr()`.

- [ ] **Step 2: Run the settings tests and confirm RED**

Run: `python -m unittest test_github_app_settings -v`

Expected: failures for missing Slack fields or parser behavior.

- [ ] **Step 3: Implement typed optional settings**

Add hidden dataclass fields and strict parsing:

```python
slack_webhook_url: str | None = field(default=None, repr=False)
slack_notify_warn: bool = False
slack_max_retries: int = 2
slack_retry_base_seconds: float = 1.0
```

Only `true` and `false` are accepted for the boolean. Whitespace-only webhook
configuration normalizes to `None`; configured URLs must be Slack or Slack Gov
HTTPS incoming-webhook endpoints. Bound retries to five and retry-base seconds
to ten. Document the four environment names with empty or safe default values
in `.env.example`.

- [ ] **Step 4: Run settings tests and confirm GREEN**

Run: `python -m unittest test_github_app_settings -v`

Expected: all settings tests pass.

### Task 2: Slack message and bounded transport

**Files:**
- Create: `agent/github_app/slack.py`
- Create: `test_github_app_slack.py`

- [ ] **Step 1: Write failing payload and fake-receiver tests**

Cover BLOCK message structure, optional KPI, safe fallbacks, single-line
length-bounded text, Slack escaping, and absence of `raw_code`, `compiled_code`,
finding evidence, SQL fragments, environment values, and webhook credentials.
Use a local `ThreadingHTTPServer` receiver to assert one JSON request.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m unittest test_github_app_slack -v`

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement message construction and transport**

Create:

```python
class SlackPublicationSink:
    def publish(self, *, publication_id, repository, pull_number, result, pull_url):
        decision = str(result.get("decision", "")).upper()
        if decision == "ALLOW" or (decision == "WARN" and not self.notify_warn):
            return {"state": "skipped", "publication_id": publication_id}
        payload = build_review_payload(
            repository=repository,
            pull_number=pull_number,
            result=result,
            pull_url=pull_url,
        )
        return self._send_bounded(publication_id, payload)
```

Return a JSON-serializable safe status dictionary. Retry only 429, 5xx,
timeout, and connection errors, with no more than `max_retries + 1` attempts.
Use only safe error categories and never exception text.

- [ ] **Step 4: Add bounded retry tests**

Fake receiver sequences must prove `500 -> 200` performs two attempts,
permanent 400 performs one, timeout/connection exhaustion is bounded, and no
request occurs for ALLOW or unconfigured WARN.

- [ ] **Step 5: Run Slack transport tests and confirm GREEN**

Run: `python -m unittest test_github_app_slack -v`

Expected: all Slack sink tests pass without external network access.

### Task 3: Durable runner integration

**Files:**
- Modify: `agent/github_app/storage.py`
- Modify: `agent/github_app/runner.py`
- Modify: `agent/github_app/server.py`
- Test: `test_github_app_slack.py`
- Test: `test_github_app_runner.py`
- Test: `test_github_app_server.py`

- [ ] **Step 1: Write failing publication-journal tests**

Prove `slack` is an accepted publication step, persists after storage reload,
and unknown step names still fail. Prove two duplicate runner deliveries cause
at most one Slack receiver request.

- [ ] **Step 2: Write failing ordering and containment tests**

Assert the Slack publisher observes completed comment/check journal entries,
ALLOW skips, BLOCK publishes, configured WARN publishes, and a Slack exception
leaves the GitHub comment and check complete while the runner returns a safe
Slack failure state.

- [ ] **Step 3: Run integration tests and confirm RED**

Run: `python -m unittest test_github_app_slack test_github_app_runner test_github_app_server -v`

Expected: failures for unsupported journal step and missing publisher wiring.

- [ ] **Step 4: Implement journal and runner integration**

Permit the `slack` journal step. Inject an optional publisher into the runner.
After check completion, reconcile prior Slack state, persist `started`, call the
sink, and persist a terminal safe state. A pre-existing `started` state becomes
`indeterminate` without resending. Catch every Slack exception locally.

- [ ] **Step 5: Wire server construction**

Build `SlackPublicationSink` only when a webhook is configured, passing WARN
and retry settings. Inject it into `PullRequestReviewRunner`; otherwise inject
no publisher so existing behavior is unchanged.

- [ ] **Step 6: Run integration tests and confirm GREEN**

Run: `python -m unittest test_github_app_slack test_github_app_runner test_github_app_server -v`

Expected: all tests pass and fake receiver request counts are exact.

### Task 4: Evidence, regressions, and delivery

**Files:**
- Create: `slack-evidence.md`
- Modify: `docs/superpowers/specs/2026-08-01-optional-slack-publication-design.md` only if implementation evidence reveals a contradiction

- [ ] **Step 1: Run focused and full validation**

Run compileall, Slack/GitHub focused tests, all 19 CLI help commands, the full
backend suite, `pip check`, strict hash-locked dependency audit, Bandit high,
secret detection, `git diff --check`, and tracked runtime-artifact audit.

Expected: all tests pass with only documented skips; zero production
advisories, high-severity findings, verified secrets, or runtime artifacts.

- [ ] **Step 2: Record redacted Slack evidence**

Write fake-receiver counts, publication state, retry count, payload shape,
redaction results, and real-delivery status. If no dedicated test credential is
available, record exactly `BLOCKED BY CREDENTIALS`; do not send a real message.

- [ ] **Step 3: Commit implementation**

Stage exact Slack source, tests, settings, environment documentation, plan, and
evidence. Run `git diff --cached --check` and content audits, then commit:

```text
feat: add optional durable Slack publication sink
```

- [ ] **Step 4: Push, open focused PR, and merge only after CI**

Push `relium-optional-slack-sink`, open one PR to backend `main`, validate the
paginated file list and logical commits, wait for `test`, `security`, and
`pr-review`, then merge with a merge commit while preserving the branch and
worktree.
