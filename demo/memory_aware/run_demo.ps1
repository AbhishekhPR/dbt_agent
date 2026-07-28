$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $RepoRoot

$ReliumDir = "demo/memory_aware/.relium"
if (Test-Path $ReliumDir) {
  Remove-Item -LiteralPath $ReliumDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $ReliumDir | Out-Null

Write-Host ""
Write-Host "A. Initialize trusted baseline"
python -m agent.cli init-baseline `
  --dbt-manifest demo/history_aware/manifest_previous.json `
  --history-path demo/memory_aware/.relium/deployment_history.json `
  --deployment-id production-baseline

Write-Host ""
Write-Host "B. Review current deployment"
python -m agent.cli review-deployment `
  --dbt-manifest demo/history_aware/manifest_current.json `
  --changed-file models/marts/fct_revenue.sql `
  --history-path demo/memory_aware/.relium/deployment_history.json `
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json `
  --deployment-id refunds-risk-review `
  --format markdown | Tee-Object -FilePath "$ReliumDir/review_initial.md"

Write-Host ""
Write-Host "C. Backtest historical deployment"
python -m agent.cli backtest-deployment `
  --baseline-manifest demo/history_aware/manifest_previous.json `
  --dbt-manifest demo/history_aware/manifest_current.json `
  --changed-file models/marts/fct_revenue.sql `
  --deployment-id refunds-backtest `
  --format markdown | Tee-Object -FilePath "$ReliumDir/backtest.md"

Write-Host ""
Write-Host "D. Record deployment outcome"
python -m agent.cli record-outcome `
  --deployment-id refunds-risk-review `
  --decision BLOCK `
  --outcome fixed_before_merge `
  --notes "Relium block led to refund logic review before merge" `
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json

Write-Host ""
Write-Host "E. Show outcome summary"
python -m agent.cli outcome-summary `
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json

Write-Host ""
Write-Host "F. Review again with outcome memory"
$FinalReview = python -m agent.cli review-deployment `
  --dbt-manifest demo/history_aware/manifest_current.json `
  --changed-file models/marts/fct_revenue.sql `
  --history-path demo/memory_aware/.relium/deployment_history.json `
  --outcomes-path demo/memory_aware/.relium/deployment_outcomes.json `
  --deployment-id refunds-risk-review-repeat `
  --format markdown
$FinalReview | Tee-Object -FilePath "$ReliumDir/review_repeat.md"

$FinalText = $FinalReview -join "`n"
foreach ($RequiredText in @("Deployment Outcome Memory", "Previous block led to fix before merge", "deployment_outcomes")) {
  if ($FinalText -notlike "*$RequiredText*") {
    throw "Final review did not include required text: $RequiredText"
  }
}

Write-Host ""
Write-Host "Memory-aware demo complete. Outputs written to $ReliumDir"
