# Security summary

Tenant keys scope lifecycle reads and writes. Warehouse adapters are read-only,
allowlisted, timeout/cost bounded, and return explicit missing states. Delivery
payloads redact secrets and raw SQL. Bandit, compileall, pip check, and secret
pattern scans pass for the implementation release. `pip-audit` in the global
validation environment reports pre-existing advisories in cryptography,
setuptools, and starlette; the task venv has no pip-audit module.
