#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

RELIUM_DIR="demo/memory_aware/.relium"
rm -rf "$RELIUM_DIR"
mkdir -p "$RELIUM_DIR"

printf '\nA. Initialize trusted baseline\n'
python -m agent.cli init-baseline \
  --dbt-manifest demo/history_aware/manifest_previous.json \
  --history-path demo/memory_aware/.relium/deployment_history.json \
  --deployment-id production-baseline

printf '\nB. Review current deployment\n'
python -m agent.cli review-deployment \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-file models/marts/fct_revenue.sql \
  --history-path demo/memory_aware/.relium/deployment_history.json \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json \
  --deployment-id refunds-risk-review \
  --format markdown | tee "$RELIUM_DIR/review_initial.md"

printf '\nC. Backtest historical deployment\n'
python -m agent.cli backtest-deployment \
  --baseline-manifest demo/history_aware/manifest_previous.json \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-file models/marts/fct_revenue.sql \
  --deployment-id refunds-backtest \
  --format markdown | tee "$RELIUM_DIR/backtest.md"

printf '\nD. Record deployment outcome\n'
python -m agent.cli record-outcome \
  --deployment-id refunds-risk-review \
  --decision BLOCK \
  --outcome fixed_before_merge \
  --notes "Relium block led to refund logic review before merge" \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json

printf '\nE. Show outcome summary\n'
python -m agent.cli outcome-summary \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json

printf '\nF. Review again with outcome memory\n'
final_review=$(python -m agent.cli review-deployment \
  --dbt-manifest demo/history_aware/manifest_current.json \
  --changed-file models/marts/fct_revenue.sql \
  --history-path demo/memory_aware/.relium/deployment_history.json \
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json \
  --deployment-id refunds-risk-review-repeat \
  --format markdown)
printf '%s\n' "$final_review" | tee "$RELIUM_DIR/review_repeat.md"

printf '%s\n' "$final_review" | grep -q "Deployment Outcome Memory"
printf '%s\n' "$final_review" | grep -q "Previous block led to fix before merge"
printf '%s\n' "$final_review" | grep -q "deployment_outcomes"

printf '\nMemory-aware demo complete. Outputs written to %s\n' "$RELIUM_DIR"
