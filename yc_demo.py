"""Relium YC terminal demo.

Run:
    python yc_demo.py

This script is deterministic, uses no external services, and avoids real
customer data. It demonstrates how Relium can catch silent SQL/dbt pipeline
risks before bad metrics reach dashboards.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import textwrap


MODEL_NAME = "customer_metrics.sql"
SAMPLE_SQL_PATH = Path(__file__).with_name("sample_customer_metrics.sql")

FALLBACK_SQL = """
with customers as (
    select
        customer_id,
        account_tier,
        created_at
    from raw.customers
),

orders as (
    select
        customer_id,
        order_id,
        order_status,
        total_amount,
        updated_at
    from raw.orders
),

customer_metrics as (
    select
        c.customer_id,
        c.account_tier,
        count(o.order_id) as orders_30d,
        sum(o.total_amount) as revenue_30d
    from customers c
    left join orders o
        on c.customer_id = o.customer_id
    where o.order_status = 'completed'
      and o.updated_at >= current_date - interval '30 day'
    group by 1, 2
)

select * from customer_metrics;
""".strip()


@dataclass(frozen=True)
class MetadataSignal:
    """A small synthetic metadata signal for the screen-recording demo."""

    label: str
    before: str
    after: str
    status: str


def load_demo_sql() -> str:
    """Load the sample SQL file when present, otherwise use the fallback SQL."""

    if SAMPLE_SQL_PATH.exists():
        return SAMPLE_SQL_PATH.read_text(encoding="utf-8").strip()
    return FALLBACK_SQL


def print_section(title: str) -> None:
    """Print a consistent section heading for a polished terminal flow."""

    print()
    print(f"== {title} ==")


def print_wrapped(text: str, indent: int = 0) -> None:
    """Wrap prose so the demo is readable in a narrow recording window."""

    prefix = " " * indent
    print(textwrap.fill(text, width=88, initial_indent=prefix, subsequent_indent=prefix))


def detect_left_join_filter(sql: str) -> bool:
    """Detect a LEFT JOIN followed by a WHERE filter on the joined table alias."""

    normalized = re.sub(r"\s+", " ", sql.lower())
    left_join_aliases = re.findall(r"left\s+join\s+[\w.]+\s+([a-z_][\w]*)\s+on", normalized)
    if not left_join_aliases:
        return False

    where_match = re.search(r"\bwhere\b(.+?)(\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)", normalized)
    if not where_match:
        return False

    where_clause = where_match.group(1)
    return any(re.search(rf"\b{re.escape(alias)}\.", where_clause) for alias in left_join_aliases)


def analyze_sql(sql: str) -> list[str]:
    """Simulate Relium's rule-based SQL and metadata analysis."""

    findings: list[str] = []

    if detect_left_join_filter(sql):
        findings.append("Risky LEFT JOIN filter issue")

    # These are deterministic demo signals that mimic metadata Relium would see.
    findings.extend(
        [
            "Possible row-count drop",
            "Null-count/null-rate anomaly",
            "Schema change risk",
        ]
    )
    return findings


def render_sql(sql: str) -> None:
    """Print the model SQL with line numbers for demo clarity."""

    for line_number, line in enumerate(sql.splitlines(), start=1):
        print(f"{line_number:>2} | {line}")


def render_findings(findings: list[str]) -> None:
    """Print the detected risks in a concise, investor-friendly format."""

    for index, finding in enumerate(findings, start=1):
        print(f"[{index}] {finding}")


def render_metadata_signals() -> None:
    """Print synthetic metadata changes without showing customer-level data."""

    signals = [
        MetadataSignal("model row count", "128,420", "94,870", "down 26.1%"),
        MetadataSignal("customer_id null rate", "0.2%", "6.8%", "up 34x"),
        MetadataSignal("orders.order_status", "present", "present", "used in WHERE"),
        MetadataSignal("orders.fulfillment_state", "present", "removed", "schema drift"),
    ]

    print(f"{'Signal':<28} {'Previous':<14} {'Current':<14} Status")
    print("-" * 74)
    for signal in signals:
        print(f"{signal.label:<28} {signal.before:<14} {signal.after:<14} {signal.status}")


def main() -> None:
    """Run the YC demo from title through final result."""

    sql = load_demo_sql()
    findings = analyze_sql(sql)

    print("Relium YC Demo - SQL/dbt Pipeline Reliability Check")
    print("Metadata-only analysis. No APIs. No secrets. No customer records.")

    print_section("Model being checked")
    print(f"dbt model: {MODEL_NAME}")
    print()
    render_sql(sql)

    print_section("SQL risk detected")
    render_findings(findings)
    print()
    print_wrapped(
        "Relium found a LEFT JOIN followed by a WHERE condition on the right-side "
        "table. That pattern can accidentally behave like an INNER JOIN and remove "
        "customers without matching completed orders."
    )

    print_section("Metadata signals")
    render_metadata_signals()

    print_section("Business impact")
    print_wrapped(
        "The pipeline may technically succeed, but dashboard metrics can become wrong. "
        "Customer counts, conversion rates, and revenue summaries may all shift without "
        "a failed dbt job or failed orchestrator run."
    )

    print_section("Recommended fix")
    print_wrapped(
        "Move the right-table filter into the JOIN condition or explicitly handle NULL rows."
    )
    print()
    print("Example:")
    print("  left join orders o")
    print("    on c.customer_id = o.customer_id")
    print("   and o.order_status = 'completed'")
    print("   and o.updated_at >= current_date - interval '30 day'")

    print_section("Final result")
    print("Status: BLOCK BAD DASHBOARD INPUT")
    print("Relium catches silent analytics pipeline failures before dashboards do.")


if __name__ == "__main__":
    main()
