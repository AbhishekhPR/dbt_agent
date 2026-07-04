import copy
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

import sqlglot
import sqlglot.expressions as exp


@dataclass
class ColumnReference:
    model_name: str | None
    column_name: str
    relation_alias: str | None = None

    def to_dict(self) -> dict:
        return _serializable(self)

    @classmethod
    def from_dict(cls, payload: dict):
        data = dict(payload or {})
        return cls(
            model_name=data.get("model_name"),
            column_name=str(data.get("column_name") or ""),
            relation_alias=data.get("relation_alias"),
        )


@dataclass
class ColumnLineageEdge:
    from_model: str | None
    from_column: str
    to_model: str
    to_column: str
    confidence: float
    reason: str
    relation_alias: str | None = None

    def to_dict(self) -> dict:
        return _serializable(self)

    @classmethod
    def from_dict(cls, payload: dict):
        data = dict(payload or {})
        return cls(
            from_model=data.get("from_model"),
            from_column=str(data.get("from_column") or ""),
            to_model=str(data.get("to_model") or ""),
            to_column=str(data.get("to_column") or ""),
            confidence=float(data.get("confidence") or 0.0),
            reason=str(data.get("reason") or ""),
            relation_alias=data.get("relation_alias"),
        )


@dataclass
class ModelColumnLineage:
    model_name: str
    output_columns: list[str] = field(default_factory=list)
    edges: list[ColumnLineageEdge] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serializable(self)

    @classmethod
    def from_dict(cls, payload: dict):
        data = dict(payload or {})
        return cls(
            model_name=str(data.get("model_name") or ""),
            output_columns=[str(value) for value in data.get("output_columns") or []],
            edges=[
                ColumnLineageEdge.from_dict(edge)
                for edge in data.get("edges") or []
                if isinstance(edge, dict)
            ],
            unknown_columns=[str(value) for value in data.get("unknown_columns") or []],
            metadata=copy.deepcopy(data.get("metadata") or {}),
        )


@dataclass
class ColumnLineageGraph:
    models: dict[str, ModelColumnLineage] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "models": {
                str(model_name): lineage.to_dict()
                for model_name, lineage in sorted(self.models.items())
            },
            "metadata": _serializable(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict):
        data = dict(payload or {})
        models = {}
        raw_models = data.get("models") or {}
        if isinstance(raw_models, dict):
            for model_name, lineage in raw_models.items():
                if isinstance(lineage, dict):
                    models[str(model_name)] = ModelColumnLineage.from_dict(lineage)
        return cls(
            models=models,
            metadata=copy.deepcopy(data.get("metadata") or {}),
        )


def build_column_lineage_graph(project_context: dict) -> ColumnLineageGraph:
    context = copy.deepcopy(project_context or {})
    models = {}

    for model in _model_items(context.get("models")):
        model_name = str(model.get("name") or "")
        if not model_name:
            continue
        models[model_name] = _lineage_for_model(model_name, model)

    return ColumnLineageGraph(
        models={name: models[name] for name in sorted(models)},
        metadata={
            "model_count": len(models),
            "source": "project_context",
        },
    )


