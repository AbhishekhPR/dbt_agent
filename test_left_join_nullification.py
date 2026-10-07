"""LEFT JOINs turned into INNER JOINs by a NULL-rejecting WHERE (Postgres).

The positive cases are the shapes a reader tried against the public CLI and
got ALLOW for: a LEFT JOIN straight to `{{ ref() }}`, and a filter applied in
a CTE after the join. The negative cases are the patterns that look similar
but keep the LEFT JOIN's NULL rows, and must never be reported.
"""

import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from agent.ast_analyzer import run_ast_analysis
from agent.cli import cli
from agent.deployment_review_service import lifecycle_code_findings, review_manifest_change
from agent.left_join_nullification import (
    STATUS_AST,
    STATUS_NO_SQL,
    STATUS_REGEX_FALLBACK,
    find_left_join_nullifications,
)
from agent.pr_guard import run_pr_guard


# -- the reader's repro cases ------------------------------------------------

REF_JOIN = """select
    o.order_id,
    o.customer_id,
    p.amount
from {{ ref('stg_orders') }} o
left join {{ ref('stg_payments') }} p
    on o.order_id = p.order_id
where p.status = 'x'
"""

PLAIN_TABLES = """select
    orders.order_id,
    payments.amount
from orders
left join payments
    on orders.order_id = payments.order_id
where payments.status = 'x'
"""

CTE_AS_ALIAS = """with orders as (
    select order_id, customer_id from {{ ref('stg_orders') }}
),
payments as (
    select order_id, amount, status from {{ ref('stg_payments') }}
)
select
    orders_t.order_id,
    pay.amount
from orders as orders_t
left join payments as pay
    on orders_t.order_id = pay.order_id
where pay.status = 'x'
"""

LATER_CTE = """with orders as (
    select order_id, customer_id from {{ ref('stg_orders') }}
),
payments as (
    select order_id, amount, status as payment_status from {{ ref('stg_payments') }}
),
joined as (
    select
        o.order_id,
        o.customer_id,
        p.amount,
        p.payment_status
    from orders o
    left join payments p
        on o.order_id = p.order_id
)
select order_id, customer_id, amount
from joined
where payment_status = 'x'
"""

LATER_CTE_QUALIFIED = """with joined as (
    select
        o.order_id,
        p.amount,
        p.status
    from {{ ref('stg_orders') }} o
    left join {{ ref('stg_payments') }} p
        on o.order_id = p.order_id
)
select j.order_id, j.amount
from joined j
where j.status = 'x'
"""

OTHER_ALIASES = """with ord as (
    select order_id, customer_id from {{ ref('stg_orders') }} where customer_id is not null
),
pmt as (
    select order_id, amount, status from {{ ref('stg_payments') }}
)
select
    ord.order_id,
    pmt.amount
from ord
left join pmt on ord.order_id = pmt.order_id
where pmt.status = 'x'
"""

REPRO_CASES = {
    "a1_ref_join": (REF_JOIN, "stg_payments"),
    "a2_plain_tables": (PLAIN_TABLES, "payments"),
    "a3_cte_as_alias": (CTE_AS_ALIAS, "payments"),
    "b1_filter_in_later_cte": (LATER_CTE, "payments"),
    "b2_filter_in_later_cte_qualified": (LATER_CTE_QUALIFIED, "stg_payments"),
    "b3_alias_ord_pmt": (OTHER_ALIASES, "pmt"),
}

_JOIN = """select o.order_id, p.amount
from {{ ref('stg_orders') }} o
left join {{ ref('stg_payments') }} p
    on o.order_id = p.order_id
"""

NULL_REJECTING = {
    "is_not_null": _JOIN + "where p.amount is not null",
    "greater_than": _JOIN + "where p.amount > 0",
    "not_equal": _JOIN + "where p.status <> 'refunded'",
    "in_list": _JOIN + "where p.status in ('paid', 'settled')",
    "like": _JOIN + "where p.status like 'paid%'",
    "function_of_column": _JOIN + "where lower(p.status) = 'paid'",
    "and_with_left_filter": _JOIN + "where o.customer_id = 1 and p.amount > 0",
}

