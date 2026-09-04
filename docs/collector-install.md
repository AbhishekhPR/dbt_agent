# Relium Collector — installation guide (PostgreSQL, v0)

The collector runs inside your environment. Relium describes what production
evidence a pull request needs; the collector measures only that, and sends back
metadata — never rows, never queries, never credentials.

It is a single command that runs, does one unit of work, and exits.

---

## 1. Prerequisites

- Python 3.10 or newer on the host that will run the collector
- Network egress from that host to your Relium API URL (HTTPS)
- Network access from that host to your PostgreSQL warehouse
- A Relium collector token (§4 — issued once in the Relium dashboard)

The collector needs no inbound ports, no daemon, and no privileged access.

**Supported warehouse in v0: PostgreSQL.** Snowflake, BigQuery, Redshift and
Databricks are not supported yet — the collector will not pretend otherwise.

---

## 2. Create a read-only PostgreSQL role

Run as a warehouse administrator. **Do not give the collector a superuser.**

```sql
CREATE ROLE relium_collector LOGIN PASSWORD '<generate-a-strong-password>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 5;
```

---

## 3. Minimum grants

Grant only the schemas your dbt project actually reads in production.

```sql
GRANT CONNECT ON DATABASE analytics TO relium_collector;

GRANT USAGE ON SCHEMA raw TO relium_collector;
GRANT SELECT ON ALL TABLES IN SCHEMA raw TO relium_collector;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw
  GRANT SELECT ON TABLES TO relium_collector;
```

Repeat the three schema lines for each schema the collector must read.

`USAGE` and `SELECT` are the whole requirement. The collector opens every
session with `default_transaction_read_only`, so it cannot write even if it
tried.

> **Why `ALTER DEFAULT PRIVILEGES` matters.** Without it, tables created after
> today are invisible to the collector. An invisible table is indistinguishable
> from a dropped one to most tools — Relium's collector detects this case
> specifically and fails with a clear message rather than reporting the table as
> missing from production, which would block your pull requests.

---

## 4. Issue the Relium collector token

Open **Integrations → Warehouse evidence** in Relium, choose the production
environment, and select **Generate collector token**. The token is shown
**once** and is not recoverable — Relium stores only a SHA-256 hash. Put it
directly into your secret manager; returning to or refreshing the setup panel
will show only the non-secret token id.

---

## 5. Install the collector

Check the artifact before installing it. You are about to run this code
against your warehouse, and the checksum is how you confirm the file you
received is the file we built.

```bash
sha256sum --check SHA256SUMS
```

Every supported collector bundle contains the wheel and the `SHA256SUMS` file
generated from that exact wheel in the same packaging job. If verification
fails, stop and contact your Relium contact. Do not install it. A checksum
copied into documentation is intentionally not used because it can drift from
the artifact it describes.

```bash
python -m venv /opt/relium/venv
/opt/relium/venv/bin/pip install relium-0.1.0-py3-none-any.whl
```

Verify:

```bash
/opt/relium/venv/bin/relium collect --help
```

The wheel depends only on `click` and `psycopg`. If `pip` pulls anything
beyond those and their own dependencies, you have the wrong artifact.

---

## 6. Configure environment variables

| Variable | Required | Meaning |
|---|---|---|
| `RELIUM_API_URL` | yes | Your Relium API base URL. Must be `https://` unless loopback. |
| `RELIUM_API_TOKEN` | yes | The token from §4. |
| `RELIUM_WAREHOUSE_DSN` | yes | Read-only PostgreSQL DSN. |
| `RELIUM_ENVIRONMENT` | no | Defaults to `production`. Must match the token's scope. |
| `RELIUM_COLLECTOR_ID` | no | Identifies this collector host. Defaults to `relium-collector`. |
| `RELIUM_STATEMENT_TIMEOUT_MS` | no | Warehouse statement timeout. Default `30000`. |
| `RELIUM_API_TIMEOUT_SECONDS` | no | API call timeout. Default `30`. |
| `RELIUM_API_CA_BUNDLE` | no | PEM bundle for a private or inspecting CA. |

Example:

```bash
export RELIUM_API_URL="https://api.relium.example.com"
export RELIUM_API_TOKEN="rlm_...."
export RELIUM_WAREHOUSE_DSN="postgresql://relium_collector:PASSWORD@warehouse.internal:5432/analytics?sslmode=require"
export RELIUM_ENVIRONMENT="production"
export RELIUM_COLLECTOR_ID="acme-prod-collector"
```

