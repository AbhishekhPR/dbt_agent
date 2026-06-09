from agent.sql_analyzer import analyze_sql_logic
from agent.diagnose import diagnose_failure
from agent.quality_checker import get_table_metrics, detect_anomalies, save_baseline

SQL = """
SELECT * FROM customers c
LEFT JOIN orders o ON c.id = o.customer_id
WHERE o.is_deleted = 0
"""

print("=== SQL Analyzer Demo ===")
report = analyze_sql_logic("demo_model", SQL, context='["id","name","email"]')
print(report)

print("=== Diagnosis Demo ===")+
err = "ERROR: column \"customer_id\" does not exist"
print(diagnose_failure(err, SQL, ""))