def _lineage_for_model(model_name: str, model: dict) -> ModelColumnLineage:
    output_columns = _ordered_unique(_columns(model.get("columns")))
    sql = _model_sql(model)
    if not sql:
        return ModelColumnLineage(
            model_name=model_name,
            output_columns=output_columns,
            edges=[],
            unknown_columns=list(output_columns),
            metadata={"status": "unknown", "reason": "missing sql"},
        )

    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception as error:
        return ModelColumnLineage(
            model_name=model_name,
            output_columns=output_columns,
            edges=[],
            unknown_columns=list(output_columns),
            metadata={
                "status": "unknown",
                "reason": f"parse failed: {error}",
            },
        )

    select = tree.find(exp.Select)
    if select is None:
        return ModelColumnLineage(
            model_name=model_name,
            output_columns=output_columns,
            edges=[],
            unknown_columns=list(output_columns),
            metadata={"status": "unknown", "reason": "no select"},
        )

    if any(isinstance(expression, exp.Star) for expression in select.expressions):
        return ModelColumnLineage(
            model_name=model_name,
            output_columns=output_columns,
            edges=[],
            unknown_columns=list(output_columns),
            metadata={"status": "unknown", "reason": "select star"},
        )

    alias_map = _relation_aliases(tree)
    edges = []
    known_outputs = []
    for expression in select.expressions:
        to_column = _output_column(expression)
        if not to_column:
            continue
        known_outputs.append(to_column)
        references = _column_references(expression, alias_map)
        for reference in references:
            edges.append(
                ColumnLineageEdge(
                    from_model=reference.model_name,
                    from_column=reference.column_name,
                    to_model=model_name,
                    to_column=to_column,
                    confidence=0.95 if reference.relation_alias else 0.7,
                    reason=(
                        "alias-qualified column reference"
                        if reference.relation_alias
                        else "unqualified column reference"
                    ),
                    relation_alias=reference.relation_alias,
                )
            )

    final_outputs = output_columns or _ordered_unique(known_outputs)
    unknown_columns = [
        column
        for column in final_outputs
        if column not in known_outputs
    ]
    return ModelColumnLineage(
        model_name=model_name,
        output_columns=final_outputs,
        edges=sorted(
            _dedupe_edges(edges),
            key=lambda edge: (
                edge.to_column,
                edge.from_model or "",
                edge.from_column,
                edge.relation_alias or "",
            ),
        ),
        unknown_columns=unknown_columns,
        metadata={
            "status": "parsed" if not unknown_columns else "partial",
            "sql_source": _sql_source(model),
        },
    )


def _model_items(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        if "name" in raw:
            return [copy.deepcopy(raw)]
        return [
            {"name": key, **(copy.deepcopy(value) if isinstance(value, dict) else {})}
            for key, value in raw.items()
        ]
    return [
        copy.deepcopy(item)
        for item in raw
        if isinstance(item, dict)
    ]


def _model_sql(model: dict) -> str:
    for field_name in ("compiled_code", "raw_code", "sql"):
        value = model.get(field_name)
        if value:
            return str(value)
    return ""


def _sql_source(model: dict) -> str | None:
    for field_name in ("compiled_code", "raw_code", "sql"):
        if model.get(field_name):
            return field_name
    return None


def _columns(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        values = []
        for key, value in raw.items():
            if isinstance(value, dict):
                values.append(str(value.get("name") or key))
            else:
                values.append(str(key))
        return values
    if isinstance(raw, (list, tuple, set)):
        return [
            str(item.get("name") if isinstance(item, dict) else item)
            for item in raw
            if item is not None
        ]
    return [str(raw)]


def _relation_aliases(tree) -> dict[str, str]:
    aliases = {}
    for table in tree.find_all(exp.Table):
        table_name = _identifier(table.name)
        alias = _identifier(table.alias_or_name)
        if alias and table_name:
            aliases[alias] = table_name
        if table_name:
            aliases[table_name] = table_name
    return aliases


def _output_column(expression) -> str | None:
    if isinstance(expression, exp.Alias):
        return _identifier(expression.alias)
    alias = getattr(expression, "alias", None)
    if alias:
        return _identifier(alias)
    if isinstance(expression, exp.Column):
        return _identifier(expression.name)
    return None


def _column_references(expression, alias_map: dict[str, str]) -> list[ColumnReference]:
    references = []
    seen = set()
    for column in expression.find_all(exp.Column):
        column_name = _identifier(column.name)
        if not column_name:
            continue
        relation_alias = _identifier(column.table) if column.table else None
        model_name = alias_map.get(relation_alias) if relation_alias else None
        key = (model_name, column_name, relation_alias)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            ColumnReference(
                model_name=model_name,
                column_name=column_name,
                relation_alias=relation_alias,
            )
        )
    return references


def _identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("`").strip("'")
    return text or None


def _dedupe_edges(edges: list[ColumnLineageEdge]) -> list[ColumnLineageEdge]:
    unique = []
    seen = set()
    for edge in edges:
        key = (
            edge.from_model,
            edge.from_column,
            edge.to_model,
            edge.to_column,
            edge.relation_alias,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return unique


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _serializable(value):
    if is_dataclass(value):
        return _serializable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _serializable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value