Store these in a secrets manager or a `0600` environment file owned by the
service account. **TLS verification is always on and cannot be disabled.** If
you terminate TLS with a private CA, point `RELIUM_API_CA_BUNDLE` at its
certificate. Outbound proxies are read from the standard `HTTPS_PROXY` /
`HTTP_PROXY` / `NO_PROXY` variables.

Use `sslmode=require` (or stricter) in the warehouse DSN.

---

## 7. Verify connectivity

```bash
relium collect --test
```

This registers the collector, opens the same server-enforced read-only
PostgreSQL session used for collection, runs `SELECT 1`, records the verified
heartbeat in Relium, and exits. A normal idle `relium collect` proves API and
token reachability but deliberately does not touch the warehouse, so it is not
a connectivity test.

---

## 8. Run it

```bash
relium collect
```

Schedule it with cron, or with whatever scheduler you already run:

```cron
* * * * * /opt/relium/venv/bin/relium collect >> /var/log/relium-collector.log 2>&1
```

**How often:** every minute is reasonable and is what we suggest. A run with no
pending request costs one authenticated API call and touches no warehouse.
Every collection request is actionable for **30 minutes**. The separate
production-observation freshness policy is 60 minutes for standard models and
15 minutes for critical models; those freshness windows do not extend or
shorten the request deadline.

**Safe to invoke repeatedly.** Concurrent or repeated runs cannot corrupt
anything: each run takes one request, and resubmitting identical evidence is
idempotent server-side. There is no lock file and no daemon.

Each run processes **one** request. If several are pending, consecutive runs
drain them.

---

## 9. Expected successful output

```
relium collect: snapshot accepted
  request       req-gh-a1b2c3d4-1
  review        gh-a1b2c3d4 (attempt 1)
  snapshot      snap-9f8e7d6c (HTTP 202)
  relations     1 (2 columns)
  signals       column_exists, data_type, freshness, is_nullable, null_rate,
                relation_exists, row_count, schema_fingerprint
```

Exit `0`. Relium recomputes the review and updates the GitHub check.

`--json` emits the same outcome as a machine-readable object.

---

## 10. Common safe failure states

| Output | Exit | Meaning and fix |
|---|---|---|
| `missing required configuration: …` | 2 | An environment variable is unset. |
| `RELIUM_API_URL must use https…` | 2 | Plaintext to a remote host is refused. |
| `could not reach the Relium API (URLError)` | 1 | Egress, DNS or proxy. Check `HTTPS_PROXY`. |
| `GET /api/collection-requests returned HTTP 401` | 1 | Token invalid, revoked or expired. |
| `could not connect to the warehouse (OperationalError)` | 1 | DSN, network or credentials. |
| `relation X exists but these credentials cannot read it; grant SELECT…` | 1 | §3 grants. **Not** a schema problem. |
| `collection request … expired at …` | 1 | The collector ran too infrequently. |
| `unknown signal(s) …` | 1 | Collector older than the Relium service. Upgrade it. |
| `no pending collection request` | 0 | Normal idle state. |

Every failure exits non-zero and explains itself. No output ever contains your
token, your DSN, your password, or a row from your warehouse.

---

## 11. Revoke credentials / uninstall

Revoke the Relium token (operator side) — takes effect immediately:

```bash
relium list-collector-tokens --organization acme
relium revoke-collector-token --token-id <token_id>
```

Remove warehouse access:

```sql
REVOKE ALL ON ALL TABLES IN SCHEMA raw FROM relium_collector;
REVOKE ALL ON SCHEMA raw FROM relium_collector;
DROP ROLE relium_collector;
```

Remove the collector:

```bash
crontab -l | grep -v 'relium collect' | crontab -
rm -rf /opt/relium/venv
```

---

## What the collector reads, and what it never sends

**Reads:** `information_schema` and `pg_catalog` for the requested relations,
plus one bounded aggregate query per relation (`count(*)`, null counts, distinct
counts) over only the requested columns.

**Sends:** column names, data types, nullability, row counts, null rates,
distinct counts, and a schema fingerprint.

**Never sends:** rows, cell values, SQL, credentials, or any column Relium did
not ask for. Relium never sends SQL either — every query is generated on your
host from a fixed vocabulary of signal names.
