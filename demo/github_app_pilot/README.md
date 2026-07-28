# GitHub App pilot fixture

This fixture supplies small, synthetic repository content for the first Relium
GitHub App pilot. It contains no credentials, warehouse rows, deployment history,
or executable setup code.

## Files

- `relium.yml` is the minimal pilot configuration. Copy it to the root of the
  dedicated test repository.
- `previous_manifest.json` models the starting state.
- `current_manifest.json` models both example changes. Copy it to
  `target/manifest.json` on the test branch when exercising the automated review.
- `models/safe_customer_dimension.sql` adds a descriptive customer-name column.
  The previous manifest shows the earlier customer-ID-only definition.
- `models/risky_revenue_refunds.sql` intentionally stops subtracting refunds from
  net revenue. The previous manifest shows the refund-aware definition.

## Mapping to live tests

For the safe scenario, begin with the previous manifest and model definition,
then open a pull request that adds `customer_name` and uses the current manifest's
customer-dimension node. For the risky scenario, begin with the refund-aware SQL
from `previous_manifest.json`, then change the model to
`models/risky_revenue_refunds.sql` and commit `current_manifest.json` as
`target/manifest.json`.

Use only a dedicated test repository. The SQL is static input for Relium's parser;
do not execute it against a database. Exact decisions depend on the full manifest
context, so verify the rendered evidence without changing detector scores or
thresholds.

`enforcement_mode: shadow` keeps BLOCK advisory, while
`enforcement_mode: enforce` makes BLOCK fail the GitHub check and return a nonzero
CI exit code. ALLOW and WARN remain non-failing. The legacy `mode` field is
deprecated and does not control GitHub check enforcement.
