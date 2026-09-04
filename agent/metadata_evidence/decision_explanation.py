"""Attempt-scoped explanation for a metadata-backed review decision.

The numeric health score is computed before warehouse evidence is requested.
This module does not re-score a review; it records and projects why the
already-computed score or evidence policy produced its verdict.
"""
from __future__ import annotations


HEALTH_LABEL = "Code review health"
HEALTH_BASIS = "static_code_and_manifest_analysis"


def build_health_explanation(score, supplied=None, findings=()):
    """Return a bounded, customer-safe description of a code health score."""
    if score is None:
        return None
    score = max(0, min(100, int(score)))
    supplied = supplied if isinstance(supplied, dict) else {}
    deductions = []
    for item in supplied.get("deductions") or []:
        if not isinstance(item, dict):
            continue
        points = item.get("points")
        if not isinstance(points, (int, float)) or isinstance(points, bool):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        deductions.append({
            "component": str(item.get("component") or "static_analysis")[:80],
            "points": abs(int(points)),
            "reason": reason[:1000],
        })

    if not deductions and score < 100:
        first = next(
            (
                item for item in findings
                if isinstance(item, dict)
                and item.get("category") == "code"
                and str(item.get("message") or "").strip()
            ),
            None,
        )
        if first is not None:
            deductions.append({
                "component": "static_analysis",
                "points": 100 - score,
                "reason": str(first["message"])[:1000],
            })

    return {
        "score": score,
        "label": HEALTH_LABEL,
        "basis": HEALTH_BASIS,
        "deductions": deductions,
    }


def primary_reason_for(decision, findings=(), policy_reasons=(),
                       health_explanation=None):
    """Choose one truthful reason for WARN/BLOCK without inventing evidence."""
    decision = str(decision or "").upper()
    if decision not in {"WARN", "BLOCK"}:
        return None

    findings = [item for item in findings if isinstance(item, dict)]
    severity_order = ("block", "warn") if decision == "BLOCK" else ("warn", "block")
    for severity in severity_order:
        for finding in findings:
            message = str(finding.get("message") or "").strip()
            if finding.get("severity") == severity and message:
                return message[:1000]

    for reason in policy_reasons or ():
        text = str(reason or "").strip()
        if text:
            return text[:1000]

    health = health_explanation if isinstance(health_explanation, dict) else {}
    for deduction in health.get("deductions") or []:
        text = str((deduction or {}).get("reason") or "").strip()
        if text:
            return text[:1000]

    score = health.get("score")
    if isinstance(score, int):
        threshold = 70 if decision == "BLOCK" else 90
        if score < threshold:
            consequence = "merge" if decision == "BLOCK" else "warning"
            return (
                f"Code review health is {score}/100, below the {threshold}-point "
                f"{consequence} threshold."
            )

    return (
        f"This historical {decision} attempt did not record a detailed reason."
    )


def build_attempt_payload(*, decision, health, findings, policy_reasons=(),
                          health_explanation=None, **extra):
    """Build the immutable explanation document stored on one attempt."""
    finding_rows = [dict(item) for item in findings]
    health_view = build_health_explanation(
        health, supplied=health_explanation, findings=finding_rows)
    payload = {
        **extra,
        "findings": finding_rows,
        "decision_reasons": [
            str(reason)[:1000] for reason in (policy_reasons or ()) if str(reason).strip()
        ],
        "primary_reason": primary_reason_for(
            decision, finding_rows, policy_reasons, health_view),
        "health_explanation": health_view,
    }
    return payload


def explanation_for_attempt(attempt):
    """Project new and legacy attempts through one compatibility path."""
    payload = attempt.get("payload") if isinstance(attempt, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    findings = payload.get("findings") or []
    health = build_health_explanation(
        attempt.get("health") if isinstance(attempt, dict) else None,
        supplied=payload.get("health_explanation"),
        findings=findings,
    )
    stored = payload.get("primary_reason")
    primary = str(stored).strip()[:1000] if isinstance(stored, str) and stored.strip() else None
    if primary is None:
        primary = primary_reason_for(
            attempt.get("decision") if isinstance(attempt, dict) else None,
            findings,
            payload.get("decision_reasons") or (),
            health,
        )
    return {"primary_reason": primary, "health_explanation": health}
