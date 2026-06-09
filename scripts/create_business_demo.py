import sqlite3
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.quality_checker import DEFAULT_FRESHNESS_THRESHOLD_MINUTES, get_table_metrics


DEFAULT_COUNTS = {
    "raw_customers": 10_000,
    "raw_products": 1_000,
    "raw_orders": 100_000,
    "raw_payments": 100_000,
    "raw_events": 300_000,
}

BATCH_SIZE = 10_000
BASE_DATE = datetime(2025, 1, 1, 9, 0, 0)
LOAD_DATE = datetime(2026, 6, 7, 9, 0, 0)

CUSTOMER_SEGMENTS = ("consumer", "small_business", "mid_market", "enterprise")
ACQUISITION_CHANNELS = ("organic", "paid_search", "paid_social", "affiliate", "email", "partner")
COUNTRIES = ("US", "CA", "GB", "DE", "FR", "IN", "AU", "BR", "JP", "SG")
PRODUCT_CATEGORIES = ("apparel", "electronics", "home", "beauty", "sports", "toys", "books", "grocery")
PRODUCT_BRANDS = tuple(f"brand_{idx:02d}" for idx in range(1, 41))
ORDER_STATUSES = ("completed", "completed", "completed", "completed", "refunded", "cancelled", "pending")
PAYMENT_METHODS = ("card", "paypal", "bank_transfer", "gift_card", "wallet")
EVENT_TYPES = ("page_view", "product_view", "add_to_cart", "checkout_started", "purchase", "support_view")

MODEL_SQL = {
    "fct_revenue.sql": """
WITH orders AS (
    SELECT
        DATE(o.created_at) AS order_date,
        o.order_id,
        o.order_status,
        o.order_total
    FROM raw_orders o
),

payments AS (
    SELECT
        p.order_id,
        SUM(CASE WHEN p.payment_status = 'captured' THEN p.amount ELSE 0 END) AS captured_amount,
        SUM(CASE WHEN p.payment_status = 'refunded' THEN p.amount ELSE 0 END) AS refund_amount
    FROM raw_payments p
    GROUP BY p.order_id
)

SELECT
    o.order_date,
    SUM(CASE WHEN o.order_status = 'completed' THEN p.captured_amount ELSE 0 END) AS revenue,
    COUNT(CASE WHEN o.order_status = 'completed' THEN o.order_id END) AS completed_orders,
    SUM(p.refund_amount) AS refund_amount,
    SUM(CASE WHEN o.order_status = 'completed' THEN p.captured_amount ELSE 0 END) - SUM(p.refund_amount) AS net_revenue
FROM orders o
LEFT JOIN payments p ON o.order_id = p.order_id
GROUP BY o.order_date
""".strip(),
    "fct_customer_lifetime_value.sql": """
SELECT
    o.customer_id,
    c.customer_segment,
    c.acquisition_channel,
    COUNT(o.order_id) AS total_orders,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END) AS lifetime_value,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END)
        / NULLIF(COUNT(CASE WHEN o.order_status = 'completed' THEN o.order_id END), 0) AS average_order_value
FROM raw_orders o
LEFT JOIN raw_customers c ON o.customer_id = c.customer_id
WHERE c.is_deleted = 0
GROUP BY
    o.customer_id,
    c.customer_segment,
    c.acquisition_channel
""".strip(),
    "fct_product_performance.sql": """
SELECT
    o.product_id,
    p.category,
    p.brand,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END) AS product_revenue,
    COUNT(o.order_id) AS order_count,
    AVG(CASE WHEN o.order_status = 'completed' THEN o.order_total END) AS average_selling_price,
    SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END)
        / NULLIF(SUM(SUM(CASE WHEN o.order_status = 'completed' THEN o.order_total ELSE 0 END)) OVER (PARTITION BY p.category), 0) AS category_performance
FROM raw_orders o
JOIN raw_products p ON o.product_id = p.product_id
WHERE p.is_active = 1
GROUP BY
    o.product_id,
    p.category,
    p.brand
""".strip(),
    "fct_daily_kpis.sql": """
WITH customer_ltv AS (
    SELECT
        COUNT(customer_id) AS active_customers,
        AVG(lifetime_value) AS average_ltv
    FROM fct_customer_lifetime_value
)

SELECT
    r.order_date,
    r.net_revenue AS daily_revenue,
    (SELECT active_customers FROM customer_ltv) AS active_customers,
    (SELECT average_ltv FROM customer_ltv) AS average_ltv,
    r.completed_orders AS completed_order_count
FROM fct_revenue r
""".strip(),
    "dashboard_executive_metrics.sql": """
WITH top_products AS (
    SELECT
        category,
        SUM(product_revenue) AS category_revenue,
        SUM(order_count) AS category_orders
    FROM fct_product_performance
    GROUP BY category
),

daily_summary AS (
    SELECT
        SUM(daily_revenue) AS total_revenue,
        AVG(daily_revenue) AS average_daily_revenue,
        SUM(completed_order_count) AS completed_orders,
        AVG(active_customers) AS average_active_customers,
        AVG(average_ltv) AS average_ltv
    FROM fct_daily_kpis
)

SELECT
    ds.total_revenue,
    ds.average_daily_revenue,
    ds.completed_orders,
    ds.average_active_customers,
    ds.average_ltv,
    tp.category AS top_category,
    tp.category_revenue AS top_category_revenue
FROM daily_summary ds
LEFT JOIN top_products tp
    ON tp.category_revenue = (
        SELECT MAX(category_revenue)
        FROM top_products
    )
""".strip(),
}