MUST_NOT_FLAG = {
    "filter_in_on_clause": """select o.order_id, p.amount
from {{ ref('stg_orders') }} o
left join {{ ref('stg_payments') }} p
    on o.order_id = p.order_id
   and p.status = 'paid'
""",
    "anti_join_is_null": _JOIN + "where p.order_id is null",
    "coalesce": _JOIN + "where coalesce(p.status, 'unpaid') = 'paid'",
    "or_is_null": _JOIN + "where p.status = 'paid' or p.status is null",
    "left_side_filter": _JOIN + "where o.customer_id = 1",
    "is_distinct_from": _JOIN + "where p.status is distinct from 'refunded'",
    "coalesced_in_cte_then_filtered": """with joined as (
    select o.order_id, coalesce(p.status, 'unpaid') as status
    from {{ ref('stg_orders') }} o
    left join {{ ref('stg_payments') }} p on o.order_id = p.order_id
)
select order_id from joined where status = 'paid'
""",
    "inner_join": """select o.order_id, p.amount
from {{ ref('stg_orders') }} o
join {{ ref('stg_payments') }} p on o.order_id = p.order_id
where p.status = 'paid'
""",
}

# relium-saas-demo PR #1, models/intermediate/int_subscription_revenue.sql.
DEMO_BASE = """with subscriptions as (

    select *
    from {{ ref('stg_subscriptions') }}

),

payments as (

    select *
    from {{ ref('stg_payments') }}

)

select
    s.subscription_id,
    s.customer_id,
    s.status as subscription_status,
    s.billing_interval,
    s.amount_cents,
    p.payment_status,
    p.paid_at,

    case
        when s.billing_interval = 'year'
            then s.amount_cents / 12.0
        else s.amount_cents
    end as monthly_recurring_revenue_cents

from subscriptions s

left join payments p
    on s.subscription_id = p.subscription_id
"""
DEMO_HEAD = DEMO_BASE + "\nwhere p.payment_status = 'succeeded'\n"


def _rules(report):
    return [bug["rule"] for bug in report["bugs"]]


class DetectorTests(unittest.TestCase):
    def test_every_repro_case_is_flagged_with_its_right_side_relation(self):
        for name, (sql, relation) in REPRO_CASES.items():
            with self.subTest(name):
                findings, status = find_left_join_nullifications(sql)
                self.assertEqual(status, STATUS_AST)
                self.assertEqual([f["relation"] for f in findings], [relation])

    def test_null_rejecting_predicates_are_flagged(self):
        for name, sql in NULL_REJECTING.items():
            with self.subTest(name):
                findings, _ = find_left_join_nullifications(sql)
                self.assertEqual(len(findings), 1)

    def test_patterns_that_keep_null_rows_are_not_flagged(self):
        for name, sql in MUST_NOT_FLAG.items():
            with self.subTest(name):
                findings, status = find_left_join_nullifications(sql)
                self.assertEqual(status, STATUS_AST)
                self.assertEqual(findings, [])

    def test_downstream_filter_names_both_the_join_and_the_filter(self):
        (finding,), _ = find_left_join_nullifications(LATER_CTE)
        self.assertEqual(finding["join_scope"], "CTE `joined`")
        self.assertEqual(finding["filter_scope"], "the final SELECT")
        self.assertEqual(finding["predicate"], "payment_status = 'x'")

    def test_is_not_null_evidence_reads_as_written(self):
        (finding,), _ = find_left_join_nullifications(NULL_REJECTING["is_not_null"])
        self.assertIn("p.amount IS NOT NULL", finding["evidence"])

    def test_a_filter_repeated_downstream_is_reported_once(self):
        sql = """with joined as (
    select o.order_id, p.status
    from orders o left join payments p on o.order_id = p.order_id
    where p.status = 'paid'
)
select order_id from joined where status = 'paid'
"""
        findings, _ = find_left_join_nullifications(sql)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["filter_scope"], "CTE `joined`")

    def test_demo_pr_head_is_flagged_and_base_is_not(self):
        head, _ = find_left_join_nullifications(DEMO_HEAD)
        base, _ = find_left_join_nullifications(DEMO_BASE)
        self.assertEqual([f["predicate"] for f in head], ["p.payment_status = 'succeeded'"])
        self.assertEqual(base, [])

    def test_unparseable_sql_falls_back_to_the_heuristic(self):
        findings, status = find_left_join_nullifications(PLAIN_TABLES + " and (((")
        self.assertEqual(status, STATUS_REGEX_FALLBACK)
        self.assertEqual([f["relation"] for f in findings], ["payments"])

    def test_macro_only_model_has_nothing_to_check(self):
        findings, status = find_left_join_nullifications(
            "{{ dbt_utils.union_relations(relations=[ref('a'), ref('b')]) }}")
        self.assertEqual((findings, status), ([], STATUS_NO_SQL))

    def test_recursive_cte_does_not_loop(self):
        findings, status = find_left_join_nullifications(
            "with recursive t(n) as (select 1 union all select n + 1 from t where n < 5) "
            "select n from t")
        self.assertEqual((findings, status), ([], STATUS_AST))


