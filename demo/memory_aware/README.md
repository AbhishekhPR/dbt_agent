# Memory-Aware Relium Demo

This demo shows Relium's full product loop:

1. Relium first builds a trusted baseline from the previous dbt manifest.
2. It reviews the current manifest and detects a semantic revenue change: refund lineage now flows into `fct_revenue`.
3. A backtest proves Relium would have caught the issue historically.
4. Outcome recording turns the review into product memory.
5. The next review uses that outcome memory and shows it in the deployment decision.

The demo reuses the manifests in `demo/history_aware` and writes its own generated state to `demo/memory_aware/.relium`.

## Run

From the repository root:

```powershell
.\demo\memory_aware\run_demo.ps1
```

or:

```bash
sh demo/memory_aware/run_demo.sh
```

Both scripts run the same flow:

```bash
python -m agent.cli init-baseline \
  --dbt-manifest demo/history_aware/manifest_previous.json \
  --history-path demo/memory_aware/.relium/deployment_history.json \
  --deployment-id production-baseline
```

```bash
python -m agent.cli review-deployment \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-file models/marts/fct_revenue.sql \
  --history-path demo/memory_aware/.relium/deployment_history.json \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json \
  --deployment-id refunds-risk-review \
  --format markdown
```

```bash
python -m agent.cli backtest-deployment \
  --baseline-manifest demo/history_aware/manifest_previous.json \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-file models/marts/fct_revenue.sql \
  --deployment-id refunds-backtest \
  --format markdown
```

```bash
python -m agent.cli record-outcome \
  --deployment-id refunds-risk-review \
  --decision BLOCK \
  --outcome fixed_before_merge \
  --notes "Relium block led to refund logic review before merge" \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json
```

```bash
python -m agent.cli outcome-summary \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json
```

```bash
python -m agent.cli review-deployment \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-file models/marts/fct_revenue.sql \
  --history-path demo/memory_aware/.relium/deployment_history.json \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json \
  --deployment-id refunds-risk-review-repeat \
  --format markdown
```

## Expected Result

The final review should include:

- `Deployment Outcome Memory`
- `Previous block led to fix before merge`
- `deployment_outcomes` in `Signals Considered`

That is the memory loop: a deployment review becomes an outcome, and the next deployment review can use that historical outcome as context.
