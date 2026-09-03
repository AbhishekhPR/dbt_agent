"""The SQL semantic comparison a review already produced, on its way to storage.

Why this is its own module
--------------------------
The comparison is computed exactly once, by the analysis, and is then lifted
out of the analysis result and persisted against the attempt. Extracting it
lived inside ``agent.github_app.runner``, which meant only ONE of the two
paths that begin a review could reach it:

  * the direct path - webhook arrives with both manifests already available -
    called ``lifecycle.begin(semantic_evidence=...)`` and stored it;
  * the CI manifest handoff path - webhook arrives first, CI submits the exact
    base and head manifests afterwards, and a worker resumes the review -
    ran the very same analysis, produced the very same comparison, and then
    called ``begin_review`` without it.

So a review that took the canonical hosted-manifest route persisted SQL NULL
for its semantic evidence, and the dashboard - correctly, given what it was
handed - reported that SQL semantic comparison was not available. The comparison
had in fact run.

This module is the shared, dependency-free extraction both paths use.
"""
from __future__ import annotations


def semantic_evidence_from_incident(incident) -> dict | None:
    """The stored form of this review's SQL semantic comparison, or None.

    Returns None when no comparison ran at all - for example when the base
    manifest could not be fetched - so an absent comparison is stored as SQL
    NULL and can never be read back as "compared, nothing changed".

    A comparison that ran and produced an empty ``models`` list is also None:
    with no model to describe there is nothing to persist, and an empty
    document would read as a comparison that covered something.
    """
    metadata = incident.get("metadata") if isinstance(incident, dict) else None
    comparison = (metadata or {}).get("manifest_comparison") or {}
    evidence = comparison.get("sql_semantic_comparison")
    if not isinstance(evidence, dict) or not evidence.get("models"):
        return None
    return evidence
