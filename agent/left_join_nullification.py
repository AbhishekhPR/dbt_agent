"""Find LEFT JOINs that a later WHERE quietly turns into INNER JOINs.

A LEFT JOIN keeps every left-side row and fills the right side with NULL when
nothing matches. A WHERE that rejects NULL on a right-side column
(`p.status = 'paid'`, `p.amount > 0`, `p.id IS NOT NULL`, ...) then drops
exactly those rows, so the query behaves as an INNER JOIN while reading as a
LEFT JOIN.

The check works on the parsed query (sqlglot, Postgres dialect), after dbt
Jinja is normalised by `agent.jinja_sql` so `{{ ref('x') }}` and
`{{ source('a', 'x') }}` read as the relation names they stand for. It
follows the right side of each LEFT JOIN through table aliases and through
CTEs and subqueries that select from the joined result, so a filter applied
one or more CTEs later is still attributed to the LEFT JOIN that produced the
NULLs.

Only NULL-rejecting predicates are reported. These are not:

  * a filter in the ON clause (it shapes the match, not the result)
  * `right.col IS NULL` (the anti-join pattern)
  * `COALESCE(right.col, ...) = ...` and other NULL-absorbing expressions
  * `right.col = x OR right.col IS NULL`
  * any filter on a left-side column

A model that cannot be parsed falls back to the previous regex heuristic on
the normalised SQL, so coverage never drops below what it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as exp

from agent.jinja_sql import NO_SQL_TO_ANALYZE, normalise_sql_for_analysis

DIALECT = "postgres"

#: Detection status: the query was parsed and analysed structurally.
STATUS_AST = "ast"
#: The query could not be parsed; the regex heuristic was used instead.
STATUS_REGEX_FALLBACK = "regex_fallback"
#: The model has no SQL of its own (its body is only a macro call).
STATUS_NO_SQL = "no_sql"

# Expressions that can return a non-NULL value from a NULL input, or whose
# value Relium cannot see. A NULL-extended column inside one of these is not
# evidence the row will be dropped.
_NULL_ABSORBING = tuple(
    getattr(exp, name)
    for name in (
        "Coalesce", "Case", "If", "Nvl2", "Greatest", "Least", "Concat",
        "ConcatWs", "AggFunc", "Window", "Anonymous", "Exists", "Subquery",
        "Is", "NullSafeEQ", "NullSafeNEQ", "Connector", "ArrayToString",
    )
    if hasattr(exp, name)
)

_SET_OPERATION = getattr(exp, "SetOperation", exp.Union)


@dataclass(frozen=True)
class _Origin:
    """The right-hand side of one LEFT JOIN: the source of the NULLs."""

    relation: str
    alias: str
    scope: str

    def label(self) -> str:
        if self.alias and self.alias.lower() != self.relation.lower():
            return f"{self.relation} (alias {self.alias})"
        return self.relation


@dataclass
class _Outputs:
    """What a query exposes to the query that selects from it."""

    columns: list[str] | None = None  # None: not knowable (e.g. SELECT * of a table)
    nullable: dict[str, _Origin] = field(default_factory=dict)


@dataclass
class _Source:
    alias: str
    relation: str
    outputs: _Outputs
    null_extended: _Origin | None


@dataclass
class _Scope:
    sources: list[_Source]
    by_alias: dict[str, _Source]
    rejected: dict[_Origin, list[str]]  # origin -> NULL-rejecting predicates


def find_left_join_nullifications(sql: str) -> tuple[list[dict], str]:
    """Return (findings, status) for one model's SQL.

    Each finding names the LEFT JOIN relation whose NULL rows are dropped,
    the predicate that drops them, and where that predicate sits.
    """
    text, status = normalise_sql_for_analysis(sql)
    if status == NO_SQL_TO_ANALYZE:
        return [], STATUS_NO_SQL
    try:
        statements = [s for s in sqlglot.parse(text, read=DIALECT) if s is not None]
    except Exception:
        return _regex_fallback(text), STATUS_REGEX_FALLBACK
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    try:
        for statement in statements:
            for item in _Analyzer(statement).findings():
                key = (item["relation"].lower(), item["alias"].lower())
                if key not in seen:
                    seen.add(key)
                    findings.append(item)
    except Exception:
        # A detector must never fail a review. The heuristic is what ran
        # before this check existed, so it is the floor, not a guess.
        return _regex_fallback(text), STATUS_REGEX_FALLBACK
    return findings, STATUS_AST


class _Analyzer:
    def __init__(self, tree: exp.Expression):
        self.tree = tree
        self.ctes = {
            cte.alias_or_name.lower(): cte.this
            for cte in tree.find_all(exp.CTE)
            if cte.alias_or_name
        }
        self._scopes: dict[int, _Scope] = {}
        self._outputs: dict[int, _Outputs] = {}
        self._in_progress: set[int] = set()

    # -- findings -----------------------------------------------------------

    def findings(self) -> list[dict]:
        results = []
        for select in self.tree.find_all(exp.Select):
            scope = self._scope(select)
            for origin, predicates in scope.rejected.items():
                downstream = origin.scope != _scope_name(select)
                where = "downstream in " + _scope_name(select) if downstream else "in the same query"
                results.append({
                    "alias": origin.alias,
                    "relation": origin.relation,
                    "join_scope": origin.scope,
                    "filter_scope": _scope_name(select),
                    "predicate": " AND ".join(predicates),
                    "evidence": (
                        f"LEFT JOIN to {origin.label()} in {origin.scope} is filtered "
                        f"{where} by WHERE {' AND '.join(predicates)}"
                    ),
                })
        return results

    # -- scopes -------------------------------------------------------------

    def _scope(self, select: exp.Select) -> _Scope:
        key = id(select)
        if key in self._scopes:
            return self._scopes[key]
        sources = []
        from_ = select.args.get("from")
        entries = [(from_.this, None)] if from_ is not None else []
        entries += [(join.this, join) for join in select.args.get("joins") or []]
        for source, join in entries:
            sources.append(self._source(source, join, select))
        by_alias = {s.alias.lower(): s for s in sources if s.alias}
        scope = _Scope(sources=sources, by_alias=by_alias, rejected={})
        self._scopes[key] = scope
        where = select.args.get("where")
        if where is not None:
            for conjunct in _conjuncts(where.this):
                for origin in self._rejecting(conjunct, scope):
                    scope.rejected.setdefault(origin, []).append(_render(conjunct))
        return scope

    def _source(self, source: exp.Expression, join: exp.Join | None, select: exp.Select) -> _Source:
        alias = source.alias_or_name or ""
        outputs = _Outputs()
        relation = alias
        if isinstance(source, exp.Table):
            relation = source.name
            body = self.ctes.get(source.name.lower()) if not source.args.get("db") else None
            if body is not None:
                outputs = self._query_outputs(body)
        elif isinstance(source, exp.Subquery):
            outputs = self._query_outputs(source.this)
        null_extended = None
        if join is not None and str(join.side or "").upper() == "LEFT":
            null_extended = _Origin(relation=relation, alias=alias, scope=_scope_name(select))
        return _Source(alias=alias, relation=relation, outputs=outputs, null_extended=null_extended)

    # -- outputs ------------------------------------------------------------

    def _query_outputs(self, query: exp.Expression) -> _Outputs:
        key = id(query)
        if key in self._outputs:
            return self._outputs[key]
        if key in self._in_progress:  # recursive CTE
            return _Outputs()
        self._in_progress.add(key)
        try:
            if isinstance(query, exp.Subquery):
                result = self._query_outputs(query.this)
            elif isinstance(query, _SET_OPERATION):
                result = self._set_outputs(query)
            elif isinstance(query, exp.Select):
                result = self._select_outputs(query)
            else:
                result = _Outputs()
        finally:
            self._in_progress.discard(key)
        self._outputs[key] = result
        return result

    def _set_outputs(self, query) -> _Outputs:
        left = self._query_outputs(query.left)
        right = self._query_outputs(query.right)
        nullable = dict(left.nullable)
        if left.columns is not None and right.columns is not None:
            for left_name, right_name in zip(left.columns, right.columns):
                if left_name not in nullable and right_name in right.nullable:
                    nullable[left_name] = right.nullable[right_name]
        return _Outputs(columns=left.columns, nullable=nullable)

    def _select_outputs(self, select: exp.Select) -> _Outputs:
        scope = self._scope(select)
        columns: list[str] | None = []
        nullable: dict[str, _Origin] = {}

        def extend(source: _Source | None):
            nonlocal columns
            if source is None or source.outputs.columns is None:
                columns = None
                return
            for name in source.outputs.columns:
                if columns is not None:
                    columns.append(name)
                origin = source.null_extended or source.outputs.nullable.get(name)
                if origin is not None:
                    nullable[name] = origin

        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                for source in scope.sources:
                    extend(source)
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                extend(scope.by_alias.get(projection.table.lower()))
            else:
                name = (projection.alias_or_name or "").lower()
                if columns is not None:
                    columns.append(name)
                value = projection.this if isinstance(projection, exp.Alias) else projection
                origin = self._propagated_origin(value, scope)
                if origin is not None and name:
                    nullable[name] = origin

        # Rows this query's own WHERE already filtered are no longer
        # NULL-extended; the finding belongs to this query, reported once.
        nullable = {k: v for k, v in nullable.items() if v not in scope.rejected}
        return _Outputs(columns=columns, nullable=nullable)

    # -- NULL reasoning -----------------------------------------------------

    def _origin_of(self, column: exp.Column, scope: _Scope) -> _Origin | None:
        name = column.name.lower()
        table = column.table.lower()
        if table:
            source = scope.by_alias.get(table)
            if source is None:
                return None
            return source.null_extended or source.outputs.nullable.get(name)
        # Unqualified: valid SQL resolves it to the one source that has it.
        for source in scope.sources:
            columns = source.outputs.columns
            if source.null_extended and columns is not None and name in columns:
                return source.null_extended
            if name in source.outputs.nullable:
                return source.outputs.nullable[name]
        return None

    def _candidates(self, expression: exp.Expression, scope: _Scope) -> list[_Origin]:
        origins = []
        for column in expression.find_all(exp.Column):
            origin = self._origin_of(column, scope)
            if origin is not None and origin not in origins:
                origins.append(origin)
        return origins

    def _propagated_origin(self, expression: exp.Expression, scope: _Scope) -> _Origin | None:
        for origin in self._candidates(expression, scope):
            if self._is_null_when(expression, origin, scope):
                return origin
        return None

    def _is_null_when(self, expression: exp.Expression, origin: _Origin, scope: _Scope) -> bool:
        """True when `expression` is NULL whenever `origin`'s columns are NULL."""
        if isinstance(expression, exp.Column):
            if isinstance(expression.this, exp.Star):
                return False
            return self._origin_of(expression, scope) == origin
        if isinstance(expression, exp.Paren):
            return self._is_null_when(expression.this, origin, scope)
        if isinstance(expression, _NULL_ABSORBING):
            return False
        if isinstance(expression, (exp.In, exp.Between)):
            return self._is_null_when(expression.this, origin, scope)
        return any(
            self._is_null_when(child, origin, scope)
            for child in expression.iter_expressions()
        )

    def _rejecting(self, condition: exp.Expression, scope: _Scope) -> set[_Origin]:
        """Origins for which `condition` is never TRUE when their columns are NULL."""
        if isinstance(condition, exp.Paren):
            return self._rejecting(condition.this, scope)
        if isinstance(condition, exp.And):
            return self._rejecting(condition.left, scope) | self._rejecting(condition.right, scope)
        if isinstance(condition, exp.Or):
            return self._rejecting(condition.left, scope) & self._rejecting(condition.right, scope)
        if isinstance(condition, exp.Is):
            if isinstance(condition.expression, exp.Null):
                return set()  # IS NULL: the anti-join pattern keeps those rows
            return self._strict_origins(condition.this, scope)  # IS TRUE / IS FALSE
        if isinstance(condition, exp.Not):
            inner = condition.this
            while isinstance(inner, exp.Paren):
                inner = inner.this
            if isinstance(inner, exp.Is):
                if isinstance(inner.expression, exp.Null):
                    return self._strict_origins(inner.this, scope)  # IS NOT NULL
                return set()  # IS NOT TRUE is TRUE for NULL
            return self._strict_origins(inner, scope)
        return self._strict_origins(condition, scope)

    def _strict_origins(self, expression: exp.Expression, scope: _Scope) -> set[_Origin]:
        return {
            origin
            for origin in self._candidates(expression, scope)
            if self._is_null_when(expression, origin, scope)
        }


