"""The fixture warehouse and its deterministic phase producer.

This is the ONLY thing in the E2E permitted to change warehouse state, and it
is permitted to change nothing else. It connects as ``fixture_producer``, a
role with no privilege on the Relium lifecycle database at all, so "the
producer cannot fabricate a review, a decision or an RCA" is enforced by
PostgreSQL rather than promised by the harness.

Phases are deterministic, not random. Each one is designed to cross exactly
one already-implemented decision threshold:

  0  baseline      healthy production                      -> ALLOW
  1  growth        more valid rows, nothing degrades       -> ALLOW
  2  null decay    customer_id NULL rate crosses 0.20      -> WARN
  3  column drop   customer_id removed from production     -> BLOCK
  4  recovery      column restored and fully backfilled    -> ALLOW

Phase 2 crosses ``decision.HIGH_NULL_RATE``. Phase 3 triggers
``column.missing_in_production``. Neither threshold was invented here and
neither was altered to make the demonstration work.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402

SCHEMA = "analytics"
ORDERS = f"{SCHEMA}.orders"
REVENUE = f"{SCHEMA}.rpt_revenue"

# Phase 2 must land ABOVE the engine's threshold without being so extreme that
# it would still be a finding at any sane threshold. 0.20 is the policy; the
# producer aims for roughly a third, which is unambiguous either way.
from agent.metadata_evidence.decision import HIGH_NULL_RATE  # noqa: E402

BASELINE_ROWS = 500
GROWTH_STEP = 50


def _connect(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row)


# ----------------------------------------------------------------- seeding

def seed(dsn) -> dict:
    """Create the warehouse and load healthy baseline production data."""
    with _connect(dsn) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(f"DROP TABLE IF EXISTS {REVENUE}")
        conn.execute(f"DROP TABLE IF EXISTS {ORDERS}")
        conn.execute(f"""
            CREATE TABLE {ORDERS} (
                order_id    BIGSERIAL PRIMARY KEY,
                customer_id BIGINT,
                amount      NUMERIC(12,2) NOT NULL,
                status      TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        # Downstream blast-radius relation. Present in production, so the
        # review has something real to reason about downstream.
        conn.execute(f"""
            CREATE TABLE {REVENUE} (
                revenue_date DATE PRIMARY KEY,
                gross_amount NUMERIC(14,2) NOT NULL
            )""")
        conn.execute(f"""
            INSERT INTO {ORDERS} (customer_id, amount, status, created_at)
            SELECT (g %% 120) + 1,
                   round((15 + (g %% 400) * 0.37)::numeric, 2),
                   CASE WHEN g %% 17 = 0 THEN 'refunded' ELSE 'completed' END,
                   now() - ((%s - g) || ' minutes')::interval
            FROM generate_series(1, %s) AS g
        """, (BASELINE_ROWS, BASELINE_ROWS))
        conn.execute(f"""
            INSERT INTO {REVENUE} (revenue_date, gross_amount)
            SELECT (current_date - g), round((5000 + g * 13.5)::numeric, 2)
            FROM generate_series(0, 29) AS g
        """)
    return stats(dsn)


# ------------------------------------------------------------- the phases

def phase_1_growth(dsn, rows=GROWTH_STEP) -> dict:
    """Append valid rows. Nothing degrades; the review must stay healthy."""
    with _connect(dsn) as conn:
        conn.execute(f"""
            INSERT INTO {ORDERS} (customer_id, amount, status, created_at)
            SELECT (g %% 120) + 1,
                   round((15 + (g %% 400) * 0.37)::numeric, 2),
                   'completed', now()
            FROM generate_series(1, %s) AS g
        """, (rows,))
    return stats(dsn)


def phase_2_null_decay(dsn, target_rate=None) -> dict:
    """Insert rows whose critical column is NULL until the rate crosses policy.

    The count is computed from the live table rather than guessed, so the phase
    is deterministic whatever Phase 1 left behind.
    """
    target_rate = target_rate if target_rate is not None else HIGH_NULL_RATE + 0.13
    with _connect(dsn) as conn:
        current = conn.execute(f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE customer_id IS NULL) AS nulls
            FROM {ORDERS}""").fetchone()
        total, nulls = current["total"], current["nulls"]
        # nulls + n >= target * (total + n)  ->  n >= (target*total - nulls)/(1-target)
        needed = int((target_rate * total - nulls) / (1 - target_rate)) + 1
        needed = max(needed, 1)
        conn.execute(f"""
            INSERT INTO {ORDERS} (customer_id, amount, status, created_at)
            SELECT NULL,
                   round((15 + (g %% 400) * 0.37)::numeric, 2),
                   'completed', now()
            FROM generate_series(1, %s) AS g
        """, (needed,))
    result = stats(dsn)
    result["rows_inserted"] = needed
    result["threshold"] = HIGH_NULL_RATE
    return result


def phase_3_drop_column(dsn) -> dict:
    """Remove the production column the head code depends on.

    This is the already-supported BLOCK condition: a required production
    column becoming unavailable in the supplied evidence. No new rule.
    """
    with _connect(dsn) as conn:
        conn.execute(f"ALTER TABLE {ORDERS} DROP COLUMN IF EXISTS customer_id")
    result = stats(dsn)
    result["dropped_column"] = "customer_id"
    return result


def phase_4_recover(dsn) -> dict:
    """Restore the column and backfill it completely."""
    with _connect(dsn) as conn:
        conn.execute(f"ALTER TABLE {ORDERS} ADD COLUMN IF NOT EXISTS customer_id BIGINT")
        # No bound parameters here, so psycopg does no placeholder expansion
        # and the modulo operator must be written literally.
        conn.execute(f"UPDATE {ORDERS} SET customer_id = (order_id % 120) + 1 "
                     "WHERE customer_id IS NULL")
    return stats(dsn)


# ------------------------------------------------------------ observation

def stats(dsn) -> dict:
    """What the warehouse currently looks like. Aggregates only, never rows."""
    with _connect(dsn) as conn:
        columns = [r["column_name"] for r in conn.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name='orders'
            ORDER BY ordinal_position""", (SCHEMA,)).fetchall()]
        if "customer_id" in columns:
            row = conn.execute(f"""
                SELECT count(*) AS row_count,
                       count(*) FILTER (WHERE customer_id IS NULL) AS null_count,
                       count(DISTINCT customer_id) AS distinct_customers
                FROM {ORDERS}""").fetchone()
            null_rate = (row["null_count"] / row["row_count"]) if row["row_count"] else 0.0
        else:
            row = conn.execute(
                f"SELECT count(*) AS row_count FROM {ORDERS}").fetchone()
            row = {"row_count": row["row_count"], "null_count": None,
                   "distinct_customers": None}
            null_rate = None

    return {
        "relation": ORDERS,
        "columns": columns,
        "customer_id_present": "customer_id" in columns,
        "row_count": row["row_count"],
        "customer_id_null_count": row["null_count"],
        "customer_id_null_rate": (round(null_rate, 4)
                                  if null_rate is not None else None),
        "distinct_customers": row["distinct_customers"],
    }


PHASES = {
    "seed": seed,
    "growth": phase_1_growth,
    "null_decay": phase_2_null_decay,
    "drop_column": phase_3_drop_column,
    "recover": phase_4_recover,
}


def main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Fixture warehouse producer")
    parser.add_argument("phase", choices=sorted(PHASES) + ["stats"])
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args(argv)

    fn = stats if args.phase == "stats" else PHASES[args.phase]
    print(json.dumps(fn(args.dsn), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
