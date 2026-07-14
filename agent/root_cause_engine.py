def analyze_root_cause(anomaly: dict) -> dict:
    table = anomaly.get("table") or anomaly.get("affected_file") or "unknown"
    message = anomaly.get("message") or anomaly.get("explanation") or "Anomaly detected"
    return {
        "table": table,
        "anomaly": anomaly.get("type") or anomaly.get("metric"),
        "likely_causes": [
            {
                "cause": message,
                "confidence": 0.75,
                "reason": "Deterministic metadata anomaly evidence.",
            }
        ],
        "affected_models": [],
        "impact_count": 0,
        "recommended_actions": [
            anomaly.get("recommendation") or "Investigate recent upstream changes."
        ],
    }


def run_root_cause(project: str, table: str, anomaly: str, message: str = "") -> dict:
    result = analyze_root_cause(
        {
            "project_path": project,
            "table": table,
            "type": anomaly,
            "message": message or anomaly,
        }
    )
    print("Root Cause Analysis")
    print(f"Table: {table}")
    print(f"Anomaly: {anomaly}")
    for cause in result.get("likely_causes", []):
        print(f"- {cause.get('cause')}")
    return result
