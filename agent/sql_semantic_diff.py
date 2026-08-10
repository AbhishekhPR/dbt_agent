"""Deterministic SQL semantic comparison between a model's base and head SQL.

Why this exists
---------------
`compare_manifest_sql` was never a general SQL comparator. Its own docstring
says so: it is a narrow refund fallback that regex-matches a net/gross minus
refund expression. Two things made it miss the canonical case:

  * the pattern requires an unqualified ``refund*``, so idiomatic
    ``r.refund_amount`` never matched;
  * ``_canonical_sql`` rewrites every ``{{ ref(...) }}`` to the same token
    ``dbt_ref``, which destroys join identity — both sides of a join look
    alike, so a removed join is invisible by construction.

So a change that turns ``gross - refunds`` into ``gross`` produced an empty
diff. That is the single most important pre-deployment case Relium claims to
cover.

What this module does
---------------------
Compares two SQL strings for the same model as ASTs, using the sqlglot already
pinned in requirements and already used by column_lineage and ast_analyzer,
and emits structured evidence — never prose, never impact claims. It answers
"what changed", not "what will happen".

What it deliberately does not do
--------------------------------
It does not decide semantic *equivalence*. Two differently-spelled expressions
that compute the same value are reported as changed. That direction is chosen
on purpose: a false "changed" costs a reviewer a glance, while a false
"unchanged" is a silent miss of exactly the kind above.

Unparseable SQL yields an explicit ``unavailable`` status. It never yields an
empty change list, because "we could not read it" and "we read it and nothing
changed" are different facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from agent.ast_analyzer import strip_jinja

#: The dialect the rest of the repository parses with.
DIALECT = "sqlite"

# Change kinds. Stable strings: they cross into persisted evidence later.
PROJECTION_ADDED = "projection_added"
PROJECTION_REMOVED = "projection_removed"
PROJECTION_EXPRESSION_CHANGED = "projection_expression_changed"
JOIN_ADDED = "join_added"
JOIN_REMOVED = "join_removed"
JOIN_CONDITION_CHANGED = "join_condition_changed"
JOIN_TYPE_CHANGED = "join_type_changed"
FILTER_CHANGED = "filter_changed"
GROUPING_CHANGED = "grouping_changed"

STATUS_EVALUATED = "evaluated"
STATUS_UNAVAILABLE = "unavailable"


@dataclass
class SqlSemanticComparison:
    """One model's base/head SQL comparison."""

    model_name: str
    status: str = STATUS_EVALUATED
    unavailable_reason: str | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def evaluated(self) -> bool:
        return self.status == STATUS_EVALUATED

    def to_dict(self) -> dict[str, Any]:
        document = {
            "model_name": self.model_name,
            "status": self.status,
            "changes": [dict(change) for change in self.changes],
        }
        if self.unavailable_reason:
            document["unavailable_reason"] = self.unavailable_reason
        return document


def compare_model_sql(model_name, before_sql, after_sql, *,
                      model_unique_id=None) -> SqlSemanticComparison:
    """Compare one model's SQL across base and head.

    Returns evaluated evidence, or an explicit unavailable status. Never
    guesses.
    """
    comparison = SqlSemanticComparison(model_name=str(model_name))

    before_tree, before_error = _parse(before_sql)
    after_tree, after_error = _parse(after_sql)
    if before_tree is None or after_tree is None:
        comparison.status = STATUS_UNAVAILABLE
        comparison.unavailable_reason = before_error or after_error or "SQL was not available"
        return comparison

    identity = {"model_name": str(model_name)}
    if model_unique_id:
        identity["model_unique_id"] = str(model_unique_id)

    comparison.changes.extend(_projection_changes(before_tree, after_tree, identity))
    comparison.changes.extend(_join_changes(before_tree, after_tree, identity))
    comparison.changes.extend(_filter_changes(before_tree, after_tree, identity))
    comparison.changes.extend(_grouping_changes(before_tree, after_tree, identity))
    return comparison


# ----------------------------------------------------------------- parsing

def _parse(sql):
    """Parse to an AST, or explain why not. Jinja refs keep their identity."""
    if not isinstance(sql, str) or not sql.strip():
        return None, "SQL was not available for this model"
    # Macros are rewritten BEFORE strip_jinja, which would otherwise delete
    # them; ref and source are left for strip_jinja to resolve to names.
    text = strip_jinja(_macro_calls_to_functions(sql))
    try:
        tree = sqlglot.parse_one(text, dialect=DIALECT)
    except Exception:
        return None, "SQL could not be parsed"
    if tree is None:
        return None, "SQL could not be parsed"
    # sqlglot is lenient and will happily turn nonsense into *some* node. If
    # there is no SELECT to compare, say so: otherwise a model whose SQL could
    # not really be read reports every projection as removed, which looks like
    # evidence and is not.
    if _select(tree) is None:
        return None, "no SELECT statement was found to compare"
    return tree, None


