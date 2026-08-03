# SQL detector coverage

Relium reports only detectors that have an implemented, tested contract. A
detector finding is evidence, not a claim that arbitrary SQL semantic
correctness is decidable. Resulting-data comparison and warehouse metadata
remain the authority for risks static analysis cannot prove.

| ID | Finding | Owner | Supported dialects | Evidence / limitation |
| --- | --- | --- | --- | --- |
| B05_CROSS_JOIN | Explicit or implicit Cartesian product | sql-reliability | SQLite, PostgreSQL, Snowflake, BigQuery | Approved one-row parameter relations can suppress a finding; intent is not inferred universally. |
| B08_DUPLICATE_GENERATING_JOIN | Join may multiply declared grain | sql-reliability | SQLite, PostgreSQL, Snowflake, BigQuery | Requires grain, relationship, or uniqueness metadata; runtime cardinality must be checked. |
| B09_GRAIN_CHANGING_AGGREGATION | GROUP BY changes declared grain | sql-reliability | SQLite, PostgreSQL, Snowflake, BigQuery | Parseable structural comparison only; resulting-data comparison covers remaining risk. |
| B10_MISSING_DEDUPLICATION | Prior deduplication pattern removed | sql-reliability | SQLite, PostgreSQL, Snowflake, BigQuery | Recognizes equivalent window/QUALIFY/DISTINCT patterns, not one required syntax. |
| B11_UNSAFE_INCREMENTAL_WATERMARK | Required lookback/watermark weakened | sql-reliability | SQLite, PostgreSQL, Snowflake, BigQuery | Requires incremental and late-arrival contracts; source clock semantics remain unevaluated without metadata. |
| C06_LEFT_TO_INNER_JOIN | LEFT relationship changed to INNER | sql-reliability | SQLite, PostgreSQL, Snowflake, BigQuery | Structural evidence identifies the relation; optimizer-equivalent rewrites require result comparison. |

Every finding includes source attribution (`raw`, `compiled`, or another
declared source), severity, remediation, owner, and known limitations. Safe
equivalent rewrites are covered by negative controls. Unsupported dialects or
missing required metadata are never emitted as PASS; they remain
`UNSUPPORTED` or `NOT EVALUATED` under the evidence policy.
