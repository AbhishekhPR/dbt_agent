from agent.diagnose import diagnose_failure
import json

# Simulating a real scenario:
# upstream renamed "order_status" to "status" but the SQL still uses old name

error_log = """
19:34:02  Runtime Error in model fct_orders (models/fct_orders.sql)
  column "order_status" does not exist
  LINE 4: WHERE order_status = 'completed'
  HINT: Perhaps you meant to reference the column "status"
19:34:02  1 of 3 ERROR creating table model dbt_prod.fct_orders
"""

model_sql = """
SELECT
    order_id,
    customer_id,
    order_total,
    order_status
FROM {{ ref('stg_orders') }}
WHERE order_status = 'completed'
"""

upstream_schema = """
stg_orders:
  - order_id: integer
  - customer_id: integer
  - order_total: float
  - status: varchar        <-- renamed from order_status
  - created_at: timestamp
"""

result = diagnose_failure(error_log, model_sql, upstream_schema)


def main():
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
