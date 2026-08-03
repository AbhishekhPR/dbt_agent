# Metadata monitoring boundary

The `WarehouseAdapter` boundary exposes schema, counts, distinct keys,
duplicates, null rates, freshness/watermarks, types, distributions, and KPI
observations without coupling detectors to a provider. The local SQLite
adapter is deterministic for E2E. The PostgreSQL adapter is read-only,
allowlisted, timeout-bounded, and cost-bounded; real warehouse validation is
`BLOCKED BY CREDENTIALS` until explicitly authorized credentials exist.

Missing or over-budget observations are `NOT EVALUATED`, never synthetic
healthy data. Customer-facing evidence contains redacted query metadata only.
