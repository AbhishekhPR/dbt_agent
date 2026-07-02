from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    id: str
    type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    relationship: str


@dataclass
class SemanticGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def downstream(self, node):
        return self._traverse(node, direction="downstream")

    def upstream(self, node):
        return self._traverse(node, direction="upstream")

    def shortest_path(self, start, end):
        start = str(start)
        end = str(end)
        if start not in self.nodes or end not in self.nodes:
            return []
        if start == end:
            return [start]

        visited = {start}
        queue = deque([(start, [start])])
        while queue:
            current, path = queue.popleft()
            for child in self._children(current):
                if child in visited:
                    continue
                next_path = [*path, child]
                if child == end:
                    return next_path
                visited.add(child)
                queue.append((child, next_path))
        return []

    def affected_nodes(self, changed_nodes):
        affected = []
        for changed_node in sorted(str(node) for node in changed_nodes or []):
            if changed_node not in self.nodes:
                continue
            affected.append(changed_node)
            affected.extend(self.downstream(changed_node))
        return _ordered_unique(affected)

    def _traverse(self, node, *, direction):
        node = str(node)
        if node not in self.nodes:
            return []

        visited = {node}
        results = []
        queue = deque(self._neighbors(node, direction))
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            results.append(current)
            queue.extend(self._neighbors(current, direction))
        return results

    def _neighbors(self, node, direction):
        if direction == "downstream":
            return self._children(node)
        return self._parents(node)

    def _children(self, node):
        return sorted(edge.child for edge in self.edges if edge.parent == node)

    def _parents(self, node):
        return sorted(edge.parent for edge in self.edges if edge.child == node)


def build_semantic_graph(project_context):
    context = dict(project_context or {})
    nodes = {}
    edges = []

    for source in _items(context.get("sources")):
        source_id = _name(source)
        _add_node(nodes, source_id, "source", source)

    for model in _items(context.get("models")):
        model_id = _name(model)
        _add_node(nodes, model_id, "model", model)
        for source_id in _list_field(model, "sources"):
            _add_node(nodes, source_id, "source", {"name": source_id})
            edges.append(Edge(source_id, model_id, "source"))
        for parent in _list_field(model, "refs"):
            _add_node(nodes, parent, "model", {"name": parent})
            edges.append(Edge(parent, model_id, "ref"))

    for semantic_model in _items(context.get("semantic_models")):
        model_id = _name(semantic_model)
        _add_node(nodes, model_id, "model", semantic_model)
        for parent in _list_field(semantic_model, "models"):
            _add_node(nodes, parent, "model", {"name": parent})
            edges.append(Edge(parent, model_id, "semantic_model"))

    for parent, child, relationship in _context_edges(context):
        _add_node(nodes, parent, _infer_type(parent, nodes), {"name": parent})
        _add_node(nodes, child, _infer_type(child, nodes), {"name": child})
        edges.append(Edge(parent, child, relationship))

    for metric in _items(context.get("metrics")):
        metric_id = _name(metric)
        _add_node(nodes, metric_id, "metric", metric)
        for parent in _metric_parents(metric):
            _add_node(nodes, parent, "model", {"name": parent})
            edges.append(Edge(parent, metric_id, "metric"))

    for exposure in _items(context.get("exposures")):
        exposure_id = _name(exposure)
        _add_node(nodes, exposure_id, "metric", exposure)
        for parent in _list_field(exposure, "depends_on"):
            _add_node(nodes, parent, _infer_type(parent, nodes), {"name": parent})
            edges.append(Edge(parent, exposure_id, "exposure"))

    return SemanticGraph(
        nodes={node_id: nodes[node_id] for node_id in sorted(nodes)},
        edges=sorted(set(edges), key=lambda edge: (edge.parent, edge.child, edge.relationship)),
    )


def explain_path(graph, changed_model, metric_name):
    return graph.shortest_path(changed_model, metric_name)


def _items(raw):
    if raw is None:
        return []
    if isinstance(raw, dict):
        if "name" in raw:
            return [raw]
        return [{"name": key, **(value if isinstance(value, dict) else {})} for key, value in raw.items()]
    return list(raw)


def _name(item):
    if isinstance(item, str):
        return item
    return str(item.get("name") or item.get("id"))


def _add_node(nodes, node_id, node_type, metadata):
    if not node_id or node_id == "None":
        return
    if node_id not in nodes:
        nodes[node_id] = Node(node_id, node_type, dict(metadata or {}))


def _list_field(item, field_name):
    if not isinstance(item, dict):
        return []
    value = item.get(field_name) or []
    if isinstance(value, str):
        return [value]
    return list(value)


def _context_edges(context):
    edges = []
    for ref in _items(context.get("refs")):
        if not isinstance(ref, dict):
            continue
        parent = ref.get("parent") or ref.get("source") or ref.get("from")
        child = ref.get("child") or ref.get("model") or ref.get("to")
        if parent and child:
            edges.append((str(parent), str(child), str(ref.get("relationship") or "ref")))
    return edges


def _metric_parents(metric):
    if not isinstance(metric, dict):
        return []
    parent = metric.get("model") or metric.get("parent")
    parents = _list_field(metric, "models") + _list_field(metric, "depends_on")
    if parent:
        parents.append(parent)
    return _ordered_unique(str(parent) for parent in parents)


def _infer_type(node_id, nodes):
    if node_id in nodes:
        return nodes[node_id].type
    return "model"


def _ordered_unique(values):
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique
