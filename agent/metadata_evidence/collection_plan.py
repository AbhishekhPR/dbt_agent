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


#: Why a plan does not require production metadata. The distinction is
#: load-bearing: one of these says the CHANGE needs no production evidence,
#: the other says this WORKSPACE cannot supply any. Reporting the second as
#: the first would tell a Free customer that no external production dependency
#: was introduced when one plainly was.
NOT_REQUIRED_NO_EXTERNAL_DEPENDENCY = "no_external_dependency"
NOT_REQUIRED_NOT_ENTITLED = "warehouse_evidence_not_entitled"


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
    #: one. DEPTH-1 ONLY, and deliberately so: this answers "what reads a
    #: changed model", which a grandchild does not. The full closure is
    #: ``downstream_edges``. Neither includes exposures.
    direct_edges: list[dict] = field(default_factory=list)
    #: Every edge of the TRANSITIVE downstream closure, each one a single dbt
    #: ``depends_on`` relationship carrying the depth at which the traversal
    #: reached its target. A grandchild is a real consequence of a change, and
    #: omitting it understated the blast radius; including it is still
    #: evidence rather than inference, because every edge here is one dbt
    #: declared, walked one hop at a time. ``direct_edges`` remains the
    #: depth-1 subset for callers that want only what a changed model is read
    #: by directly.
    downstream_edges: list[dict] = field(default_factory=list)
    #: Direct downstream models only: the bounded set whose production state a
    #: collector is asked to observe. Deliberately narrower than
    #: ``downstream_models`` — describing the blast radius truthfully and
    #: scanning the warehouse for all of it are different decisions, and a
    #: pull request must never trigger an unbounded collection.
    collected_downstream_models: list[str] = field(default_factory=list)
    required_evidence_level: str = "profile"
    metadata_required: bool = True
    #: Set when ``metadata_required`` is False. See the constants above.
    metadata_not_required_reason: str | None = None
    #: Whether this workspace's plan includes warehouse evidence at all.
    warehouse_evidence_entitled: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "targets": [t.as_dict() for t in self.targets],
            "changed_models": list(self.changed_models),
            "added_dependencies": list(self.added_dependencies),
            "removed_dependencies": list(self.removed_dependencies),
            "downstream_models": list(self.downstream_models),
            "direct_edges": [dict(e) for e in self.direct_edges],
            "downstream_edges": [dict(e) for e in self.downstream_edges],
            "collected_downstream_models": list(self.collected_downstream_models),
            "max_downstream_depth": max(
                (int(e.get("depth") or 0) for e in self.downstream_edges),
                default=0),
            "required_evidence_level": self.required_evidence_level,
            "metadata_required": self.metadata_required,
            "metadata_not_required_reason": self.metadata_not_required_reason,
            "warehouse_evidence_entitled": self.warehouse_evidence_entitled,
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
                          evidence_level="profile", critical_models=(),
                          warehouse_evidence_entitled=True) -> CollectionPlan:
    """Build a bounded, targeted collection plan.

    Only relations the review actually needs are requested. A pull request
    never triggers a full warehouse scan.

    ``warehouse_evidence_entitled`` says whether this workspace's plan includes
    warehouse evidence at all. When it does not, the plan still describes every
    target it WOULD have collected — the description is honest analysis and
    costs nothing — but it does not REQUIRE any of it, because requiring
    evidence the workspace cannot legally submit is a wait that can never end.
    It defaults to True so that every existing caller, and every deployment
    with no billing configured, plans exactly as it did before.
    """
    if evidence_level not in EVIDENCE_LEVEL_SIGNALS:
        raise CollectionPlanError(f"unknown evidence level: {evidence_level}")

    head_nodes = _model_nodes(head_manifest)
    base_nodes = _model_nodes(base_manifest)
    head_sources = _source_nodes(head_manifest)
    changed = [m for m in (changed_models or [])]
    critical = set(critical_models or ())

    plan = CollectionPlan(changed_models=list(changed),
                          required_evidence_level=evidence_level,
                          warehouse_evidence_entitled=bool(warehouse_evidence_entitled))

    if not head_nodes:
        plan.metadata_required = False
        plan.metadata_not_required_reason = NOT_REQUIRED_NO_EXTERNAL_DEPENDENCY
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

    # Downstream blast radius.
    #
    # Two different questions, answered separately because they have different
    # right answers:
    #
    #   what is IMPACTED   -> the full transitive downstream closure. A change
    #                         to a staging model reaches the executive report
    #                         three hops away, and a blast radius that stopped
    #                         at the first hop reported one downstream model
    #                         where the project's own lineage has three.
    #   what is COLLECTED  -> the DIRECT downstream only. Collection is a
    #                         bounded request against a customer warehouse, and
    #                         a pull request must never trigger a scan that
    #                         grows with the depth of the project.
    #
    # Neither is inference. Every edge below is one relationship dbt itself
    # declared in ``depends_on``, walked a single hop at a time.
    direct_downstream, direct_edges = _direct_downstream(head_nodes, changed_ids)
    downstream, downstream_edges = _transitive_downstream(
        head_nodes, changed_ids, direct_edges)

    plan.downstream_models = sorted(downstream)
    plan.collected_downstream_models = sorted(direct_downstream)
    plan.direct_edges = sorted(
        direct_edges,
        key=lambda e: (e["source_model_unique_id"], e["target_model_unique_id"]))
    plan.downstream_edges = sorted(
        downstream_edges,
        key=lambda e: (e["depth"], e["source_model_unique_id"],
                       e["target_model_unique_id"]))

    for node_id in sorted(direct_downstream):
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

    has_external = any(t.dependency_kind == "external" for t in plan.targets)
    if not has_external:
        plan.metadata_required = False
        plan.metadata_not_required_reason = NOT_REQUIRED_NO_EXTERNAL_DEPENDENCY
        plan.notes.append(
            "every dependency is produced inside this pull request; no external "
            "production evidence is required")
    elif not warehouse_evidence_entitled:
        # ###############################################################
        # # A PLAN THAT CANNOT SUPPLY EVIDENCE MUST NOT REQUIRE IT.     #
        # ###############################################################
        #
        # The targets stay in the plan: they are a true description of the
        # production state this change depends on, and saying so is part of
        # the analysis. What changes is that the review does not wait on
        # them, because the endpoint that would accept the snapshot refuses
        # this workspace outright. The targets are not silently dropped and
        # the limitation is not silently hidden - see the finding
        # ``metadata.not_entitled``, which names the capability.
        plan.metadata_required = False
        plan.metadata_not_required_reason = NOT_REQUIRED_NOT_ENTITLED
        plan.notes.append(
            "production warehouse evidence is not included on this workspace's "
            "plan; the external production state described by these targets was "
            "not collected and was not evaluated")
    else:
        plan.metadata_required = True
    return plan