def create_business_demo(base_path: Path | str | None = None, counts: dict | None = None) -> Path:
    root = Path(base_path) if base_path is not None else Path(__file__).resolve().parent.parent
    demo_path = root / "business_demo"
    db_dir = demo_path / "db"
    models_dir = demo_path / "models"
    db_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    resolved_counts = dict(DEFAULT_COUNTS)
    if counts:
        resolved_counts.update(counts)

    _write_models(models_dir)

    db_path = db_dir / "business.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        _drop_tables(conn)
        _create_tables(conn)
        _insert_customers(conn, resolved_counts["raw_customers"])
        _insert_products(conn, resolved_counts["raw_products"])
        _insert_orders(conn, resolved_counts["raw_orders"], resolved_counts)
        _insert_payments(conn, resolved_counts["raw_payments"], resolved_counts["raw_orders"])
        _insert_events(conn, resolved_counts["raw_events"], resolved_counts["raw_customers"])
        _create_indexes(conn)
        conn.commit()
    finally:
        conn.close()

    _sync_quality_baselines(root, db_path)

    print("Business demo database created.")
    print()
    for table in ("raw_customers", "raw_products", "raw_orders", "raw_payments", "raw_events"):
        print(f"{table}: {resolved_counts[table]} rows")

    return db_path


def _write_models(models_dir: Path) -> None:
    for filename, sql in MODEL_SQL.items():
        (models_dir / filename).write_text(sql + "\n", encoding="utf-8")


