"""Rendering for a review that is waiting on production metadata.

A waiting review has completed code analysis and has NOT reached a verdict.
The published result must say exactly that. It must never look like a
successful completed verification, because a green check on an unverified
change is worse than no check at all.
"""
from __future__ import annotations

# Not one of ALLOW/WARN/BLOCK. `conclusion_for_decision` maps anything it does
# not recognise as a pass or a failure to the neutral conclusion, so this
# deliberately falls through to neutral.
WAITING_DECISION = "WAITING_FOR_METADATA"
WAITING_FOR_MANIFEST_DECISION = "WAITING_FOR_MANIFEST"


def render_manifest_waiting_result(outcome, *, base_sha, head_sha):
    """Neutral publication for a webhook that arrived before CI evidence."""
    markdown = "\n".join([
        "## Relium deployment review",
        "",
        "**This review is waiting for the CI-generated dbt manifests and has "
        "not reached a decision yet.**",
        "",
        "| | |",
        "|---|---|",
        "| Decision | _not yet decided_ |",
        f"| Lifecycle | `{outcome.lifecycle_state}` |",
        f"| Base commit | `{base_sha}` |",
        f"| Head commit | `{head_sha}` |",
        "",
        "Relium will update **this comment** and **this check** once CI "
        "submits manifests for both exact commits. No approval is "
        "implied until then.",
    ])
    return {
        "decision": WAITING_FOR_MANIFEST_DECISION,
        "final": False,
        "coverage": outcome.coverage,
        "health": outcome.health,
        "lifecycle_state": outcome.lifecycle_state,
        "review_id": outcome.review_id,
        "attempt": outcome.attempt,
        "evidence": dict(outcome.evidence),
        "rendered": {"markdown": markdown},
        "incident": {
            "decision": WAITING_FOR_MANIFEST_DECISION,
            "health": outcome.health,
            "severity": "LOW",
            "confidence": 0,
            "top_reasons": [
                "Both exact BASE and HEAD manifests are required before review."
            ],
            "recommendation": "Wait for the repository CI manifest handoff.",
            "affected_models": [],
        },
    }


def render_waiting_markdown(outcome, *, base_sha, head_sha):
    plan = outcome.plan or {}
    targets = [t for t in plan.get("targets", [])
               if t.get("dependency_kind") == "external"]

    lines = [
        "## Relium deployment review",
        "",
        "**Code analysis complete. This review is waiting for production "
        "metadata and has not reached a decision yet.**",
        "",
        "| | |",
        "|---|---|",
        f"| Decision | _not yet decided_ |",
        f"| Evidence coverage | `{outcome.coverage}` |",
        f"| Health | `{outcome.health}` |",
        f"| Lifecycle | `{outcome.lifecycle_state}` |",
        f"| Base commit | `{base_sha}` |",
        f"| Head commit | `{head_sha}` |",
        "",
        "### Production evidence requested",
        "",
    ]

    if targets:
        lines += ["| Relation | Columns | Signals |", "|---|---|---|"]
        for target in targets:
            columns = ", ".join(f"`{c}`" for c in (target.get("columns") or [])) or "-"
            signals = ", ".join(f"`{s}`" for s in
                                (target.get("required_signals") or [])) or "-"
            lines.append(f"| `{target['relation_name']}` | {columns} | {signals} |")
    else:
        lines.append("_No external production relation was required._")

    head_derived = [t for t in plan.get("targets", [])
                    if t.get("dependency_kind") == "head_derived"]
    if head_derived:
        lines += [
            "",
            "### Produced inside this pull request",
            "",
            "These are created by models changed here, so their absence from "
            "current production is expected and is not a failure:",
            "",
        ]
        lines += [f"- `{t['relation_name']}`" for t in head_derived]

    lines += [
        "",
        "Relium will update **this comment** and **this check** once the "
        "metadata arrives. No approval is implied until then.",
    ]
    return "\n".join(lines)


def render_waiting_result(outcome, *, base_sha, head_sha):
    """Build the publication payload for a waiting review.

    `decision` is deliberately not ALLOW/WARN/BLOCK: the check conclusion
    resolves to neutral, and the rendered body says the review is unfinished.
    """
    markdown = render_waiting_markdown(outcome, base_sha=base_sha,
                                       head_sha=head_sha)
    plan = outcome.plan or {}
    return {
        "decision": WAITING_DECISION,
        "final": False,
        "coverage": outcome.coverage,
        "health": outcome.health,
        "lifecycle_state": outcome.lifecycle_state,
        "review_id": outcome.review_id,
        "attempt": outcome.attempt,
        "collection_request_id": outcome.request_id,
        "requested_relations": [
            t["relation_name"] for t in plan.get("targets", [])
            if t.get("dependency_kind") == "external"
        ],
        "evidence": dict(outcome.evidence),
        "rendered": {"markdown": markdown},
        "incident": {
            "decision": WAITING_DECISION,
            "health": outcome.health,
            "severity": "LOW",
            "confidence": 0,
            "top_reasons": [
                "Code analysis completed; production metadata was requested "
                "and has not arrived yet.",
            ],
            "recommendation": (
                "Run the Relium collector for this pull request, or wait for "
                "the scheduled collection, before relying on this review."),
            "affected_models": list(plan.get("changed_models") or []),
        },
    }