def _direct_downstream(head_nodes, changed_ids):
    """Models that read a changed model, and the edge that proves each one.

    The intersection below names WHICH changed models each downstream node
    reads. Recording only the target would throw that away and leave the graph
    unrecoverable whenever more than one model changed.

    A node that is ITSELF changed is not blast radius: it is head-derived
    state this pull request proposes, and the review reasons about it directly.
    """
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
    return downstream, edges


def _transitive_downstream(head_nodes, changed_ids, direct_edges):
    """The full downstream closure, breadth-first, with the depth of each edge.

    Breadth-first so ``depth`` is the SHORTEST path from any changed model,
    which is the depth a reader means when they ask how far a change reaches.
    A node is expanded once, so a diamond in the graph yields every real edge
    into its join point but never revisits it, and a cycle - which dbt does not
    permit but a hand-built manifest can still contain - terminates.
    """
    children: dict[str, list[str]] = {}
    for node_id, node in head_nodes.items():
        for parent in _depends_on(node):
            children.setdefault(parent, []).append(node_id)

    edges = [dict(edge, depth=1) for edge in direct_edges]
    downstream = {edge["target_model_unique_id"] for edge in direct_edges}
    frontier = sorted(downstream)
    depth = 1
    expanded = set(changed_ids)
    while frontier:
        depth += 1
        next_frontier = []
        for source_id in frontier:
            if source_id in expanded:
                continue
            expanded.add(source_id)
            for target_id in sorted(set(children.get(source_id, ()))):
                if target_id in changed_ids or target_id not in head_nodes:
                    continue
                edges.append({"source_model_unique_id": source_id,
                              "target_model_unique_id": target_id,
                              "depth": depth})
                if target_id not in downstream:
                    downstream.add(target_id)
                    next_frontier.append(target_id)
        frontier = sorted(next_frontier)
    return downstream, edges


def ttl_minutes_for(criticality: str) -> int:
    return CRITICAL_TTL_MINUTES if criticality == "critical" else DEFAULT_TTL_MINUTES
