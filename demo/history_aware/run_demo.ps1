python -m agent.cli review-deployment `
  --dbt-manifest demo/history_aware/manifest_previous.json `
  --changed-model fct_revenue `
  --history-path demo/history_aware/.relium/deployment_history.json `
  --deployment-id previous_deploy `
  --auto-record `
  --allow-blocked-recording `
  --format markdown

python -m agent.cli review-deployment `
  --dbt-manifest demo/history_aware/manifest_current.json `
  --changed-model fct_revenue `
  --history-path demo/history_aware/.relium/deployment_history.json `
  --deployment-id current_deploy `
  --format markdown