def _select(tree):
    return tree if isinstance(tree, exp.Select) else tree.find(exp.Select)


def _render(node) -> str | None:
    """Canonical SQL text for one node.

    Rendering through sqlglot is what makes whitespace and casing stop being
    differences: two spellings that produce the same AST produce the same
    string here.
    """
    if node is None:
        return None
    try:
        return _fold_identifiers(
            _unwrap_redundant_parens(node.copy())).sql(dialect=DIALECT)
    except Exception:
        return None


def _fold_identifiers(node):
    """Case-fold unquoted identifiers so `A` and `a` render alike.

    String literals are untouched — `'Paid'` and `'paid'` are different
    values, not different spellings — and a quoted identifier keeps its case
    because the warehouse does too.
    """
    for identifier in node.find_all(exp.Identifier):
        if not identifier.quoted and isinstance(identifier.this, str):
            identifier.set("this", identifier.this.lower())
    return node


def _unwrap_redundant_parens(node):
    """Drop parentheses that cannot be carrying meaning.

    Only a paren wrapping an atom or another paren is removed — never one
    wrapping an operation, because ``(a + b) * c`` and ``a + b * c`` are
    different sums and must stay different. Verified by
    ``test_a_real_precedence_change_is_still_reported``.
    """
    # The root has no parent, so `replace` cannot rewrite it in place. Peel it
    # first, then rewrite the descendants.
    while isinstance(node, exp.Paren) and _is_atomic(node.this):
        node = node.this.copy()
    for paren in list(node.find_all(exp.Paren)):
        inner = paren.this
        if _is_atomic(inner):
            paren.replace(inner.copy())
    return node


def _is_atomic(node) -> bool:
    """True when parentheses around this node cannot be carrying precedence."""
    return isinstance(node, (exp.Paren, exp.Column, exp.Literal, exp.Func))


# -------------------------------------------------------------- projections

def _projections(tree) -> dict[str, str]:
    """Output name → rendered expression, for named projections.

    ``SELECT *`` carries no output names, so it is skipped rather than
    guessed at; an unresolved star is not evidence of anything.
    """
    projections = {}
    # CTE bodies first, namespaced by CTE name. A refund subtraction is very
    # often computed in a staging CTE and merely selected through at the top
    # level, so comparing only the outer SELECT would see an unchanged column
    # reference and call a real change nothing.
    for cte in tree.find_all(exp.CTE):
        alias = _text(cte.alias)
        inner_select = cte.this if isinstance(cte.this, exp.Select) else cte.find(exp.Select)
        if not alias or inner_select is None:
            continue
        for name, rendered in _select_projections(inner_select).items():
            projections[f"{alias}.{name}"] = rendered

    select = _select(tree)
    if select is not None:
        projections.update(_select_projections(select))
    return projections


def _select_projections(select) -> dict[str, str]:
    projections = {}
    for expression in select.expressions:
        if isinstance(expression, exp.Star):
            continue
        name = _projection_name(expression)
        if not name:
            continue
        inner = expression.this if isinstance(expression, exp.Alias) else expression
        rendered = _render(inner)
        if rendered is not None:
            projections[name] = rendered
    return projections


def _projection_name(expression) -> str | None:
    """The output name, case-folded when the identifier is unquoted.

    Unquoted identifiers are case-insensitive in every warehouse Relium
    targets, so `A` and `a` are the same column and must not read as one
    output being dropped and another added. A *quoted* identifier is
    case-sensitive and is left exactly as written.
    """
    if isinstance(expression, exp.Alias):
        return _fold(expression.args.get("alias"), _text(expression.alias))
    alias = getattr(expression, "alias", None)
    if alias:
        return _fold(None, _text(alias))
    if isinstance(expression, exp.Column):
        return _fold(expression.this, _text(expression.name))
    return None


def _fold(identifier, text) -> str | None:
    if text is None:
        return None
    if isinstance(identifier, exp.Identifier) and identifier.quoted:
        return text
    return text.lower()


def _projection_changes(before, after, identity) -> list[dict]:
    previous, current = _projections(before), _projections(after)
    changes = []
    for name in sorted(set(previous) | set(current)):
        was, now = previous.get(name), current.get(name)
        if was == now:
            continue
        if was is None:
            changes.append({**identity, "kind": PROJECTION_ADDED,
                            "output_name": name, "before_sql": None,
                            "after_sql": now})
        elif now is None:
            changes.append({**identity, "kind": PROJECTION_REMOVED,
                            "output_name": name, "before_sql": was,
                            "after_sql": None})
        else:
            changes.append({**identity, "kind": PROJECTION_EXPRESSION_CHANGED,
                            "output_name": name, "before_sql": was,
                            "after_sql": now})
    return changes


