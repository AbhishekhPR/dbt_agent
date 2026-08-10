"""The downloadable production metadata evidence bundle.

One file, built from what an attempt already recorded. The dashboard shows
WHAT CHANGED; this is the artifact a customer keeps when they need the full
bounded observation behind those changes - the values that did not change
included, because "this signal was measured and held steady" is exactly the
context a change card cannot carry without becoming a catalogue.

Two properties matter more than anything else here.

STABILITY. The bundle is assembled from the snapshot ids the attempt itself
recorded, and from nothing else. It never asks the store for "the latest
snapshot", so downloading an old attempt's evidence a month later produces the
same bytes it would have produced the day it ran. The snapshots those ids point
at are immutable at the database boundary (migrations 0004 and 0012), so this
is a real guarantee rather than a hopeful one. Nothing in the bundle is
generated at download time - there is no timestamp of export, no request id -
so two downloads of the same attempt are byte-identical.

DISCLOSURE. Every field is named by an allowlist below. A field the schema
gains later is absent from the export until someone decides it belongs there,
which is the safe direction for a file a customer will forward by email. In
particular the export carries no `min_value`/`max_value` - the only two columns
in the whole schema derived from an actual cell of a customer's table - no
collector identity, no hashes, no manifests, no SQL and no paths.
"""
from __future__ import annotations

# Snapshot-level fields. `observed_at` and `collected_at` are the observation
# timestamps; `received_at`, `expires_at` and `ttl_seconds` are Relium's own
# bookkeeping and are not the customer's evidence.
_SNAPSHOT_FIELDS = (
    "snapshot_id",
    "environment",
    "completeness",
    "freshness_state",
    "observed_at",
    "collected_at",
)

# Relation-level fields: identity, existence, structure and the bounded
# aggregates.
#
# Deliberately absent: `relation_index` and `column_index` (positional, and
# meaningless outside one snapshot), `collection_error` (a collector message
# that could quote a DSN or a query), `unevaluated_checks`, and the dbt and
# lineage columns, none of which the evidence contract names.
_RELATION_FIELDS = (
    "model_unique_id",
    "relation_database",
    "relation_schema",
    "relation_name",
    "relation_type",
    "exists_in_production",
    "schema_fingerprint",
    "row_count",
    "freshness_timestamp",
    "freshness_lag_seconds",
    "collection_status",
    "observed_at",
)

# Column-level fields.
#
# `min_value` and `max_value` are excluded and must stay excluded. They are the
# only fields in the schema whose value is an actual cell from a customer
# table, and a downloadable file is precisely the wrong place for them.
_COLUMN_FIELDS = (
    "column_name",
    "exists_in_production",
    "data_type",
    "is_nullable",
    "ordinal_position",
    "null_count",
    "null_rate",
    "duplicate_count",
    "duplicate_rate",
    "distinct_count",
    "cardinality",
    "collection_status",
)


def _isoformat(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _pick(row, fields):
    """Copy exactly the named fields, converting timestamps to ISO strings.

    A field the row does not carry is emitted as null rather than omitted, so
    the bundle's shape is the same for every snapshot and a consumer never has
    to distinguish "absent key" from "not observed".
    """
    return {field: _isoformat(row.get(field)) for field in fields}


def bounded_observation(snapshot):
    """One production observation, reduced to the disclosable evidence.

    Returns None for a missing snapshot - which is what a ``no_baseline``
    comparison has - rather than an empty object that would read as an
    observation in which nothing existed.
    """
    if not snapshot:
        return None
    observation = _pick(snapshot, _SNAPSHOT_FIELDS)
    observation["relations"] = [
        {**_pick(relation, _RELATION_FIELDS),
         "columns": [_pick(column, _COLUMN_FIELDS)
                     for column in (relation.get("columns") or [])]}
        for relation in (snapshot.get("relations") or [])
    ]
    return observation


def build_evidence_bundle(store, *, organization_id, repository_id, environment,
                          review_id, attempt, comparison):
    """Assemble the bundle for one attempt from its own recorded evidence.

    ``comparison`` is the document stored on the attempt. The snapshot ids come
    from it and from nowhere else: that is what binds the download to the same
    two observations the dashboard is describing, permanently.
    """
    baseline_id = comparison.get("baseline_snapshot_id")
    current_id = comparison.get("current_snapshot_id")

    baseline = (store.get_snapshot(organization_id, repository_id, baseline_id)
                if baseline_id else None)
    current = (store.get_snapshot(organization_id, repository_id, current_id)
               if current_id else None)

    return {
        "evidence_type": "production_metadata_comparison",
        "evidence_version": 1,
        "review_id": review_id,
        "attempt": attempt,
        "organization_id": organization_id,
        "repository_id": repository_id,
        "environment": environment,
        # The comparison exactly as the attempt recorded it, already projected
        # through the API's per-kind allowlist by the caller.
        "comparison": comparison,
        # The full bounded observations behind it. These carry every signal
        # that was measured, including the ones that did not change - which is
        # the reason this file exists and the reason the dashboard is not it.
        "baseline_observation": bounded_observation(baseline),
        "current_observation": bounded_observation(current),
    }


def evidence_filename(review_id, attempt) -> str:
    """A predictable, filesystem-safe name for the downloaded artifact."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(review_id))
    return f"relium-metadata-evidence-{safe}-attempt-{int(attempt)}.json"
