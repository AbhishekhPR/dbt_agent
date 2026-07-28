# Scan Verbose Output Design

## Goal

Add an optional `--verbose` flag to `relium scan` that presents a complete model-level audit trail without changing the existing compact report.

## Behavior

Without `--verbose`, the command continues to print the current compact report exactly as before. With `--verbose`, it prints that compact report followed by a deterministic `Scanned models:` section.

Every scanned dbt model receives a section containing its model name, resolved compiled SQL file path, and either `No risks found` or each AST finding as rule name, uppercase severity, and message. When `--changed-model` is provided, the verbose output also includes the existing downstream-model result; no downstream section is added when the option is absent.

## Architecture

`scan_dbt_project()` keeps producing structured data. Each AST model report gains the resolved SQL artifact path, allowing a new `format_verbose_scan_report()` formatter in `agent/dbt_project_scan.py` to render model-level output. The CLI owns only the boolean Click option and formatter selection.

## Testing

Fixture tests will verify that verbose output includes all scanned models, including models with no risks; includes the compiled artifact path and AST rule details for risky models; includes downstream models when requested; and leaves default output unchanged when `--verbose` is omitted.
