# Relium

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
