# Relium

## Setup

Install the Python dependencies before running the CLI or tests:

```powershell
python -m pip install -r requirements.txt
```

Relium requires real dependency installation.

- If `click` is missing, the CLI will fail instead of using a local shim.
- If `sqlglot` is missing, AST analysis will fail instead of silently downgrading to regex logic.

## Scan a compiled dbt project

Relium scans dbt's local compilation artifacts; it does not connect to a warehouse.

After compiling a local checkout of [dbt Labs Jaffle Shop](https://github.com/dbt-labs/jaffle-shop):

```powershell
dbt compile --project-dir path/to/jaffle-shop
python -m agent.cli scan --project path/to/jaffle-shop --changed-model customers
```

When installed as a console command, use:

```powershell
relium scan --project path/to/jaffle-shop --changed-model customers
```

Omit `--changed-model` to perform the complete AST scan without a blast-radius result.

## GitHub PR review artifact

The `Relium PR Review` GitHub Actions workflow runs on pull requests and uploads
the local Relium PR review markdown as an artifact named `relium-pr-review`.
It does not post GitHub comments or block pull requests yet.

GitHub PR commenting and decision-based blocking will be added later.