def _conjuncts(condition: exp.Expression) -> list[exp.Expression]:
    while isinstance(condition, exp.Paren) and isinstance(condition.this, exp.And):
        condition = condition.this
    if isinstance(condition, exp.And):
        return _conjuncts(condition.left) + _conjuncts(condition.right)
    return [condition]


def _render(condition: exp.Expression) -> str:
    """SQL for evidence, written the way an author would write it."""
    if isinstance(condition, exp.Not):
        inner = condition.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return f"{inner.this.sql(dialect=DIALECT)} IS NOT NULL"
    return condition.sql(dialect=DIALECT)


def _scope_name(select: exp.Select) -> str:
    node = select.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            return f"CTE `{node.alias_or_name}`"
        if isinstance(node, exp.Subquery):
            return f"subquery `{node.alias_or_name}`" if node.alias_or_name else "a subquery"
        node = node.parent
    return "the final SELECT"


# -- fallback ---------------------------------------------------------------

_LEGACY_LEFT_JOIN_RE = re.compile(
    r"left\s+(?:outer\s+)?join\s+([\w\.\"]+)(?:\s+(?:as\s+)?([\w_]+))?\s+on\b", re.I)
_LEGACY_WHERE_RE = re.compile(r"where\s+([\s\S]+)$", re.I)


def _regex_fallback(sql: str) -> list[dict]:
    """The pre-AST heuristic, for models the parser cannot read."""
    findings = []
    where = _LEGACY_WHERE_RE.search(sql)
    if not where:
        return findings
    for match in _LEGACY_LEFT_JOIN_RE.finditer(sql):
        relation = match.group(1).split(".")[-1].strip('"')
        alias = match.group(2) or relation
        if alias.lower() in {"on", "where"}:
            alias = relation
        if re.search(rf"\b{re.escape(alias)}\.[\w_]+\b", where.group(1)):
            findings.append({
                "alias": alias,
                "relation": relation,
                "join_scope": "unparsed SQL",
                "filter_scope": "unparsed SQL",
                "predicate": "",
                "evidence": (
                    f"LEFT JOIN to {relation} with a WHERE referencing {alias} "
                    "(model could not be parsed; heuristic match)"
                ),
            })
    return findings
