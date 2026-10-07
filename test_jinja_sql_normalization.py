"""Jinja-safe SQL normalization, one test per real failure shape.

SHAPES holds a minimal model for every Jinja construct that made Spellbook's
dex project unreadable (1795 models: 45 parsed, 278 TokenError, 491
ParseError before this change). They are written here rather than copied,
because Spellbook is BSL-licensed; each one is reduced to the construct it
is about, and these tests pin how each one is normalised.

Ported from 9f43bc3 for the LEFT JOIN check only: `run_ast_analysis` still
uses `strip_jinja` for its other detectors, so the corpus and finding tests
that depend on that switch are not ported.
"""

import unittest

import sqlglot
from sqlglot import expressions as exp

from agent.jinja_sql import (
    NO_SQL_TO_ANALYZE,
    contains_placeholder,
    normalise,
    normalise_sql_for_analysis,
)

SHAPES = {
    # {{ config(post_hook='{{ macro() }}') }}: the inner }} ended the outer
    # expression and stranded a quote -> TokenError.
    "nested_config_post_hook": """{{ config(
    alias = 'trades'
    , post_hook='{{ hide_models() }}'
)}}

select block_time, amount from {{ ref('base_trades') }}
""",
    # Jinja interpolated inside a string literal.
    "jinja_inside_string_literal": """select *
from {{ ref('trades') }}
where block_time >= timestamp '{{ var("start_date") }}'
""",
    # A macro that generates a whole query, used as a CTE body:
    # "Required keyword: 'this' missing for CTE".
    "macro_as_cte_body": """with dexs as (
    {{ uniswap_compatible_trades('ethereum', 'uniswap', '3') }}
)
select blockchain, project from dexs
""",
    # {% for %} with a trailing UNION ALL for every iteration but the last.
    "for_loop_union_fragments": """select * from (
{% for chain in chains %}
    select '{{ chain }}' as blockchain, amount from {{ ref('trades') }}
    {% if not loop.last %} union all {% endif %}
{% endfor %}
) t
""",
    # {% if %}/{% else %}: keeping both branches juxtaposes two alternatives.
    "if_else_branches": """select block_time, amount
from {{ ref('trades') }}
where 1 = 1
{% if is_incremental() %}
  and {{ incremental_predicate('block_time') }}
{% else %}
  and block_time >= timestamp '2024-01-01'
{% endif %}
""",
    # A macro in the select list: strip_jinja left ", as net_amount".
    "macro_in_select_list": """select
    block_time
    , {{ currency_conversion('amount', 'usd') }} as net_amount
from {{ ref('trades') }}
""",
    # A model whose whole body is one macro call: nothing of the author's to read.
    "macro_only_model": """{{ config(alias = 'trades') }}

{{ uniswap_compatible_trades('ethereum', 'uniswap', '2') }}
""",
    # An apostrophe inside a comment must not open a string literal.
    "apostrophe_in_comment": """-- don't include refunds here
select amount from {{ ref('trades') }}
""",
    # {% set %} / {% do %} tags carry no SQL.
    "set_and_do_tags": """{% set chains = ['ethereum', 'base'] %}
{% do log('building trades') %}
select amount from {{ ref('trades') }}
""",
    # A bare Jinja variable used as a value.
    "bare_variable_expression": """select amount * {{ conversion_rate }} as amount_usd
from {{ ref('trades') }}
""",
}


def _parse(sql):
    text, status = normalise_sql_for_analysis(sql)
    if status == NO_SQL_TO_ANALYZE:
        return None, status
    return sqlglot.parse_one(text), None


class StructurePreservedTests(unittest.TestCase):
    def test_config_header_is_removed_but_the_query_survives(self):
        tree, _ = _parse(SHAPES["nested_config_post_hook"])
        self.assertEqual({c.alias_or_name for c in tree.find(exp.Select).expressions},
                         {"block_time", "amount"})
        self.assertEqual(tree.find(exp.Table).name, "base_trades")

    def test_string_literal_stays_a_string_literal(self):
        tree, _ = _parse(SHAPES["jinja_inside_string_literal"])
        literals = [literal.this for literal in tree.find_all(exp.Literal) if literal.is_string]
        self.assertTrue(any(contains_placeholder(value) for value in literals))
        self.assertIsNotNone(tree.find(exp.Where))

    def test_macro_cte_body_becomes_a_placeholder_query(self):
        tree, _ = _parse(SHAPES["macro_as_cte_body"])
        self.assertEqual([cte.alias for cte in tree.find_all(exp.CTE)], ["dexs"])
        self.assertTrue(contains_placeholder(tree.sql()))

    def test_for_loop_body_is_kept_without_a_dangling_union(self):
        tree, _ = _parse(SHAPES["for_loop_union_fragments"])
        self.assertIsNotNone(tree.find(exp.Select))
        self.assertNotIn("union", tree.sql().lower())

    def test_only_the_first_if_branch_survives(self):
        tree, _ = _parse(SHAPES["if_else_branches"])
        rendered = tree.sql().lower()
        self.assertIn("incremental_predicate", rendered)
        self.assertNotIn("2024-01-01", rendered)

    def test_macro_in_select_list_keeps_its_alias_and_arguments(self):
        tree, _ = _parse(SHAPES["macro_in_select_list"])
        aliases = {projection.alias_or_name for projection in tree.find(exp.Select).expressions}
        self.assertEqual(aliases, {"block_time", "net_amount"})
        rendered = tree.sql().lower()
        self.assertIn("currency_conversion", rendered)  # the macro name stays visible
        self.assertTrue(contains_placeholder(rendered))  # its value does not

    def test_ref_and_source_still_resolve_to_relation_names(self):
        tree, _ = _parse("select 1 from {{ ref('trades') }} "
                         "join {{ source('raw', 'orders') }} on true")
        self.assertEqual({t.name for t in tree.find_all(exp.Table)}, {"trades", "orders"})

    def test_comment_apostrophe_does_not_swallow_the_query(self):
        tree, _ = _parse(SHAPES["apostrophe_in_comment"])
        self.assertEqual(tree.find(exp.Table).name, "trades")

    def test_bare_variable_becomes_a_placeholder_identifier(self):
        tree, _ = _parse(SHAPES["bare_variable_expression"])
        self.assertTrue(contains_placeholder(tree.sql()))
        self.assertEqual(tree.find(exp.Select).expressions[0].alias_or_name, "amount_usd")

    def test_macro_only_model_has_no_sql_to_analyse(self):
        _tree, status = _parse(SHAPES["macro_only_model"])
        self.assertEqual(status, NO_SQL_TO_ANALYZE)


class RepairScopeTests(unittest.TestCase):
    """Repairs target structure a removed Jinja tag leaves, nothing else."""

    def test_author_sql_is_not_deleted_to_make_it_parse(self):
        sql = "select a, b from t where a = 1 and b = 2"
        self.assertEqual(normalise(sql).split(), sql.split())

    def test_unbalanced_sql_without_jinja_is_left_unparseable(self):
        # Not our business to repair: it never parsed and still does not.
        text, _status = normalise_sql_for_analysis("select from where )")
        with self.assertRaises(sqlglot.errors.ParseError):
            sqlglot.parse_one(text)


if __name__ == "__main__":
    unittest.main()
