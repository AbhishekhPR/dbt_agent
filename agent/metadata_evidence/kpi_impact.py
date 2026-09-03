"""The KPI impact a review already inferred, on its way to storage.

Why this is its own module
--------------------------
The inference is computed exactly once, by the analysis, and is then lifted out
of the analysis result and persisted against the attempt. Two different
processes begin a review and both have to reach the same answer:

  * the direct path - webhook arrives with both manifests already available -
    runs the analysis in ``agent.github_app.runner``;
  * the CI manifest handoff path - webhook arrives first, CI submits the exact
    base and head manifests afterwards, and a worker resumes the review in
    ``agent.metadata_evidence.manifest_handoff``.

This is the same shape of problem ``semantic_evidence.py`` was written for, and
the same shape of fix: one dependency-free extraction, used by both callers, so
a review cannot persist different evidence depending on which route created it.

What is and is not here
-----------------------
Exactly what ``agent.semantic_kpi_inference`` can establish: which discovered
KPIs a changed model reaches, through which models and paths, and with what
confidence. That is a LINEAGE claim.

There is deliberately no monetary figure. Nothing in the KPI machinery observes
revenue, volume or cost — ``DiscoveredKPI`` carries a name, related models and
columns, and a confidence — so a currency amount here would be invented rather
than inferred, and a reviewer would have no way to tell the difference.
"""
from __future__ import annotations

#: Every status this document may carry. ``evaluated`` is the only one the
#: inference produces today; the set exists so a reader can be rejected rather
#: than silently rendered if an unknown status ever appears.
KPI_IMPACT_STATUSES = frozenset({"evaluated"})


def kpi_impact_from_incident(incident) -> dict | None:
    """The stored form of this review's KPI impact inference, or None.

    Returns None when the inference did not run at all — no project context, so
    no semantic graph and no KPIs to test — so an absent inference is stored as
    SQL NULL and can never be read back as "analysed, nothing impacted".

    An inference that RAN and found no impacted KPI is not None. It is a real
    answer, and the difference between "we did not look" and "we looked and
    found nothing" is the reason this is a column of its own.
    """
    metadata = incident.get("metadata") if isinstance(incident, dict) else None
    document = (metadata or {}).get("kpi_impact")
    if not isinstance(document, dict):
        return None
    if document.get("status") not in KPI_IMPACT_STATUSES:
        return None
    return document