def _drop_tables(conn: sqlite3.Connection) -> None:
    for table in ("raw_events", "raw_payments", "raw_orders", "raw_products", "raw_customers"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE raw_customers (
            customer_id INTEGER PRIMARY KEY,
            customer_segment TEXT NOT NULL,
            acquisition_channel TEXT NOT NULL,
            signup_date TEXT NOT NULL,
            country TEXT NOT NULL,
            is_deleted INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE raw_products (
            product_id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            brand TEXT NOT NULL,
            price REAL NOT NULL,
            is_active INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE raw_orders (
            order_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            order_status TEXT NOT NULL,
            order_total REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE raw_payments (
            payment_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            payment_status TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE raw_events (
            event_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_time TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _insert_customers(conn: sqlite3.Connection, count: int) -> None:
    rows = []
    for customer_id in range(1, count + 1):
        signup_date = BASE_DATE - timedelta(days=customer_id % 730)
        updated_at = LOAD_DATE - timedelta(minutes=customer_id % 720)
        rows.append(
            (
                customer_id,
                CUSTOMER_SEGMENTS[customer_id % len(CUSTOMER_SEGMENTS)],
                ACQUISITION_CHANNELS[(customer_id * 3) % len(ACQUISITION_CHANNELS)],
                signup_date.strftime("%Y-%m-%d"),
                COUNTRIES[(customer_id * 7) % len(COUNTRIES)],
                1 if customer_id % 97 == 0 else 0,
                updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    conn.executemany("INSERT INTO raw_customers VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _insert_products(conn: sqlite3.Connection, count: int) -> None:
    rows = []
    for product_id in range(1, count + 1):
        price = round(8.0 + (product_id % 250) * 1.37 + (product_id % 9) * 0.11, 2)
        updated_at = LOAD_DATE - timedelta(minutes=product_id % 240)
        rows.append(
            (
                product_id,
                PRODUCT_CATEGORIES[product_id % len(PRODUCT_CATEGORIES)],
                PRODUCT_BRANDS[product_id % len(PRODUCT_BRANDS)],
                price,
                0 if product_id % 53 == 0 else 1,
                updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    conn.executemany("INSERT INTO raw_products VALUES (?, ?, ?, ?, ?, ?)", rows)


def _insert_orders(conn: sqlite3.Connection, count: int, counts: dict) -> None:
    product_count = counts["raw_products"]
    customer_count = counts["raw_customers"]
    rows = []
    for order_id in range(1, count + 1):
        customer_id = ((order_id * 17) % customer_count) + 1
        product_id = ((order_id * 23) % product_count) + 1
        status = ORDER_STATUSES[order_id % len(ORDER_STATUSES)]
        created_at = BASE_DATE + timedelta(minutes=order_id * 11)
        updated_at = LOAD_DATE - timedelta(minutes=order_id % 1440)
        base_amount = 12.5 + (product_id % 300) * 1.29 + (customer_id % 11) * 2.15
        order_total = round(base_amount * (1 + (order_id % 5) * 0.08), 2)
        rows.append(
            (
                order_id,
                customer_id,
                product_id,
                status,
                order_total,
                created_at.strftime("%Y-%m-%d %H:%M:%S"),
                updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        if len(rows) >= BATCH_SIZE:
            conn.executemany("INSERT INTO raw_orders VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            rows.clear()
    if rows:
        conn.executemany("INSERT INTO raw_orders VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _insert_payments(conn: sqlite3.Connection, count: int, order_count: int) -> None:
    rows = []
    for payment_id in range(1, count + 1):
        order_id = ((payment_id - 1) % order_count) + 1
        method = PAYMENT_METHODS[payment_id % len(PAYMENT_METHODS)]
        status = _payment_status(payment_id)
        created_at = BASE_DATE + timedelta(minutes=order_id * 11 + 4)
        updated_at = LOAD_DATE - timedelta(minutes=payment_id % 1440)
        amount = round(15.0 + (order_id % 300) * 1.31 + (payment_id % 13) * 1.75, 2)
        rows.append(
            (
                payment_id,
                order_id,
                method,
                status,
                amount,
                created_at.strftime("%Y-%m-%d %H:%M:%S"),
                updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        if len(rows) >= BATCH_SIZE:
            conn.executemany("INSERT INTO raw_payments VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            rows.clear()
    if rows:
        conn.executemany("INSERT INTO raw_payments VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _insert_events(conn: sqlite3.Connection, count: int, customer_count: int) -> None:
    rows = []
    for event_id in range(1, count + 1):
        customer_id = ((event_id * 19) % customer_count) + 1
        event_time = BASE_DATE + timedelta(minutes=event_id * 3)
        session_id = f"s{customer_id:05d}_{event_id // 6:07d}"
        rows.append(
            (
                event_id,
                customer_id,
                EVENT_TYPES[event_id % len(EVENT_TYPES)],
                session_id,
                (LOAD_DATE - timedelta(minutes=event_id % 1440)).strftime("%Y-%m-%d %H:%M:%S"),
                event_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        if len(rows) >= BATCH_SIZE:
            conn.executemany("INSERT INTO raw_events VALUES (?, ?, ?, ?, ?, ?)", rows)
            rows.clear()
    if rows:
        conn.executemany("INSERT INTO raw_events VALUES (?, ?, ?, ?, ?, ?)", rows)


def _payment_status(payment_id: int) -> str:
    if payment_id % 29 == 0:
        return "refunded"
    if payment_id % 41 == 0:
        return "failed"
    return "captured"


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX idx_raw_orders_customer_id ON raw_orders(customer_id);
        CREATE INDEX idx_raw_orders_product_id ON raw_orders(product_id);
        CREATE INDEX idx_raw_orders_created_at ON raw_orders(created_at);
        CREATE INDEX idx_raw_payments_order_id ON raw_payments(order_id);
        CREATE INDEX idx_raw_events_customer_id ON raw_events(customer_id);
        CREATE INDEX idx_raw_events_event_time ON raw_events(event_time);
        """
    )


def _sync_quality_baselines(root: Path, db_path: Path) -> None:
    baseline_dir = root / "quality_baselines"
    baseline_dir.mkdir(exist_ok=True)
    for table in ("raw_customers", "raw_products", "raw_orders", "raw_payments", "raw_events"):
        metrics = get_table_metrics(str(db_path), table)
        freshness_minutes = metrics.get("freshness_minutes")
        if freshness_minutes is not None and freshness_minutes > DEFAULT_FRESHNESS_THRESHOLD_MINUTES:
            metrics["freshness_threshold_minutes"] = freshness_minutes + DEFAULT_FRESHNESS_THRESHOLD_MINUTES
        (baseline_dir / f"{table}.json").write_text(
            json.dumps(metrics, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    create_business_demo()
