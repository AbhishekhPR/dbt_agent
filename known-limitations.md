# Known limitations

- No dedicated GitHub App/repository credentials were available for live
  production-like lifecycle scenarios.
- No authorized production warehouse DSN was available; PostgreSQL contract and
  local SQLite tests do not constitute warehouse validation.
- Real Slack delivery is `BLOCKED BY CREDENTIALS`.
- Static detectors and semantic comparison do not decide arbitrary SQL
  semantic correctness; resulting-data comparison remains necessary.
- Column lineage is incomplete for unsupported SQL/dialects and is disclosed as
  such.
- Permanent hosted production readiness, multi-worker production load, and
  durable backup/restore remain unvalidated.
