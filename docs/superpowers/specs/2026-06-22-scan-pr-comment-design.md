# Scan PR Comment Design

## Goal

Run Relium scan for pull requests and maintain one persistent Markdown report comment.

## CLI

`scan` gains `--format text|markdown` and optional `--output PATH`. Text remains the default. Markdown begins with a Relium heading and contains the compact scan fields in a two-column table. Writing to `--output` saves exactly the selected rendered report while preserving normal stdout behavior.

## PR comment

The Action prefixes the generated Markdown with `<!-- relium-scan-report -->`. A scan-specific wrapper around the existing GitHub comment helper searches PR comments for that marker, updates a match, and creates one only when no match exists. This prevents duplicate comments on reruns.

## Workflow

`.github/workflows/relium.yml` runs on `pull_request`, installs Python and dbt dependencies, runs `dbt deps` then `dbt compile`, invokes scan with `--format markdown --output relium_report.md`, and calls the marker-based comment wrapper using `GITHUB_TOKEN` with pull-request write permission. It adds no warehouse or Slack integration.

## Tests

Tests cover Markdown rendering, output-file persistence, unchanged text default output, and marker-based update/create behavior through the existing GitHub-comment client seams.
