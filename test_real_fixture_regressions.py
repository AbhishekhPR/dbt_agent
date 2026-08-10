"""Regressions for defects that only a real dbt project exposed.

Every case here was green in the unit suites while the product was wrong. The
unit fixtures wrote `o.amount - coalesce(r.refund_amount, 0)` and joined
`stg_refunds`; real dbt projects write coalesce-wrapped arithmetic and join
through `{{ ref(...) }}`. The gap between those two spellings hid two
defects that made Relium miss the single case it exists to catch.

These use the smallest manifest that still reproduces the real spelling. They
pin the *existing* policy outcome rather than choosing a new one.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from agent.pr_analysis import _has_refund_adjustment
from agent.sql_semantic_diff import compare_model_sql
from test_sql_semantic_decision import manifest, review

REPO_ROOT = Path(__file__).resolve().parent


def changes(before: str, after: str) -> list[dict]:
    result = compare_model_sql("fct_orders", before, after)
    return list(result.to_dict().get("changes") or [])


def kinds(before: str, after: str) -> list[str]:
    return [change["kind"] for change in changes(before, after)]


# -- 1. dbt ref identity must survive preprocessing ------------------------
#
# `_macro_calls_to_functions` rewrites dbt macros into ordinary function
# calls so strip_jinja cannot delete them. It must never do that to ref or
# source: strip_jinja resolves those into the relation name, and a
# `ref('x')` left in a FROM/JOIN clause parses as an anonymous function
# rather than a relation -- so the join stops existing and a removed
# dependency becomes invisible.

REF_JOIN_BEFORE = """
select
    orders.order_id,
    coalesce(refunds.refund_amount, 0.0) as refund_amount
from {{ ref('stg_orders') }} as orders
left join {{ ref('int_order_refunds') }} as refunds
    on orders.order_id = refunds.order_id
"""

REF_JOIN_AFTER = """
select
    orders.order_id
