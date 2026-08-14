"""Targeted production-metadata collection planning.

Given the immutable base manifest (state 1) and the immutable head manifest
(state 2), work out the smallest set of production relations whose actual state
(state 3) the review genuinely needs, and why.

The central distinction this module exists to make:

  * an **external** dependency must already exist in production. If the head
    code reads ``orders.discount_amount`` and production has no such column,
    that is a real finding.

  * a **head-derived** dependency is produced by another model *inside this
    same pull request*. Its absence from current production is expected and
    must never be reported as a failure. What must be verified instead is that
    the head graph really does produce it, in the right order, from sources
    that production evidence supports.

Getting that distinction wrong in either direction is a product defect: block
every PR that adds a column, or wave through a PR that reads a column nobody
produces.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

# Signals requested per evidence level. Higher levels are supersets, so a
# review never asks for profiling work it will not use.
EVIDENCE_LEVEL_SIGNALS = {
    "schema": ("relation_exists", "column_exists", "data_type", "is_nullable",
               "schema_fingerprint"),
    "profile": ("relation_exists", "column_exists", "data_type", "is_nullable",
                "schema_fingerprint", "row_count", "null_rate", "freshness"),
    "full": ("relation_exists", "column_exists", "data_type", "is_nullable",
             "schema_fingerprint", "row_count", "null_rate", "freshness",
             "duplicate_rate", "distinct_count", "cardinality", "min_max"),
}

# Freshness policy. A structurally valid snapshot older than this is STALE,
# not CURRENT.
DEFAULT_TTL_MINUTES = 60
CRITICAL_TTL_MINUTES = 15


class CollectionPlanError(ValueError):
    """Raised when a plan cannot be built from the supplied artifacts."""


@dataclass(frozen=True)
class Target:
    relation_name: str
    dependency_kind: str            # external | head_derived | internal
    columns: tuple[str, ...] = ()
    required_signals: tuple[str, ...] = ()
    model_unique_id: str | None = None
    relation_database: str | None = None
    relation_schema: str | None = None
    criticality: str = "standard"
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "relation_name": self.relation_name,
            "dependency_kind": self.dependency_kind,
            "columns": list(self.columns),
            "required_signals": list(self.required_signals),
            "model_unique_id": self.model_unique_id,
            "relation_database": self.relation_database,
            "relation_schema": self.relation_schema,
            "criticality": self.criticality,
            "reason": self.reason,
        }


@dataclass
class CollectionPlan:
    targets: list[Target] = field(default_factory=list)
    changed_models: list[str] = field(default_factory=list)
    added_dependencies: list[str] = field(default_factory=list)
    removed_dependencies: list[str] = field(default_factory=list)
    downstream_models: list[str] = field(default_factory=list)
    #: Direct source -> target pairs behind ``downstream_models``. The planner
    #: already knows which changed model each downstream node reads; keeping
    #: it means a blast-radius graph is evidence rather than inference. Two
    #: changed models and three downstream models leave the flat lists
    #: ambiguous, and guessing an edge is indistinguishable from fabricating
    #: one. DIRECT edges only: no transitive closure, no exposures.
    direct_edges: list[dict] = field(default_factory=list)
    required_evidence_level: str = "profile"
    metadata_required: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "targets": [t.as_dict() for t in self.targets],
            "changed_models": list(self.changed_models),
            "added_dependencies": list(self.added_dependencies),
            "removed_dependencies": list(self.removed_dependencies),
            "downstream_models": list(self.downstream_models),
            "direct_edges": [dict(e) for e in self.direct_edges],
            "required_evidence_level": self.required_evidence_level,
            "metadata_required": self.metadata_required,
            "notes": list(self.notes),
            "target_count": len(self.targets),
            "external_target_count": sum(
                1 for t in self.targets if t.dependency_kind == "external"),
            "head_derived_target_count": sum(
                1 for t in self.targets if t.dependency_kind == "head_derived"),
        }


# ---------------------------------------------------------------- manifests

def manifest_hash(manifest) -> str | None:
    """Stable content hash of a dbt manifest, used to bind evidence to the
    exact artifact a decision was computed from."""
    if not isinstance(manifest, dict):
        return None
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _model_nodes(manifest) -> dict[str, dict]:
    if not isinstance(manifest, dict):
        return {}
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    return {k: v for k, v in nodes.items()
            if isinstance(v, dict) and v.get("resource_type") == "model"}


def _source_nodes(manifest) -> dict[str, dict]:
    if not isinstance(manifest, dict):
        return {}
    sources = manifest.get("sources")
    return sources if isinstance(sources, dict) else {}


def _relation_of(node: dict) -> str:
    """The PHYSICAL relation a dbt node resolves to.

    dbt names the physical object differently by resource type, and getting
    this wrong sends a collector to a table that does not exist:

      * a source carries ``identifier`` (the real table); ``name`` is only the
        logical name used in ``source()`` calls,
      * a model carries ``alias`` (defaults to the model name),
      * ``schema`` is already the final schema, after any custom-schema macro.

    Reading ``alias or name`` missed ``identifier`` entirely, so every source
    declared with a custom identifier resolved to a non-existent relation -
    which a collector reports as absent from production, which decides BLOCK.
    A false BLOCK on every pull request touching that source.
    """
    schema = node.get("schema")
    name = node.get("identifier") or node.get("alias") or node.get("name")
    return f"{schema}.{name}" if schema else str(name)


def _depends_on(node: dict) -> list[str]:
    depends = node.get("depends_on")
    if not isinstance(depends, dict):
        return []
    return [n for n in (depends.get("nodes") or []) if isinstance(n, str)]


def _columns_of(node: dict) -> list[str]:
    columns = node.get("columns")
    if isinstance(columns, dict):
        return sorted(columns.keys())
    return []


# ---------------------------------------------------------------- planning

def build_collection_plan(*, base_manifest, head_manifest, changed_models,
                          evidence_level="profile", critical_models=()) -> CollectionPlan:
    """Build a bounded, targeted collection plan.

    Only relations the review actually needs are requested. A pull request
    never triggers a full warehouse scan.
    """
    if evidence_level not in EVIDENCE_LEVEL_SIGNALS:
        raise CollectionPlanError(f"unknown evidence level: {evidence_level}")

    head_nodes = _model_nodes(head_manifest)
    base_nodes = _model_nodes(base_manifest)
    head_sources = _source_nodes(head_manifest)
    changed = [m for m in (changed_models or [])]
    critical = set(critical_models or ())

    plan = CollectionPlan(changed_models=list(changed),
                          required_evidence_level=evidence_level)

    if not head_nodes:
        plan.metadata_required = False
        plan.notes.append("head manifest contains no models; no metadata required")
        return plan

    # Resolve changed model names to head node ids.
    def _find(name: str) -> tuple[str | None, dict | None]:
        for node_id, node in head_nodes.items():
            if node_id == name or node.get("name") == name:
                return node_id, node
        return None, None

    changed_ids = set()
    for name in changed:
        node_id, _ = _find(name)
        if node_id:
            changed_ids.add(node_id)

    # Everything the head graph produces. A dependency on any of these is
    # internal to the proposal, not a claim about current production.
    produced_by_head = set(head_nodes)

    # Models changed in THIS pull request produce head-derived state.
    head_derived_ids = set(changed_ids)

    seen: dict[str, Target] = {}

    def _add(target: Target) -> None:
        existing = seen.get(target.relation_name)
        if existing is None:
            seen[target.relation_name] = target
            return
        # Merge columns, and let a stricter dependency kind win.
        merged_columns = tuple(sorted(set(existing.columns) | set(target.columns)))
        kind = existing.dependency_kind
        if existing.dependency_kind == "head_derived" and target.dependency_kind == "external":
            kind = "external"
        seen[target.relation_name] = Target(
            relation_name=existing.relation_name,
            dependency_kind=kind,
            columns=merged_columns,
            required_signals=existing.required_signals,
            model_unique_id=existing.model_unique_id or target.model_unique_id,
            relation_database=existing.relation_database or target.relation_database,
            relation_schema=existing.relation_schema or target.relation_schema,
            criticality=("critical" if "critical" in
                         (existing.criticality, target.criticality) else existing.criticality),
            reason=existing.reason or target.reason,
        )

    signals = EVIDENCE_LEVEL_SIGNALS[evidence_level]

    for node_id in sorted(changed_ids):
        node = head_nodes[node_id]
        base_node = base_nodes.get(node_id)

        base_deps = set(_depends_on(base_node) if base_node else ())
        head_deps = set(_depends_on(node))
        added = head_deps - base_deps
        removed = base_deps - head_deps
        plan.added_dependencies.extend(sorted(added))
        plan.removed_dependencies.extend(sorted(removed))

        criticality = "critical" if node.get("name") in critical else "standard"

        # The changed model's own relation. It exists in production today
        # (unless the PR creates it), so its current state is drift evidence.
        _add(Target(
            relation_name=_relation_of(node),
            dependency_kind="head_derived" if base_node is None else "internal",
            columns=tuple(_columns_of(node)),
            required_signals=signals,
            model_unique_id=node_id,
            relation_database=node.get("database"),
            relation_schema=node.get("schema"),
            criticality=criticality,
            reason=("model is new in this pull request" if base_node is None
                    else "model changed in this pull request"),
        ))

        # Every dependency of the changed model, classified.
        for dep in sorted(head_deps):
            if dep in head_derived_ids:
                # produced by a model changed in THIS pull request
                dep_node = head_nodes.get(dep)
                if dep_node is None:
                    continue
                _add(Target(
                    relation_name=_relation_of(dep_node),
                    dependency_kind="head_derived",
                    columns=tuple(_columns_of(dep_node)),
                    required_signals=signals,
                    model_unique_id=dep,
                    relation_database=dep_node.get("database"),
                    relation_schema=dep_node.get("schema"),
                    criticality=criticality,
                    reason=("produced by an upstream model changed in this pull "
                            "request; absence from current production is expected"),
                ))
            elif dep in produced_by_head:
                dep_node = head_nodes[dep]
                _add(Target(
                    relation_name=_relation_of(dep_node),
                    dependency_kind="external",
                    columns=tuple(_columns_of(dep_node)),
                    required_signals=signals,
                    model_unique_id=dep,
                    relation_database=dep_node.get("database"),
                    relation_schema=dep_node.get("schema"),
                    criticality=criticality,
                    reason="unchanged upstream model; production state is authoritative",
                ))
            elif dep in head_sources:
                source = head_sources[dep]
                _add(Target(
                    relation_name=_relation_of(source),
                    dependency_kind="external",
                    columns=tuple(_columns_of(source)),
                    required_signals=signals,
                    model_unique_id=dep,
                    relation_database=source.get("database"),
                    relation_schema=source.get("schema"),
                    criticality=criticality,
                    reason="external source dependency; must exist in production",
                ))

    # Downstream blast radius: models that read a changed model.
    #
    # The intersection below names WHICH changed models each downstream node
    # reads. Recording only the target would throw that away and leave the
    # graph unrecoverable whenever more than one model changed, so the pairs
    # are kept alongside the flat list the rest of the plan already uses.
    downstream = set()
    edges: list[dict] = []
    for node_id, node in head_nodes.items():
        if node_id in changed_ids:
            continue
        sources = set(_depends_on(node)) & changed_ids
        if not sources:
            continue
        downstream.add(node_id)
        # A node reading two changed models yields two truthful edges, never
        # the cartesian product of changed x downstream.
        for source_id in sources:
            edges.append({
                "source_model_unique_id": source_id,
                "target_model_unique_id": node_id,
            })
    plan.downstream_models = sorted(downstream)
    plan.direct_edges = sorted(
        edges,
        key=lambda e: (e["source_model_unique_id"], e["target_model_unique_id"]))

    for node_id in sorted(downstream):
        node = head_nodes[node_id]
        _add(Target(
            relation_name=_relation_of(node),
            dependency_kind="external",
            columns=tuple(_columns_of(node)),
            required_signals=signals,
            model_unique_id=node_id,
            relation_database=node.get("database"),
            relation_schema=node.get("schema"),
            criticality="standard",
            reason="downstream of a changed model; blast-radius evidence",
        ))

    plan.targets = [seen[k] for k in sorted(seen)]
    plan.added_dependencies = sorted(set(plan.added_dependencies))
    plan.removed_dependencies = sorted(set(plan.removed_dependencies))
    plan.metadata_required = any(t.dependency_kind == "external" for t in plan.targets)

    if not plan.metadata_required:
        plan.notes.append(
            "every dependency is produced inside this pull request; no external "
            "production evidence is required")
    return plan


def ttl_minutes_for(criticality: str) -> int:
    return CRITICAL_TTL_MINUTES if criticality == "critical" else DEFAULT_TTL_MINUTES