class AstAnalysisTests(unittest.TestCase):
    def test_repro_cases_report_one_high_severity_finding(self):
        for name, (sql, _relation) in REPRO_CASES.items():
            with self.subTest(name):
                report = run_ast_analysis(sql, name, dialect="postgres")
                self.assertEqual(_rules(report).count("LEFT_JOIN_NULLIFIED"), 1)
                self.assertEqual(report["overall_risk"], "high")

    def test_negative_cases_report_no_left_join_finding(self):
        for name, sql in MUST_NOT_FLAG.items():
            with self.subTest(name):
                report = run_ast_analysis(sql, name, dialect="postgres")
                self.assertNotIn("LEFT_JOIN_NULLIFIED", _rules(report))


class PrGuardEndToEndTests(unittest.TestCase):
    """`relium pr_guard` over a dbt project, as a reader would run it."""

    def _project(self, models):
        root = Path(tempfile.mkdtemp())
        (root / "dbt_project.yml").write_text(
            "name: repro\nversion: '1.0'\nprofile: repro\n", encoding="utf-8")
        for name, sql in models.items():
            path = root / "models" / f"{name}.sql"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sql, encoding="utf-8")
        return root

    def _guard(self, root, name):
        return run_pr_guard(
            str(root),
            changed_files=[f"models/{name}.sql"],
            output=str(root / ".relium" / "report.md"),
            github_comment=True,
            comment_output=str(root / ".relium" / "comment.md"),
        )

    def test_every_repro_case_blocks(self):
        root = self._project({name: sql for name, (sql, _) in REPRO_CASES.items()})
        for name in REPRO_CASES:
            with self.subTest(name):
                result = self._guard(root, name)
                self.assertEqual(result["decision"], "BLOCK")
                self.assertIn("LEFT JOIN possibly nullified by WHERE",
                              (root / ".relium" / "comment.md").read_text(encoding="utf-8"))

    def test_negative_cases_allow(self):
        root = self._project(MUST_NOT_FLAG)
        for name in MUST_NOT_FLAG:
            with self.subTest(name):
                result = self._guard(root, name)
                self.assertEqual(result["decision"], "ALLOW")

    def test_demo_pr_head_blocks_and_base_only_warns(self):
        name = "int_subscription_revenue"
        head = self._guard(self._project({name: DEMO_HEAD}), name)
        base = self._guard(self._project({name: DEMO_BASE}), name)
        self.assertEqual(head["decision"], "BLOCK")
        self.assertEqual(base["decision"], "WARN")  # SELECT * in the CTEs only

    def test_cli_fails_the_ref_join_case(self):
        root = self._project({"a1_ref_join": REF_JOIN})
        result = CliRunner().invoke(cli, [
            "pr_guard", "--project", str(root),
            "--changed-files", "models/a1_ref_join.sql",
            "--output", str(root / "report.md"),
        ])
        self.assertEqual(result.exit_code, 1, result.output)
        report = (root / "report.md").read_text(encoding="utf-8")
        self.assertIn("[HIGH] LEFT JOIN possibly nullified by WHERE", report)


class HostedReviewTests(unittest.TestCase):
    """The GitHub App and dashboard review manifest raw_code the same way."""

    def _review(self, raw_code, compiled_code=None):
        model = {
            "resource_type": "model",
            "name": "fct_payments",
            "unique_id": "model.analytics.fct_payments",
            "original_file_path": "models/marts/fct_payments.sql",
            "columns": {"order_id": {"name": "order_id"}},
            "raw_code": raw_code,
        }
        if compiled_code is not None:
            model["compiled_code"] = compiled_code
        manifest = {
            "metadata": {"project_name": "analytics", "dbt_version": "1.8.0"},
            "nodes": {model["unique_id"]: model},
        }
        return review_manifest_change(
            manifest=manifest,
            changed_files=[model["original_file_path"]],
            deployment_id="deploy-1",
        )

    def test_hosted_review_flags_the_same_cases_as_the_cli(self):
        for name in ("a1_ref_join", "b1_filter_in_later_cte", "b2_filter_in_later_cte_qualified"):
            sql, _relation = REPRO_CASES[name]
            with self.subTest(name):
                result = self._review(sql)
                rules = [f["rule"] for f in result["material_findings"]]
                self.assertIn("LEFT_JOIN_NULLIFIED", rules)
                self.assertEqual(result["decision"], "BLOCK")
                self.assertEqual(result["incident"]["severity"], "HIGH")

    def test_hosted_left_join_finding_is_blocking_in_the_lifecycle(self):
        result = self._review(REF_JOIN)
        finding = next(f for f in lifecycle_code_findings(result)
                       if f["code"] == "LEFT_JOIN_NULLIFIED")
        self.assertEqual(finding["severity"], "block")
        self.assertEqual(finding["detail"]["source_severity"], "high")

    def test_hosted_negative_case_is_not_flagged(self):
        result = self._review(MUST_NOT_FLAG["anti_join_is_null"])
        self.assertNotIn("LEFT_JOIN_NULLIFIED",
                         json.dumps(result["material_findings"]))