from {{ ref('stg_orders') }} as orders
"""


class RefIdentitySurvivesPreprocessing(unittest.TestCase):
    def test_a_ref_join_is_identified_by_its_relation_name(self):
        removed = [c for c in changes(REF_JOIN_BEFORE, REF_JOIN_AFTER)
                   if c["kind"] == "join_removed"]
        self.assertEqual([c["relation"] for c in removed], ["int_order_refunds"])

    def test_the_removed_join_keeps_its_type_and_condition(self):
        removed = next(c for c in changes(REF_JOIN_BEFORE, REF_JOIN_AFTER)
                       if c["kind"] == "join_removed")
        self.assertEqual(removed["before_join_type"], "LEFT")
        self.assertIn("refunds.order_id", removed["before_condition_sql"])

    def test_ref_is_not_rewritten_into_a_function_call(self):
        """The defect itself: ref survived as `ref('x')` and the join vanished."""
        from agent.sql_semantic_diff import _macro_calls_to_functions
        for jinja in ("{{ ref('int_order_refunds') }}",
                      "{{ source('raw', 'orders') }}",
                      "{{ ref( 'spaced_out' ) }}"):
            with self.subTest(jinja=jinja):
                self.assertEqual(_macro_calls_to_functions(jinja), jinja)

    def test_a_genuine_macro_is_still_rewritten(self):
        from agent.sql_semantic_diff import _macro_calls_to_functions
        self.assertEqual(
            _macro_calls_to_functions("{{ currency_conversion(a, b) }}"),
            "currency_conversion(a, b)")

    def test_an_unchanged_ref_join_reports_no_change(self):
        self.assertEqual(changes(REF_JOIN_BEFORE, REF_JOIN_BEFORE), [])


# -- 2. the coalesce-wrapped refund shape real projects write --------------
#
# The policy predicate anchored the revenue word immediately left of the `-`,
# so it matched `amount - refund` but not a minuend ending in `0.0)`. The
# refund removal produced ALLOW/100: evidence visible, decision disagreeing.

COALESCE_BEFORE = ("select coalesce(items.gross_order_amount, 0.0) "
                   "- coalesce(refunds.refund_amount, 0.0) as net_order_amount "
                   "from t")
COALESCE_AFTER = ("select coalesce(items.gross_order_amount, 0.0) "
                  "as net_order_amount from t")


class CoalesceWrappedRefundShape(unittest.TestCase):
    def setUp(self):
        self.incident = review(manifest(COALESCE_BEFORE, ["net_order_amount"]),
                               manifest(COALESCE_AFTER, ["net_order_amount"]))["incident"]
        self.comparison = self.incident["metadata"]["manifest_comparison"]

    def test_the_expression_change_is_reported(self):
        changed = [c for c in changes(COALESCE_BEFORE, COALESCE_AFTER)
                   if c["kind"] == "projection_expression_changed"]
        self.assertEqual([c["output_name"] for c in changed], ["net_order_amount"])

    def test_the_predicate_reaches_through_the_wrapping_call(self):
        self.assertTrue(_has_refund_adjustment(
            "COALESCE(items.gross_order_amount, 0.0) "
            "- COALESCE(refunds.refund_amount, 0.0)"))
        self.assertFalse(_has_refund_adjustment(
            "COALESCE(items.gross_order_amount, 0.0)"))

    def test_the_material_refund_signal_fires(self):
        self.assertTrue(self.comparison["material_sql_changes"])

    def test_the_decision_is_block_at_health_65(self):
        self.assertEqual(self.incident["decision"], "BLOCK")
        self.assertEqual(self.incident["health"], 65)

    def test_a_subtraction_without_a_revenue_minuend_is_not_the_signal(self):
        self.assertFalse(_has_refund_adjustment(
            "widget_count - coalesce(refunds.refund_amount, 0.0)"))

    def test_an_unrelated_subtraction_is_not_the_signal(self):
        self.assertFalse(_has_refund_adjustment(
            "coalesce(total_amount, 0.0) - coalesce(discount_amount, 0.0)"))


# -- 3. semantic evidence is not automatically a finding -------------------
#
# The verified ALLOW fixture adds a predicate to an unrelated model. It must
# produce real evidence and trip nothing.

FILTER_BEFORE = "select customer_id, status from t group by customer_id, status"
FILTER_AFTER = ("select customer_id, status from t where status is not null "
                "group by customer_id, status")


class UnrelatedEvidenceStaysPolicyNeutral(unittest.TestCase):
    def setUp(self):
        self.incident = review(manifest(FILTER_BEFORE, ["customer_id", "status"]),
                               manifest(FILTER_AFTER, ["customer_id", "status"]))["incident"]
        self.comparison = self.incident["metadata"]["manifest_comparison"]

    def test_semantic_evidence_is_produced(self):
        self.assertIn("filter_changed", kinds(FILTER_BEFORE, FILTER_AFTER))

    def test_no_material_sql_change_is_claimed(self):
        self.assertEqual(self.comparison["material_sql_changes"], [])

    def test_the_decision_stays_allow_at_full_health(self):
        self.assertEqual(self.incident["decision"], "ALLOW")
        self.assertEqual(self.incident["health"], 100)


# -- 4. no accidental control characters in source -------------------------
#
# The ref defect was a literal 0x08 byte written into a regex where `\b` was
# intended, so `(?!ref\x08|source\x08)` could never match and the negative
# lookahead always succeeded. It was invisible in every editor and every
# diff. This is cheap enough to run on every suite.

_ALLOWED_CONTROLS = frozenset({0x09, 0x0A, 0x0D})


class SourceCarriesNoStrayControlCharacters(unittest.TestCase):
    def _tracked_text_files(self):
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT,
            capture_output=True, check=True).stdout
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            path = REPO_ROOT / entry.decode()
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                continue
            try:
                yield entry.decode(), raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

    def test_no_c0_control_bytes_outside_tab_newline_return(self):
        offenders = []
        for name, text in self._tracked_text_files():
            for lineno, line in enumerate(text.split("\n"), 1):
                for char in line:
                    point = ord(char)
                    if (point < 0x20 and point not in _ALLOWED_CONTROLS) or point == 0x7F:
                        offenders.append(f"{name}:{lineno} contains 0x{point:02X}")
        self.assertEqual(offenders, [], "stray control characters in tracked source")


if __name__ == "__main__":
    unittest.main()
