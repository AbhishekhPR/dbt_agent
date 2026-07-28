# History-Aware Relium Demo

This fixture shows Relium detecting a historical semantic change in a dbt manifest.

The previous manifest defines `Revenue` as completed customer payments from orders and payments. The current manifest adds refund lineage through `stg_refunds`, so the current review should surface:

- `Previous Snapshot Loaded: YES`
- `Historical Semantic Change`
- `Revenue gained upstream dependency refunds`
- `Deployment Decision`
- `Pipeline Health`

## Run

From the repository root:

```bash
python -m agent.cli review-deployment \
  --dbt-manifest demo/history_aware/manifest_previous.json \
  --changed-model fct_revenue \
  --history-path demo/history_aware/fixtures/deployment_history.json \
  --deployment-id previous_deploy \
  --auto-record \
  --format markdown
```

```bash
python -m agent.cli review-deployment \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-model fct_revenue \
  --history-path demo/history_aware/fixtures/deployment_history.json \
  --deployment-id current_deploy \
  --format markdown
```

A prebuilt history fixture is included at `fixtures/deployment_history.json`, so the second command can be run directly to demonstrate the historical comparison. If your local review blocks the first deployment and you want to overwrite the fixture manually, add `--allow-blocked-recording` to the first command.
