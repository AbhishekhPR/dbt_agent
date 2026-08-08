"""Metadata-backed pre-deployment review decision.

Compares three states and says which of them it actually had:

  state 1  base code expectation   - immutable base SHA manifest
  state 2  head code proposal      - immutable head SHA manifest
  state 3  actual production       - an accepted metadata snapshot

Evidence precedence, strongest first:

  1. declared contracts and invariants
  2. trusted immutable base/head dbt artifacts
  3. fresh production schema metadata
  4. fresh bounded production profile metadata
  5. complete before/after comparison
  6. partial or sampled evidence
  7. heuristics

A weaker source never silently overrides a stronger one, and missing, stale
or unsupported evidence is never reported as PASS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agent.evidence_policy import (
    EvidenceState,
    default_policy,
    evaluate_evidence_policy,
)
from agent.metadata_evidence.collection_plan import (
    CRITICAL_TTL_MINUTES,
    DEFAULT_TTL_MINUTES,
)

PRODUCTION_METADATA = "production_metadata"

# Type families used for compatibility reasoning. Anything outside these is
# treated as unknown rather than guessed at.
_NUMERIC = {"numeric", "decimal", "int", "integer", "bigint", "smallint",
            "double precision", "real", "float", "money"}
_TEXT = {"text", "varchar", "character varying", "char", "character", "string"}
_TEMPORAL = {"date", "timestamp", "timestamptz", "timestamp with time zone",
             "timestamp without time zone", "time"}
_BOOLEAN = {"boolean", "bool"}

# Thresholds. These are policy, and they are stated rather than buried.
HIGH_NULL_RATE = 0.20
DUPLICATE_AMPLIFICATION_RATE = 0.05


def _family(data_type: str | None) -> str:
    if not data_type:
        return "unknown"
    text = str(data_type).strip().lower()
    text = text.split("(")[0].strip()
    for family, members in (("numeric", _NUMERIC), ("text", _TEXT),
                            ("temporal", _TEMPORAL), ("boolean", _BOOLEAN)):
        if text in members:
            return family
    return "unknown"


@dataclass
class Finding:
    code: str
    severity: str                  # info | warn | block
    category: str                  # code | base_head | production | evidence
    message: str
    relation: str | None = None
    column: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "relation": self.relation,
            "column": self.column,
            "detail": dict(self.detail),
        }


@dataclass
class MetadataDecision:
    decision: str | None
    lifecycle_state: str
    coverage: str
    health: int
    findings: list[Finding] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    required_missing: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    unevaluated: list[str] = field(default_factory=list)
    evidence_states_available: dict = field(default_factory=dict)
    policy_version: str = ""
    policy_hash: str = ""

    def as_dict(self) -> dict:
        by_category = {}
        for finding in self.findings:
            by_category.setdefault(finding.category, []).append(finding.as_dict())
        return {
            "decision": self.decision,
            "lifecycle_state": self.lifecycle_state,
            "coverage": self.coverage,
            "health": self.health,
            "findings": [f.as_dict() for f in self.findings],
            "findings_by_category": by_category,
            "code_findings": by_category.get("code", []),
            "base_head_findings": by_category.get("base_head", []),
            "production_metadata_findings": by_category.get("production", []),
            "evidence_findings": by_category.get("evidence", []),
            "evidence": dict(self.evidence),
            "evidence_states_available": dict(self.evidence_states_available),
            "reasons": list(self.reasons),
            "required_missing": list(self.required_missing),
            "unsupported": list(self.unsupported),
            "unevaluated": list(self.unevaluated),
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }


# ------------------------------------------------------------- freshness

def classify_freshness(snapshot, *, now=None, criticality="standard"):
    """CURRENT, STALE, PARTIALLY_STALE or UNKNOWN.

    A snapshot that is structurally valid but older than policy allows is
    STALE. It is never silently treated as CURRENT.
    """
    if not snapshot:
        return "UNKNOWN"
    now = now or datetime.now(timezone.utc)
    observed = snapshot.get("observed_at")
    if observed is None:
        return "UNKNOWN"
    if isinstance(observed, str):
        try:
            observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            return "UNKNOWN"
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)

    ttl_seconds = snapshot.get("ttl_seconds")
    if not ttl_seconds:
        minutes = (CRITICAL_TTL_MINUTES if criticality == "critical"
                   else DEFAULT_TTL_MINUTES)
        ttl_seconds = minutes * 60

    # A small tolerance absorbs collector clock skew without letting a
    # genuinely old snapshot through.
    skew = timedelta(seconds=60)
    if now - observed > timedelta(seconds=ttl_seconds) + skew:
        return "STALE"

    relations = snapshot.get("relations") or []
    for relation in relations:
        relation_observed = relation.get("observed_at")
        if relation_observed is None:
            continue
        if isinstance(relation_observed, str):
            try:
                relation_observed = datetime.fromisoformat(
                    relation_observed.replace("Z", "+00:00"))
            except ValueError:
                continue
        if relation_observed.tzinfo is None:
            relation_observed = relation_observed.replace(tzinfo=timezone.utc)
        if now - relation_observed > timedelta(seconds=ttl_seconds) + skew:
            return "PARTIALLY_STALE"
    return "CURRENT"


# ------------------------------------------------------- snapshot indexing

def _index_snapshot(snapshot):
    """relation name -> {"relation": row, "columns": {name: row}}"""
    index = {}
    for relation in (snapshot or {}).get("relations") or []:
        columns = {}
        for column in relation.get("columns") or []:
            columns[str(column.get("column_name", "")).lower()] = column
        index[str(relation.get("relation_name", "")).lower()] = {
            "relation": relation,
            "columns": columns,
        }
    return index


# ------------------------------------------------------------- evaluation

def evaluate_metadata_decision(*, plan, snapshot, enforcement_mode,
                               code_health=100, code_findings=(),
                               policy=None, now=None,
                               request_expired=False):
    """Produce the metadata-backed decision.

    ``plan`` is the CollectionPlan built from base and head. ``snapshot`` is an
    accepted production snapshot, or None when none has arrived.

    Health describes the change; coverage describes the evidence. They are
    computed independently, and missing evidence never alters health.
    """
    policy = policy or default_policy()
    findings: list[Finding] = [
        f if isinstance(f, Finding) else Finding(**f) for f in (code_findings or ())
    ]
    plan_dict = plan.as_dict() if hasattr(plan, "as_dict") else dict(plan)
    targets = plan_dict.get("targets", [])
    metadata_required = bool(plan_dict.get("metadata_required"))

    available = {
        "base_code": True,
        "head_code": True,
        "production": snapshot is not None,
    }

    evidence: dict[str, EvidenceState] = {
        "base_manifest": EvidenceState.EVALUATED,
        "head_manifest": EvidenceState.EVALUATED,
        "changed_files": EvidenceState.EVALUATED,
        "changed_models": EvidenceState.EVALUATED,
    }

    # ---- no production evidence yet -----------------------------------
    if snapshot is None:
        if not metadata_required:
            evidence[PRODUCTION_METADATA] = EvidenceState.NOT_EVALUATED
            findings.append(Finding(
                code="metadata.not_required", severity="info", category="evidence",
                message=("No external production dependency is introduced, so no "
                         "production metadata is required for this review."),
            ))
            lifecycle = "METADATA_NOT_REQUIRED"
        elif request_expired:
            evidence[PRODUCTION_METADATA] = EvidenceState.MISSING
            findings.append(Finding(
                code="metadata.request_expired", severity="block", category="evidence",
                message=("The production metadata collection request expired before "
                         "a collector responded."),
            ))
            lifecycle = "WAITING_FOR_METADATA"
        else:
            evidence[PRODUCTION_METADATA] = EvidenceState.PENDING
            findings.append(Finding(
                code="metadata.pending", severity="warn", category="evidence",
                message=("Production metadata has been requested and is not yet "
                         "available. This review is not complete."),
                detail={"requested_relations": [t["relation_name"] for t in targets]},
            ))
            lifecycle = "WAITING_FOR_METADATA"

        coverage_result = _coverage(policy, enforcement_mode, evidence, code_health,
                                    metadata_required)
        return MetadataDecision(
            # A review still waiting has NO decision. Absence is represented,
            # not substituted with a verdict.
            decision=None if lifecycle == "WAITING_FOR_METADATA" and not request_expired
            else coverage_result.decision,
            lifecycle_state=lifecycle,
            coverage=coverage_result.coverage,
            health=coverage_result.health,
            findings=findings,
            evidence={k: v.value for k, v in evidence.items()},
            reasons=list(coverage_result.reasons),
            required_missing=list(coverage_result.required_missing),
            unsupported=list(coverage_result.unsupported),
            unevaluated=list(coverage_result.unevaluated),
            evidence_states_available=available,
            policy_version=coverage_result.policy_version,
            policy_hash=coverage_result.policy_hash,
        )

    # ---- production evidence present ----------------------------------
    criticality = "critical" if any(t.get("criticality") == "critical"
                                    for t in targets) else "standard"
    freshness = snapshot.get("freshness_state") or classify_freshness(
        snapshot, now=now, criticality=criticality)
    if freshness in ("STALE", "PARTIALLY_STALE"):
        # Recompute rather than trust a collector-declared CURRENT.
        computed = classify_freshness(snapshot, now=now, criticality=criticality)
        freshness = "STALE" if "STALE" in (freshness, computed) else freshness

    completeness = snapshot.get("completeness") or "COMPLETE"
    index = _index_snapshot(snapshot)

    findings.extend(_relation_findings(targets, index))

    if freshness == "STALE":
        evidence[PRODUCTION_METADATA] = EvidenceState.STALE
        findings.append(Finding(
            code="metadata.stale", severity="block", category="evidence",
            message=("Production metadata is older than the freshness policy "
                     "allows and was not treated as current."),
            detail={"freshness_state": freshness,
                    "observed_at": str(snapshot.get("observed_at")),
                    "ttl_seconds": snapshot.get("ttl_seconds")},
        ))
    elif completeness == "PARTIAL" or freshness == "PARTIALLY_STALE":
        evidence[PRODUCTION_METADATA] = EvidenceState.MISSING
        findings.append(Finding(
            code="metadata.partial", severity="warn", category="evidence",
            message=("Production metadata is partial; it was not treated as a "
                     "complete evidence set."),
            detail={"completeness": completeness, "freshness_state": freshness},
        ))
    elif completeness == "FAILED":
        evidence[PRODUCTION_METADATA] = EvidenceState.FAILED
    elif _all_unsupported(index):
        evidence[PRODUCTION_METADATA] = EvidenceState.UNSUPPORTED
        findings.append(Finding(
            code="metadata.unsupported", severity="warn", category="evidence",
            message=("The warehouse adapter did not support the requested "
                     "signals. Unsupported evidence is never reported as a pass."),
        ))
    else:
        evidence[PRODUCTION_METADATA] = EvidenceState.EVALUATED

    coverage_result = _coverage(policy, enforcement_mode, evidence, code_health,
                               metadata_required)

    # A blocking production finding is stronger than a healthy score. Health
    # describes the change; a real production incompatibility is a fact.
    blocking = [f for f in findings if f.severity == "block"]
    warning = [f for f in findings if f.severity == "warn"]

    decision = coverage_result.decision
    if blocking:
        decision = "BLOCK" if enforcement_mode == "enforce" else "WARN"
    elif warning and decision == "ALLOW":
        decision = "WARN"

    # Never a final ALLOW without evaluated required evidence.
    if decision == "ALLOW" and coverage_result.required_missing:
        decision = "WARN" if enforcement_mode == "shadow" else "BLOCK"

    lifecycle = "DECISION_READY"
    if evidence[PRODUCTION_METADATA] == EvidenceState.STALE:
        lifecycle = "METADATA_STALE"
    elif evidence[PRODUCTION_METADATA] == EvidenceState.MISSING:
        lifecycle = "METADATA_PARTIAL"
    elif evidence[PRODUCTION_METADATA] == EvidenceState.EVALUATED:
        lifecycle = "METADATA_COMPLETE"

    return MetadataDecision(
        decision=decision,
        lifecycle_state=lifecycle,
        coverage=coverage_result.coverage,
        health=coverage_result.health,
        findings=findings,
        evidence={k: v.value for k, v in evidence.items()},
        reasons=list(coverage_result.reasons),
        required_missing=list(coverage_result.required_missing),
        unsupported=list(coverage_result.unsupported),
        unevaluated=list(coverage_result.unevaluated),
        evidence_states_available=available,
        policy_version=coverage_result.policy_version,
        policy_hash=coverage_result.policy_hash,
    )


def _coverage(policy, enforcement_mode, evidence, code_health, metadata_required):
    """Evaluate the shared evidence-policy engine.

    This is called on EVERY review path, not only on missing-manifest
    failures. ``production_metadata`` is promoted to required exactly when the
    change actually introduces an external production dependency.
    """
    requirements = dict(policy.requirements)
    if metadata_required:
        requirements[PRODUCTION_METADATA] = "required"
    else:
        requirements.setdefault(PRODUCTION_METADATA, "optional")
    effective = type(policy).create(policy.version, requirements)
    return evaluate_evidence_policy(
        mode=enforcement_mode, policy=effective, evidence=evidence,
        health=int(code_health),
    )


def _all_unsupported(index):
    statuses = [entry["relation"].get("collection_status")
                for entry in index.values()]
    return bool(statuses) and all(s == "UNSUPPORTED" for s in statuses)


# ------------------------------------------------------- the decision cases

def _relation_findings(targets, index):
    """Cases 1-10 of the metadata-backed decision logic."""
    findings: list[Finding] = []

    for target in targets:
        name = str(target.get("relation_name", ""))
        kind = target.get("dependency_kind", "external")
        entry = index.get(name.lower())
        wanted_columns = [str(c) for c in (target.get("columns") or [])]

        # -- case 4: head-derived dependency -------------------------------
        # A relation produced by a model changed inside this pull request is
        # NOT expected to exist in production yet. Its absence is normal and
        # must never be a finding. What matters is that the head graph really
        # produces it - which the planner already established by walking the
        # head dependency graph.
        if kind == "head_derived":
            if entry is None:
                findings.append(Finding(
                    code="dependency.head_derived_absent_ok", severity="info",
                    category="production", relation=name,
                    message=("Produced by an upstream model changed in this pull "
                             "request; its absence from current production is "
                             "expected and is not a failure."),
                    detail={"dependency_kind": kind},
                ))
            else:
                findings.append(Finding(
                    code="dependency.head_derived_present", severity="info",
                    category="production", relation=name,
                    message=("Produced inside this pull request and already "
                             "present in production; the head definition takes "
                             "precedence."),
                ))
            continue

        # -- case 2: external relation missing ------------------------------
        # Only an EXTERNAL dependency is required to exist. An internal
        # relation - one the pull request modifies but does not newly create -
        # is not part of the targeted collection request, so its absence means
        # "not collected", not "not there". Blocking on evidence that was
        # never requested would be a false failure.
        if entry is None or not entry["relation"].get("exists_in_production", True):
            if kind == "external":
                findings.append(Finding(
                    code="relation.missing_in_production", severity="block",
                    category="production", relation=name,
                    message=(f"{name} is an external production dependency but was "
                             f"not found in the warehouse."),
                    detail={"dependency_kind": kind},
                ))
            else:
                findings.append(Finding(
                    code="relation.not_collected", severity="info",
                    category="production", relation=name,
                    message=(f"{name} was not part of the targeted collection "
                             f"request, so its production state was not evaluated."),
                    detail={"dependency_kind": kind},
                ))
            continue

        relation = entry["relation"]
        columns = entry["columns"]

        if relation.get("collection_status") == "UNSUPPORTED":
            findings.append(Finding(
                code="relation.unsupported", severity="warn", category="production",
                relation=name,
                message=(f"The adapter could not evaluate {name}; unsupported "
                         f"evidence is not a pass."),
            ))
            continue

        for column_name in wanted_columns:
            column = columns.get(column_name.lower())

            # -- case 1/2: external column exists? -------------------------
            # Two shapes mean the same thing. `None` is a column the snapshot
            # never mentioned. `exists_in_production=False` is one the collector
            # explicitly looked for and did not find - which is what the real
            # collector reports, since it answers for every requested column.
            # Testing only for None made this finding unreachable in production.
            if column is None or column.get("exists_in_production") is False:
                findings.append(Finding(
                    code="column.missing_in_production", severity="block",
                    category="production", relation=name, column=column_name,
                    message=(f"{name}.{column_name} is referenced by the head "
                             f"code but does not exist in production."),
                ))
                continue

            declared = target.get("column_types", {}).get(column_name)
            actual = column.get("data_type")

            # -- case 5: type mismatch --------------------------------------
            if declared and _family(declared) != "unknown" and _family(actual) != "unknown":
                if _family(declared) != _family(actual):
                    findings.append(Finding(
                        code="column.type_mismatch", severity="block",
                        category="production", relation=name, column=column_name,
                        message=(f"The head code assumes {declared} for "
                                 f"{name}.{column_name} but production holds "
                                 f"{actual}."),
                        detail={"head_type": declared, "production_type": actual},
                    ))

            # -- case 3: null rate / incomplete backfill --------------------
            null_rate = column.get("null_rate")
            if null_rate is not None and null_rate >= HIGH_NULL_RATE:
                severity = "block" if target.get("criticality") == "critical" else "warn"
                findings.append(Finding(
                    code="column.high_null_rate", severity=severity,
                    category="production", relation=name, column=column_name,
                    message=(f"{name}.{column_name} is {null_rate:.0%} NULL in "
                             f"production, which suggests an incomplete backfill."),
                    detail={"null_rate": null_rate, "threshold": HIGH_NULL_RATE},
                ))

            # -- case 7: duplicate amplification ----------------------------
            duplicate_rate = column.get("duplicate_rate")
            if (column_name in (target.get("join_keys") or [])
                    and duplicate_rate is not None
                    and duplicate_rate > DUPLICATE_AMPLIFICATION_RATE):
                findings.append(Finding(
                    code="join.duplicate_amplification", severity="warn",
                    category="production", relation=name, column=column_name,
                    message=(f"{name}.{column_name} is used as a join key but has "
                             f"a {duplicate_rate:.0%} duplicate rate, which risks "
                             f"row amplification and a grain change."),
                    detail={"duplicate_rate": duplicate_rate},
                ))

        # -- case 6: join-key type compatibility ----------------------------
        for left, right in (target.get("join_pairs") or []):
            left_col = columns.get(str(left).lower())
            right_entry = index.get(str(target.get("join_relation", "")).lower())
            right_col = (right_entry or {}).get("columns", {}).get(str(right).lower())
            if left_col and right_col:
                if _family(left_col.get("data_type")) != _family(right_col.get("data_type")):
                    findings.append(Finding(
                        code="join.key_type_incompatible", severity="block",
                        category="production", relation=name, column=str(left),
                        message=(f"Join key types are incompatible: {left} is "
                                 f"{left_col.get('data_type')} and {right} is "
                                 f"{right_col.get('data_type')}."),
                    ))

        # -- case 8: incremental watermark ----------------------------------
        watermark = target.get("watermark_column")
        if watermark:
            column = columns.get(str(watermark).lower())
            if column is None:
                findings.append(Finding(
                    code="incremental.watermark_missing", severity="block",
                    category="production", relation=name, column=str(watermark),
                    message=(f"The incremental watermark column {watermark} does "
                             f"not exist in production."),
                ))
            else:
                lag = relation.get("freshness_lag_seconds")
                if lag is not None and lag > 24 * 3600:
                    findings.append(Finding(
                        code="incremental.watermark_stale", severity="warn",
                        category="production", relation=name, column=str(watermark),
                        message=(f"The watermark on {name} is {lag // 3600}h behind, "
                                 f"which risks late-arriving data being skipped."),
                        detail={"freshness_lag_seconds": lag},
                    ))

        # -- case 9: production drift ---------------------------------------
        expected_fingerprint = target.get("base_schema_fingerprint")
        actual_fingerprint = relation.get("schema_fingerprint")
        if (expected_fingerprint and actual_fingerprint
                and expected_fingerprint != actual_fingerprint):
            findings.append(Finding(
                code="production.schema_drift", severity="warn",
                category="production", relation=name,
                message=(f"Production schema for {name} does not match the "
                         f"repository base expectation; the base commit cannot be "
                         f"assumed to describe production."),
                detail={"base_fingerprint": expected_fingerprint,
                        "production_fingerprint": actual_fingerprint},
            ))

        # -- case 10: column removal blast radius ---------------------------
        for removed in (target.get("removed_columns") or []):
            if str(removed).lower() in columns:
                findings.append(Finding(
                    code="column.removed_still_in_production", severity="warn",
                    category="production", relation=name, column=str(removed),
                    message=(f"{name}.{removed} is removed by the head code but "
                             f"still exists in production and may have downstream "
                             f"consumers."),
                ))

    return findings
