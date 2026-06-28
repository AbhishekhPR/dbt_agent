from dataclasses import dataclass
from enum import Enum

from agent.signals import Severity, Signal


class DeploymentDecision(str, Enum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    BLOCK = "BLOCK"


@dataclass
class Decision:
    health: int
    decision: DeploymentDecision
    severity: Severity
    confidence: int
    reasons: list[str]
    signals: list[Signal]


SEVERITY_RANKS = {
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def evaluate(signals: list[Signal]):
    health = 100 + sum(signal.score for signal in signals)
    health = max(0, min(100, health))

    if health >= 90:
        decision = DeploymentDecision.ALLOW
    elif health >= 70:
        decision = DeploymentDecision.WARN
    else:
        decision = DeploymentDecision.BLOCK

    severity = Severity.LOW
    if signals:
        severity = max(
            (signal.severity for signal in signals),
            key=lambda value: SEVERITY_RANKS.get(value, 0),
        )

    confidence = 0
    if signals:
        confidence = int(
            sum(signal.confidence for signal in signals) / len(signals)
        )

    reasons = []
    for signal in signals:
        reasons.extend(signal.reasons)

    return Decision(
        health=health,
        decision=decision,
        severity=severity,
        confidence=confidence,
        reasons=reasons,
        signals=signals,
    )