# -------------------------------------------------------------------- joins

def _joins(tree) -> dict[str, dict]:
    """Joined relation name → join type and rendered condition."""
    select = _select(tree)
    if select is None:
        return {}
    joins = {}
    for join in select.find_all(exp.Join):
        table = join.this.find(exp.Table) if join.this is not None else None
        relation = _text(table.name) if table is not None else None
        if not relation:
            continue
        joins[relation] = {
            "join_type": _join_type(join),
            "condition_sql": _render(join.args.get("on")),
        }
    return joins


def _join_type(join) -> str:
    side = (join.side or "").upper()
    kind = (join.kind or "").upper()
    label = " ".join(part for part in (side, kind) if part)
    return label or "INNER"


def _join_changes(before, after, identity) -> list[dict]:
    previous, current = _joins(before), _joins(after)
    changes = []
    for relation in sorted(set(previous) | set(current)):
        was, now = previous.get(relation), current.get(relation)
        if was is None:
            changes.append({**identity, "kind": JOIN_ADDED, "relation": relation,
                            "before": None,
                            "after_join_type": now["join_type"],
                            "after_condition_sql": now["condition_sql"]})
            continue
        if now is None:
            changes.append({**identity, "kind": JOIN_REMOVED, "relation": relation,
                            "before_join_type": was["join_type"],
                            "before_condition_sql": was["condition_sql"],
                            "after": None})
            continue
        if was["join_type"] != now["join_type"]:
            changes.append({**identity, "kind": JOIN_TYPE_CHANGED, "relation": relation,
                            "before_join_type": was["join_type"],
                            "after_join_type": now["join_type"]})
        if was["condition_sql"] != now["condition_sql"]:
            changes.append({**identity, "kind": JOIN_CONDITION_CHANGED,
                            "relation": relation,
                            "before_sql": was["condition_sql"],
                            "after_sql": now["condition_sql"]})
    return changes


# --------------------------------------------------------- filters, grouping

def _clause(tree, key):
    select = _select(tree)
    if select is None:
        return None
    node = select.args.get(key)
    return _render(node.this if isinstance(node, (exp.Where, exp.Having)) else node)


def _filter_changes(before, after, identity) -> list[dict]:
    changes = []
    for scope, key in (("where", "where"), ("having", "having")):
        was, now = _clause(before, key), _clause(after, key)
        if was == now:
            continue
        changes.append({**identity, "kind": FILTER_CHANGED, "scope": scope,
                        "before_sql": was, "after_sql": now})
    return changes


def _grouping_changes(before, after, identity) -> list[dict]:
    was, now = _group_expressions(before), _group_expressions(after)
    if was == now:
        return []
    return [{**identity, "kind": GROUPING_CHANGED,
             "before_sql": ", ".join(was) or None,
             "after_sql": ", ".join(now) or None}]


def _group_expressions(tree) -> list[str]:
    select = _select(tree)
    if select is None:
        return []
    group = select.args.get("group")
    if group is None:
        return []
    rendered = [_render(expression) for expression in group.expressions]
    return [value for value in rendered if value is not None]


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("`")
    return text or None


#: `{{ some_macro(a, b) }}` left behind by strip_jinja, which resolves ref and
#: source but deletes everything else.
_MACRO_CALL = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\((.*?)\)\s*\}\}", re.S)

#: Resolved by strip_jinja into the relation name. Rewriting one of these as
#: a function call would leave `ref('x')` in the FROM/JOIN clause, where it
#: parses as an anonymous function rather than a relation -- and the join
#: silently vanishes from the comparison.
_JINJA_RESOLVED = frozenset({"ref", "source"})


def _macro_calls_to_functions(sql: str) -> str:
    """Rewrite a dbt macro call as an ordinary SQL function call.

    strip_jinja deletes non-ref jinja outright, which turns
    `{{ currency_conversion(...) }} as net_order_amount_usd` into
    `, as net_order_amount_usd` — unparseable, so the whole model reports
    unavailable and a real change goes unseen.

    Rendering it as `currency_conversion(...)` keeps the statement parseable
    AND keeps the arguments in the tree, so editing what is passed to a macro
    is still visible as a projection change rather than being flattened into
    one opaque token.
    """
    if "{{" not in sql:
        return sql
    return _MACRO_CALL.sub(_rewrite_macro, sql)


def _rewrite_macro(match) -> str:
    """Render one macro call, leaving ref/source for strip_jinja."""
    name = match.group(1)
    if name.lower() in _JINJA_RESOLVED:
        return match.group(0)
    return f"{name}({match.group(2).strip()})"
