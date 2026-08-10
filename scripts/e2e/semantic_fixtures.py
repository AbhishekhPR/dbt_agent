"""Genuine SQL mutations of the real fixture project, for the semantic E2E.

Pure text transforms over the fixture repository's own files. No manifest is
hand-authored and no SemanticDiff is constructed here — dbt parses whatever
these produce, and the AST engine decides what changed. That separation is
the point: if the harness could describe the expected evidence, the E2E would
be asserting its own opinion rather than the product's.

Two fixtures:

  BLOCK  removes the real refund dependency from fct_orders. The refunds
         relation is used in three places, so a valid HEAD has to edit all
         three — the refund_amount projection, the net_order_amount
         subtraction, and the currency_conversion argument feeding
         net_order_amount_usd — before the LEFT JOIN can go.

  ALLOW  changes a join condition in an unrelated model. It produces real
         AST evidence and trips no existing policy, which is what proves
         semantic evidence is not automatically a finding.

Every mutation asserts it actually changed the file. A fixture that silently
no-ops would produce an empty diff and look like a passing E2E.
"""
from __future__ import annotations

FACT_PATH = "models/marts/finance/fct_orders.sql"

#: The only models each fixture edits. The E2E asserts presence rather than an
#: exact change count, so it needs a way to tell "an extra truthful
#: observation" from "evidence about something we never touched". Any change
#: attributed outside these names is unexplained and fails the run.
BLOCK_MUTATED_MODELS = ("fct_orders",)
ALLOW_MUTATED_MODELS = ("int_customer_orders",)

#: The exact fragments the BLOCK mutation removes, each verified present
#: before removal so a fixture drift fails loudly instead of quietly
#: producing a smaller diff.
_REFUND_PROJECTION = (
    "    coalesce(refunds.refund_amount, 0.0) as refund_amount,\n"
)
_NET_WITH_REFUNDS = (
    "    coalesce(items.gross_order_amount, 0.0) "
    "- coalesce(refunds.refund_amount, 0.0) as net_order_amount,"
)
_NET_GROSS_ONLY = (
    "    coalesce(items.gross_order_amount, 0.0) as net_order_amount,"
)
_USD_WITH_REFUNDS = (
    "'coalesce(items.gross_order_amount, 0.0) "
    "- coalesce(refunds.refund_amount, 0.0)'"
)
_USD_GROSS_ONLY = "'coalesce(items.gross_order_amount, 0.0)'"
_REFUND_JOIN = (
    "left join {{ ref('int_order_refunds') }} as refunds\n"
    "    on orders.order_id = refunds.order_id\n"
)


class FixtureMutationError(RuntimeError):
    """The fixture project is not the shape this mutation was written for."""


def _replace_once(text: str, needle: str, replacement: str, label: str) -> str:
    occurrences = text.count(needle)
    if occurrences != 1:
        raise FixtureMutationError(
            f"expected exactly one {label} in {FACT_PATH}, found {occurrences}")
    return text.replace(needle, replacement)


def block_fixture_files(main_files: dict[str, str]) -> dict[str, str]:
    """HEAD for the refund fixture: remove the int_order_refunds dependency.

    Returns a full file map. The base is the fixture repository's own main,
    unmodified apart from relium.yml, so the comparison is against real
    project SQL rather than against something this harness invented.
    """
    if FACT_PATH not in main_files:
        raise FixtureMutationError(f"{FACT_PATH} is missing from the fixture project")

    sql = main_files[FACT_PATH]
    # Order matters only for readability; each is asserted unique.
    sql = _replace_once(sql, _REFUND_PROJECTION, "", "refund_amount projection")
    sql = _replace_once(sql, _NET_WITH_REFUNDS, _NET_GROSS_ONLY,
                        "net_order_amount refund subtraction")
    sql = _replace_once(sql, _USD_WITH_REFUNDS, _USD_GROSS_ONLY,
                        "currency_conversion refund argument")
    sql = _replace_once(sql, _REFUND_JOIN, "", "int_order_refunds join")

    if "refunds." in sql or "int_order_refunds" in sql:
        raise FixtureMutationError(
            "fct_orders still references the refunds relation after mutation")

    files = dict(main_files)
    files[FACT_PATH] = sql
    return files


#: The ALLOW fixture. `int_customer_orders` is not part of any refund
#: expression, so changing how it joins produces real AST evidence without
#: touching the condition the refund policy looks for.
ALLOW_PATH = "models/intermediate/int_customer_orders.sql"


def allow_fixture_files(main_files: dict[str, str], *, marker: str) -> dict[str, str]:
    """HEAD for the ALLOW fixture: a real but policy-irrelevant SQL change.

    Adds a predicate to an existing WHERE clause, or introduces one. Either
    way the engine emits `filter_changed`, which no existing policy consumes,
    so the review should stay ALLOW while What Changed still has something
    true to show.
    """
    if ALLOW_PATH not in main_files:
        raise FixtureMutationError(f"{ALLOW_PATH} is missing from the fixture project")

    sql = main_files[ALLOW_PATH]
    mutated = _append_predicate(sql, marker)
    if mutated == sql:
        raise FixtureMutationError(
            f"{ALLOW_PATH} was not changed; the fixture would prove nothing")

    files = dict(main_files)
    files[ALLOW_PATH] = mutated
    return files


def _append_predicate(sql: str, marker: str) -> str:
    """Add a predicate on a column the upstream relation genuinely has.

    `status` comes from stg_orders, which this model already reads and
    already filters on inside its CASE expressions, so the predicate is valid
    SQL rather than a reference to a column that does not exist. A
    `where 1 = 1` guard is deliberately avoided: a parser may fold it away and
    the fixture would prove nothing.

    The WHERE clause must precede GROUP BY, so it is inserted rather than
    appended.
    """
    predicate = f"status is not null /* relium semantic e2e {marker} */"
    lowered = sql.lower()
    if "\nwhere " in lowered:
        index = lowered.index("\nwhere ")
        end = sql.find("\n", index + 1)
        end = len(sql) if end == -1 else end
        return sql[:end] + f"\n  and {predicate}" + sql[end:]
    if "\ngroup by " in lowered:
        index = lowered.index("\ngroup by ")
        return sql[:index] + f"\nwhere {predicate}" + sql[index:]
    return sql.rstrip() + f"\nwhere {predicate}\n"


def relium_config() -> str:
    """Enforcement on, so the refund policy can actually block."""
    return "enabled: true\nenforcement_mode: enforce\n"
