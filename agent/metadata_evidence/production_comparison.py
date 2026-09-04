"""Deterministic comparison of two production metadata observations.

This answers exactly one question:

    What changed in production metadata between the previous eligible
    observation and this one?

It does NOT answer "what did this PR cause". The baseline is the previous
production observation for the same organization, repository and environment;
nothing about it is bound to a commit, a manifest or a diff, and no output here
attributes a change to anybody's code. The evidence is descriptive.

Three rules shape everything below.

1. Absence is not zero. The collector is targeted, so a snapshot contains what
   was asked for and nothing else. A relation the current snapshot never looked
   at is not "missing from production", and a NULL metric is not 0. The legacy
   ``metadata_drift`` module coerced ``None`` to ``0`` before subtracting,
   which is how "we did not measure it" became "it dropped to zero". Only
   values both snapshots genuinely observed are compared.

2. Only the intersection is compared, and the gap is reported. A relation in
   the baseline that this snapshot did not observe produces no change - it
   would be inventing a removal out of a collection scope. A relation in this
   snapshot with no baseline counterpart produces no change either, but it does
   count against coverage, which is what makes the result ``partial`` instead
   of silently looking complete.

3. This is evidence, not policy. Nothing here computes severity, health, a
   verdict or a threshold. A row count falling and a row count rising are the
   same kind of fact to this module.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STATUS_EVALUATED = "evaluated"
STATUS_PARTIAL = "partial"
STATUS_NO_BASELINE = "no_baseline"
STATUS_UNAVAILABLE = "unavailable"

# A snapshot whose collection outright failed carries no trustworthy
# observation, so it is not a legitimate point of comparison. PARTIAL is
# eligible: it observed less than was asked for, which coverage reports, but
# what it did observe is real.
ELIGIBLE_COMPLETENESS = ("COMPLETE", "PARTIAL")
# A stale observation is still retained for audit, but it cannot describe the
# production state against which a later current observation is compared.
ELIGIBLE_FRESHNESS = ("CURRENT",)

# Relation and column rows the collector could not evaluate are present in the
# snapshot but carry no observation. Treating them as observed would compare a
# value nobody measured.
_OBSERVED_STATUSES = frozenset({"COLLECTED", "PARTIAL"})

# Relative deltas are rounded so that the same two snapshots always produce a
# byte-identical evidence document. Five places is enough to distinguish a
# fraction of a percent without exposing float noise as meaningful precision.
_RELATIVE_PLACES = 5
_POINT_PLACES = 6


# ------------------------------------------------------------------ identity

def _relation_identity(relation):
    """The stable identities a relation can be matched on, most stable first.

    ``model_unique_id`` is the dbt graph identity and survives a relation being
    renamed or moved between schemas. The database/schema/name triple is the
    warehouse identity and is used when the graph identity is absent, or when
    one of the two snapshots recorded it and the other did not.
    """
    keys = []
    model_unique_id = relation.get("model_unique_id")
    if model_unique_id:
        keys.append(("model", str(model_unique_id)))
    name = relation.get("relation_name")
    if name:
        keys.append(("relation", (
            str(relation.get("relation_database") or ""),
            str(relation.get("relation_schema") or ""),
            str(name),
        )))
    return keys


def _index_relations(snapshot):
    """Index a snapshot's relations by every identity they can be matched on.

    ``relation_index`` is positional and means nothing across snapshots, so it
    is used only as the tie-break: a snapshot that somehow carries two rows for
    one identity resolves to the first, deterministically, rather than to
    whichever row the driver happened to return last.
    """
    index = {}
    for relation in snapshot.get("relations") or []:
        for key in _relation_identity(relation):
            existing = index.get(key)
            if existing is None or _position(relation) < _position(existing):
                index[key] = relation
    return index


def _position(row):
    value = row.get("relation_index")
    return value if isinstance(value, int) else 0


def _match_relation(index, relation):
    for key in _relation_identity(relation):
        found = index.get(key)
        if found is not None:
            return found
    return None


def _index_columns(relation):
    """Columns of one relation by name, the only identity stable across
    snapshots. ``column_index`` is positional and is used only to tie-break."""
    index = {}
    for position, column in enumerate(relation.get("columns") or []):
        name = column.get("column_name")
        if not name:
            continue
        name = str(name)
        if name in index:
            continue
        index[name] = (position, column)
    return {name: column for name, (_, column) in index.items()}


# ----------------------------------------------------------------- observed

def _observed(entity):
    """Whether this row carries a real observation.

    A row that exists with a status of SKIPPED, UNSUPPORTED or FAILED is a
    record that Relium did not measure the thing - which is different from
    measuring it and finding it absent.
    """
    return entity.get("collection_status", "COLLECTED") in _OBSERVED_STATUSES


def _exists(entity):
    value = entity.get("exists_in_production")
    return True if value is None else bool(value)


def _both(before, after):
    """A signal is comparable only when both sides genuinely carry a value."""
    return before is not None and after is not None


# ------------------------------------------------------------------ changes

def _change(kind, *, signal, relation, column=None, before=None, after=None, **extra):
    change = {
        "kind": kind,
        "model": relation.get("model_unique_id"),
        "relation": relation.get("relation_name"),
        "column": column,
        "signal": signal,
        "before": before,
        "after": after,
    }
    change.update(extra)
    return change


def _absolute_and_relative(before, after):
    """Delta fields for a count.

    ``relative_delta`` is omitted rather than set to zero or infinity when the
    baseline is 0: there is no meaningful proportion of nothing, and emitting
    one would be a number the data does not support.
    """
    fields = {"absolute_delta": after - before}
    if before != 0:
        fields["relative_delta"] = round((after - before) / before, _RELATIVE_PLACES)
    return fields


def _percentage_points(before, after):
    """Delta for a rate stored as a 0..1 fraction.

    The result is in PERCENTAGE POINTS, not percent. 1.2% to 14.8% is +13.6
    percentage points; calling that a 1133% increase would be a different
    (and here, misleading) statement. The field name carries the unit so the
    distinction cannot be lost downstream.
    """
    return {"percentage_point_delta": round((after - before) * 100, _POINT_PLACES)}


def _compare_relation(baseline, current):
    """Structural and behavioural changes for one matched relation."""
    changes = []

    baseline_exists = _exists(baseline)
    current_exists = _exists(current)
    if baseline_exists != current_exists:
        changes.append(_change(
            "relation_availability_changed", signal="relation_exists",
            relation=current, before=baseline_exists, after=current_exists))
        # A relation that has appeared or disappeared has no comparable
        # behavioural metrics on the missing side; anything else would be
        # comparing a measurement against nothing.
        return changes

    if not current_exists:
        return changes

    before_fp = baseline.get("schema_fingerprint")
    after_fp = current.get("schema_fingerprint")
    if _both(before_fp, after_fp) and before_fp != after_fp:
        changes.append(_change(
            "schema_fingerprint_changed", signal="schema_fingerprint",
            relation=current, before=before_fp, after=after_fp))

    before_rows = baseline.get("row_count")
    after_rows = current.get("row_count")
    if _both(before_rows, after_rows) and before_rows != after_rows:
        before_rows, after_rows = int(before_rows), int(after_rows)
        changes.append(_change(
            "row_count_changed", signal="row_count", relation=current,
            before=before_rows, after=after_rows,
            **_absolute_and_relative(before_rows, after_rows)))

    # The canonical persisted freshness representation is the lag in seconds.
    # ``freshness_timestamp`` is the raw observation and moves on every
    # collection, so comparing it would report a change on every snapshot.
    before_lag = baseline.get("freshness_lag_seconds")
    after_lag = current.get("freshness_lag_seconds")
    if _both(before_lag, after_lag) and before_lag != after_lag:
        before_lag, after_lag = int(before_lag), int(after_lag)
        changes.append(_change(
            "freshness_changed", signal="freshness", relation=current,
            before=before_lag, after=after_lag,
            absolute_delta=after_lag - before_lag))

    changes.extend(_compare_columns(baseline, current))
    return changes


_COUNT_SIGNALS = (
    ("distinct_count", "distinct_count_changed"),
)

# Rates are fractions in [0, 1] and are compared in PERCENTAGE POINTS.
#
# `cardinality` belongs here, not with the counts. The collector computes it as
# distinct_count / row_count (agent/collector/warehouse.py), so it is a ratio -
# the rate-shaped twin of distinct_count, exactly as duplicate_rate is the twin
# of duplicate_count. Treating it as a count meant coercing it with int(),
# which turned a cardinality of 0.37 into 0 and reported a real change as
# either nothing or a total collapse. Migration 0012 fixes the storage type;
# this fixes the arithmetic that reads it.
_RATE_SIGNALS = (
    ("null_rate", "null_rate_changed"),
    ("duplicate_rate", "duplicate_rate_changed"),
    ("cardinality", "cardinality_changed"),
)


def _compare_columns(baseline_relation, current_relation):
    changes = []
    baseline_columns = _index_columns(baseline_relation)

    for column in current_relation.get("columns") or []:
        name = column.get("column_name")
        if not name or not _observed(column):
            continue
        before = baseline_columns.get(str(name))
        if before is None or not _observed(before):
            continue

        before_exists = _exists(before)
        after_exists = _exists(column)
        if before_exists != after_exists:
            changes.append(_change(
                "column_availability_changed", signal="column_exists",
                relation=current_relation, column=str(name),
                before=before_exists, after=after_exists))
            continue
        if not after_exists:
            continue

        before_type = before.get("data_type")
        after_type = column.get("data_type")
        if _both(before_type, after_type) and before_type != after_type:
            changes.append(_change(
                "column_type_changed", signal="data_type",
                relation=current_relation, column=str(name),
                before=before_type, after=after_type))

        before_nullable = before.get("is_nullable")
        after_nullable = column.get("is_nullable")
        if _both(before_nullable, after_nullable) and before_nullable != after_nullable:
            changes.append(_change(
                "column_nullability_changed", signal="nullable",
                relation=current_relation, column=str(name),
                before=bool(before_nullable), after=bool(after_nullable)))

        for signal, kind in _RATE_SIGNALS:
            before_rate = before.get(signal)
            after_rate = column.get(signal)
            if _both(before_rate, after_rate) and before_rate != after_rate:
                before_rate, after_rate = float(before_rate), float(after_rate)
                changes.append(_change(
                    kind, signal=signal, relation=current_relation, column=str(name),
                    before=before_rate, after=after_rate,
                    **_percentage_points(before_rate, after_rate)))

        for signal, kind in _COUNT_SIGNALS:
            before_count = before.get(signal)
            after_count = column.get(signal)
            if _both(before_count, after_count) and before_count != after_count:
                before_count, after_count = int(before_count), int(after_count)
                changes.append(_change(
                    kind, signal=signal, relation=current_relation, column=str(name),
                    before=before_count, after=after_count,
                    **_absolute_and_relative(before_count, after_count)))

    return changes


def _sort_key(change):
    return (
        str(change.get("relation") or ""),
        str(change.get("model") or ""),
        str(change.get("column") or ""),
        str(change.get("signal") or ""),
        str(change.get("kind") or ""),
    )


# ------------------------------------------------------------------ builder

def _isoformat(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def build_comparison(baseline, current):
    """Compare two expanded snapshots. Pure: no store, no clock, no policy.

    ``baseline`` of None is the caller's way of saying no prior eligible
    observation exists, and produces ``no_baseline`` with no changes rather
    than a comparison against zeros.
    """
    if current is None:
        return {"status": STATUS_UNAVAILABLE,
                "reason": "no current production observation was available",
                "baseline_snapshot_id": None, "current_snapshot_id": None,
                "baseline_observed_at": None, "current_observed_at": None,
                "changes": []}

    # A snapshot row that was read without its observations carries no
    # `relations` key at all, which is not the same as observing nothing. Left
    # unchecked it would sail through as "evaluated, no changes" - a clean bill
    # of health derived from data that was never loaded. An expanded snapshot
    # always has the key, even when the list is empty.
    if "relations" not in current or (baseline is not None
                                      and "relations" not in baseline):
        return {"status": STATUS_UNAVAILABLE,
                "reason": "a snapshot was read without its observations",
                "baseline_snapshot_id": baseline.get("snapshot_id") if baseline else None,
                "current_snapshot_id": current.get("snapshot_id"),
                "baseline_observed_at": None,
                "current_observed_at": _isoformat(current.get("observed_at")),
                "changes": []}

    header = {
        "baseline_snapshot_id": baseline.get("snapshot_id") if baseline else None,
        "current_snapshot_id": current.get("snapshot_id"),
        "baseline_observed_at": _isoformat(baseline.get("observed_at")) if baseline else None,
        "current_observed_at": _isoformat(current.get("observed_at")),
    }

    if baseline is None:
        # No zeros are manufactured and no coverage is claimed: there was
        # nothing to cover.
        return {"status": STATUS_NO_BASELINE, **header, "changes": []}

    baseline_index = _index_relations(baseline)

    changes = []
    relations_observed = 0
    relations_compared = 0
    columns_observed = 0
    columns_compared = 0
    relations_without_baseline = []
    columns_without_baseline = []

    for relation in current.get("relations") or []:
        if not _observed(relation):
            continue
        relations_observed += 1
        observed_columns = [c for c in (relation.get("columns") or []) if _observed(c)]
        columns_observed += len(observed_columns)

        match = _match_relation(baseline_index, relation)
        if match is None or not _observed(match):
            relations_without_baseline.append({
                "model": relation.get("model_unique_id"),
                "relation": relation.get("relation_name"),
            })
            continue
        relations_compared += 1

        baseline_columns = _index_columns(match)
        for column in observed_columns:
            name = str(column.get("column_name") or "")
            counterpart = baseline_columns.get(name)
            if counterpart is None or not _observed(counterpart):
                columns_without_baseline.append({
                    "model": relation.get("model_unique_id"),
                    "relation": relation.get("relation_name"),
                    "column": name,
                })
            else:
                columns_compared += 1

        changes.extend(_compare_relation(match, relation))

    coverage = {
        "relations_observed": relations_observed,
        "relations_compared": relations_compared,
        "relations_without_baseline": relations_without_baseline,
        "columns_observed": columns_observed,
        "columns_compared": columns_compared,
        "columns_without_baseline": columns_without_baseline,
        "baseline_completeness": baseline.get("completeness"),
        "current_completeness": current.get("completeness"),
    }

    incomplete = bool(relations_without_baseline or columns_without_baseline)
    # A snapshot that itself reports PARTIAL observed less than was asked of
    # it, so the comparison cannot be complete regardless of how well the two
    # sides matched.
    incomplete = incomplete or "PARTIAL" in (
        baseline.get("completeness"), current.get("completeness"))

    return {
        "status": STATUS_PARTIAL if incomplete else STATUS_EVALUATED,
        **header,
        "changes": sorted(changes, key=_sort_key),
        "coverage": coverage,
    }


# ------------------------------------------------------------- store-backed

def compute_comparison(store, *, organization_id, repository_id, environment,
                       current_snapshot):
    """Select the baseline for ``current_snapshot`` and compare against it.

    The result is computed once, by the caller that already holds the durable
    current snapshot, and is meant to be written straight onto the attempt. It
    is deliberately not recomputed at read time: an attempt that recorded
    baseline X against current Y must still say X against Y after a newer
    snapshot arrives.

    A failure to compute is reported as ``unavailable`` rather than raised.
    Losing the comparison must not fail the review lifecycle that produced it -
    the decision does not depend on this evidence, and section 9 keeps it that
    way.
    """
    if current_snapshot is None:
        return build_comparison(None, None)
    try:
        baseline = store.previous_production_snapshot(
            organization_id, repository_id, environment,
            snapshot_id=current_snapshot["snapshot_id"],
            observed_at=current_snapshot["observed_at"],
            received_at=current_snapshot.get("received_at"),
        )
        return build_comparison(baseline, current_snapshot)
    except Exception:  # pragma: no cover - defensive, exercised via tests
        logger.exception("production metadata comparison failed for snapshot %s",
                         current_snapshot.get("snapshot_id"))
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": "the comparison could not be computed",
            "baseline_snapshot_id": None,
            "current_snapshot_id": current_snapshot.get("snapshot_id"),
            "baseline_observed_at": None,
            "current_observed_at": _isoformat(current_snapshot.get("observed_at")),
            "changes": [],
        }
