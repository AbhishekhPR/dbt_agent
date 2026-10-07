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
# Also deliberately neutral, for the same reason: a conflict is not a pass,
# and is not a BLOCK either. Nothing about the CODE has been judged -- Relium
# cannot tell which of two disagreeing manifests describes this commit, so it
# declines to decide rather than guessing.
MANIFEST_CONFLICT_DECISION = "MANIFEST_CONFLICT"


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


_DELIVERY_CONFLICT_EXPLANATION = (
    "Relium keeps one immutable manifest per commit, and the manifest "
    "just submitted for the commit above describes a different project "
    "than the one already recorded for it. Relium will not review from "
    "either document, because it cannot tell which one this commit really "
    "compiled to.",
    "",
    "Usually this means a manifest was generated from a different commit "
    "than the one it was submitted for - a stale `target/` directory, a "
    "cached artifact, or a compile that ran against the wrong ref. "
    "Re-run `dbt compile` from a clean checkout of that exact commit; "
    "Relium retries on the next delivery and updates **this comment** and "
    "**this check** in place. No approval is implied in the meantime.",
)


def _ci_conflict_explanation(conflicts):
    """For a conflict CI's own recompile hit. The commit is the same, so the
    build changed, and the way out is in the build or in a new commit."""
    paths = []
    for conflict in conflicts:
        for path in conflict.get("differences") or ():
            if path not in paths:
                paths.append(path)
    lines = [
        "Relium keeps one immutable manifest per commit. The manifest your CI "
        "just compiled for the commit above disagrees with the one already "
        "recorded for it, so Relium will not review from either document.",
        "",
    ]
    if paths:
        lines += ["Where they differ:", ""]
        lines += [f"- `{path}`" for path in paths]
        lines.append("")
    lines.append(
        "The same commit compiling differently usually means the build "
        "changed, not the code: an unpinned dbt or dbt package upgrade, "
        "different `vars` or profile settings, or a different project "
        "directory. To finish this review, either pin your CI's dbt and "
        "package versions to the ones that produced the stored manifest and "
        "re-run the workflow, or push a new commit (for a base-side conflict, "
        "update the pull request onto a newer base commit). Relium updates "
        "**this comment** and **this check** in place. No approval is implied "
        "in the meantime.")
    return lines


def render_manifest_conflict_result(outcome, *, base_sha, head_sha,
                                    conflicts=()):
    """Action-required publication for a commit whose evidence disagrees.

    Distinct from the waiting render on purpose. "Waiting for CI" tells the
    author to do nothing, which would be false here: nothing is going to
    arrive that resolves this, and the review will not progress until someone
    acts. Still neutral rather than failing -- the code has not been judged.

    Reads ``outcome.evidence``, where the conflicted side is marked CONFLICT.
    ``conflicts`` (or ``evidence["manifest_conflicts"]``) only chooses the
    explanation: one CI recorded names the differing paths and points at the
    build. The reason strings live in the review payload and the audit trail,
    which is where an operator looks; a pull request comment needs to say
    which commit and what to do.
    """
    sides = [
        (name, sha)
        for name, sha in (("base", base_sha), ("head", head_sha))
        if outcome.evidence.get(f"{name}_manifest") == "CONFLICT"
    ]
    rows = [f"| `{sha}` | {name} |" for name, sha in sides]
    conflicts = list(conflicts or outcome.evidence.get("manifest_conflicts")
                     or ())
    explanation = (_ci_conflict_explanation(conflicts)
                   if any(c.get("source") == "ci" for c in conflicts)
                   else _DELIVERY_CONFLICT_EXPLANATION)
    markdown = "\n".join([
        "## Relium deployment review",
        "",
        "**Action required: this commit already has different dbt manifest "
        "evidence, so no review was performed.**",
        "",
        "| | |",
        "|---|---|",
        "| Decision | _not decided_ |",
        f"| Lifecycle | `{outcome.lifecycle_state}` |",
        f"| Base commit | `{base_sha}` |",
        f"| Head commit | `{head_sha}` |",
        "",
        "| Conflicting commit | Side |",
        "|---|---|",
        *(rows or ["| _not recorded_ | |"]),
        "",
        *explanation,
    ])
    return {
        "decision": MANIFEST_CONFLICT_DECISION,
        "final": False,
        "coverage": outcome.coverage,
        "health": outcome.health,
        "lifecycle_state": outcome.lifecycle_state,
        "review_id": outcome.review_id,
        "attempt": outcome.attempt,
        "evidence": dict(outcome.evidence),
        "rendered": {"markdown": markdown},
        "incident": {
            "decision": MANIFEST_CONFLICT_DECISION,
            "health": outcome.health,
            "severity": "LOW",
            "confidence": 0,
            "top_reasons": [
                "This commit already has different dbt manifest evidence."
            ],
            "recommendation": (
                "Re-run dbt compile from a clean checkout of that exact "
                "commit and let CI resubmit."
            ),
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
