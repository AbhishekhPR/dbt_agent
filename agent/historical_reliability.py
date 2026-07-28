from agent.signals import Signal


LOW_SEVERITY_THRESHOLD = 85
MEDIUM_SEVERITY_THRESHOLD = 70
INCIDENT_PENALTY = 10
ROLLBACK_PENALTY = 15
MAX_DEPLOYMENT_BONUS = 5


def evaluate_history(history: dict) -> dict:
    deployment_count = _non_negative_int(history.get("deployment_count", 0))
    incident_count = _non_negative_int(history.get("incident_count", 0))
    rollback_count = _non_negative_int(history.get("rollback_count", 0))
    average_health_score = _clamp(
        _number(history.get("average_health_score", 100)),
        0,
        100,
    )

    deployment_bonus = min(deployment_count * 0.25, MAX_DEPLOYMENT_BONUS)
    raw_score = (
        average_health_score
        + deployment_bonus
        - (incident_count * INCIDENT_PENALTY)
        - (rollback_count * ROLLBACK_PENALTY)
    )
    score = round(_clamp(raw_score, 0, 100))
    severity = _severity(score)

    metadata = {
        "deployment_count": deployment_count,
        "incident_count": incident_count,
        "rollback_count": rollback_count,
        "average_health_score": average_health_score,
        "incident_rate": _rate(incident_count, deployment_count),
        "rollback_rate": _rate(rollback_count, deployment_count),
    }
    for key, value in history.items():
        if key not in metadata:
            metadata[key] = value

    return {
        "score": score,
        "severity": severity,
        "confidence": _confidence(deployment_count),
        "reasons": _reasons(
            score=score,
            incident_count=incident_count,
            rollback_count=rollback_count,
            average_health_score=average_health_score,
        ),
        "metadata": metadata,
    }


def to_signal(result: dict) -> Signal:
    score = _clamp(_number(result.get("score", 100)), 0, 100)
    return Signal(
        component="historical_reliability",
        severity=result.get("severity", _severity(score)),
        confidence=_non_negative_int(result.get("confidence", 75)),
        score=round(score - 100),
        reasons=list(result.get("reasons", [])),
        metadata=dict(result.get("metadata", {})),
    )


def _severity(score: int | float) -> str:
    if score >= LOW_SEVERITY_THRESHOLD:
        return "LOW"
    if score >= MEDIUM_SEVERITY_THRESHOLD:
        return "MEDIUM"
    return "HIGH"


def _confidence(deployment_count: int) -> int:
    if deployment_count >= 10:
        return 95
    if deployment_count >= 3:
        return 85
    return 75


def _reasons(
    *,
    score: int,
    incident_count: int,
    rollback_count: int,
    average_health_score: int | float,
) -> list[str]:
    reasons = []
    if incident_count >= 2:
        reasons.append("Repeated incidents detected")
    elif incident_count == 1:
        reasons.append("Incident history detected")

    if rollback_count >= 2:
        reasons.append("Repeated rollbacks detected")
    elif rollback_count == 1:
        reasons.append("Rollback history detected")

    if average_health_score < LOW_SEVERITY_THRESHOLD:
        reasons.append("Average health score below target")

    if not reasons and score >= LOW_SEVERITY_THRESHOLD:
        reasons.append("Historical reliability is strong")

    return reasons


def _rate(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(count / total, 4)


def _non_negative_int(value) -> int:
    return max(0, int(_number(value)))


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: int | float, minimum: int, maximum: int) -> int | float:
    return max(minimum, min(maximum, value))