class EvidenceRenderingTests(unittest.TestCase):
    """Each LEFT_JOIN_NULLIFIED finding names its table and filter, on one line."""

    PREDICATE = "`WHERE p.payment_status = 'succeeded'`"

    def _evidence_lines(self, text):
        return [line for line in text.splitlines() if "Evidence:" in line]

    def _pr_guard(self, sql):
        root = Path(tempfile.mkdtemp())
        (root / "models").mkdir()
        (root / "models" / "m.sql").write_text(sql, encoding="utf-8")
        result = run_pr_guard(
            str(root), changed_files=["models/m.sql"],
            output=str(root / "report.md"), github_comment=True,
            comment_output=str(root / "comment.md"))
        return (result,
                (root / "report.md").read_text(encoding="utf-8"),
                (root / "comment.md").read_text(encoding="utf-8"))

    def _hosted(self, sql):
        model = {
            "resource_type": "model", "name": "m", "unique_id": "model.a.m",
            "original_file_path": "models/m.sql", "columns": {}, "raw_code": sql,
        }
        return review_manifest_change(
            manifest={"metadata": {}, "nodes": {model["unique_id"]: model}},
            changed_files=["models/m.sql"], deployment_id="deploy-1")

    def test_pr_guard_report_and_comment_show_one_evidence_line(self):
        _result, report, comment = self._pr_guard(DEMO_HEAD)
        for text in (report, comment):
            (line,) = self._evidence_lines(text)
            self.assertIn("LEFT JOIN to payments (alias p)", line)
            self.assertIn(self.PREDICATE, line)

    def test_other_findings_get_no_evidence_line(self):
        # DEMO_BASE has only SELECT * findings.
        _result, report, comment = self._pr_guard(DEMO_BASE)
        self.assertEqual(self._evidence_lines(report), [])
        self.assertEqual(self._evidence_lines(comment), [])

    def test_downstream_filter_evidence_names_the_cte(self):
        _result, report, _comment = self._pr_guard(LATER_CTE)
        (line,) = self._evidence_lines(report)
        self.assertIn("in CTE `joined`", line)
        self.assertIn("`WHERE payment_status = 'x'`", line)

    def test_github_app_comment_shows_the_evidence_on_both_paths(self):
        from agent.github_app.review_comment import render_review_comment
        from agent.metadata_evidence.publication_reconcile import build_review_result

        direct = self._hosted(DEMO_HEAD)
        attempt = {
            "attempt": 1, "decision": "WARN", "evidence_coverage": "COMPLETE",
            "health": 65, "enforcement_mode": "shadow",
            "payload": {"findings": lifecycle_code_findings(direct)},
        }
        review = {"review_id": "r", "enforcement_mode": "shadow",
                  "payload": {"plan": {"changed_models": ["m"]}}}
        reconciled = build_review_result(review, attempt)
        for name, result in (("direct", direct), ("reconcile", reconciled)):
            with self.subTest(name):
                (line,) = [l for l in render_review_comment(result).splitlines()
                           if l.startswith("**Evidence:**")]
                self.assertIn(self.PREDICATE, line)

    def test_long_evidence_stays_on_one_bounded_line(self):
        from agent.ast_analyzer import evidence_line

        bug = {"rule": "LEFT_JOIN_NULLIFIED",
               "line_reference": "LEFT JOIN to p\nfiltered by " + "x" * 1000}
        line = evidence_line(bug)
        self.assertNotIn("\n", line)
        self.assertLessEqual(len(line), 300)
        self.assertIsNone(evidence_line({"rule": "SELECT_STAR", "line_reference": "x"}))


if __name__ == "__main__":
    unittest.main()
