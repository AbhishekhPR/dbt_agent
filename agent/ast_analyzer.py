import re
import sqlglot
import sqlglot.expressions as exp
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


# ─────────────────────────────────────────
# JINJA STRIPPER
# ─────────────────────────────────────────
def strip_jinja(sql: str) -> str:
    """
    Removes dbt Jinja templating before AST parsing.
    Replaces {{ ref('model') }} with a plain table name.
    Replaces {{ config(...) }} blocks with nothing.
    """
    sql = re.sub(r'\{\{[^}]*config[^}]*\}\}', '', sql, flags=re.DOTALL)
    sql = re.sub(r"\{\{\s*ref\(['\"](\w+)['\"]\)\s*\}\}", r'\1', sql)
    sql = re.sub(r"\{\{\s*source\(['\"](\w+)['\"],\s*['\"](\w+)['\"]\)\s*\}\}", r'\1__\2', sql)
    sql = re.sub(r'\{\{.*?\}\}', 'placeholder', sql, flags=re.DOTALL)
    sql = re.sub(r'\{#.*?#\}', '', sql, flags=re.DOTALL)
    sql = re.sub(r'\{%.*?%\}', '', sql, flags=re.DOTALL)
    return sql.strip()


# ─────────────────────────────────────────
# PARSER
# ─────────────────────────────────────────
def parse_sql(sql: str, dialect: str = "sqlite") -> exp.Expression | None:
    try:
        clean_sql = strip_jinja(sql)
        return sqlglot.parse_one(clean_sql, dialect=dialect)
    except Exception as e:
        print(f"  ⚠️  AST parse failed: {e}")
        return None


# ─────────────────────────────────────────
# RULE 1: LEFT JOIN nullified by WHERE
# ─────────────────────────────────────────
def check_left_join_nullified(tree: exp.Expression) -> list:
    """
    Finds LEFT JOINs where the right table's column appears in a WHERE clause.
    This silently converts LEFT JOIN to INNER JOIN, dropping unmatched rows.

    Pattern:
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE c.is_deleted = 0   ← kills the LEFT JOIN
    """
    bugs = []
    joins = list(tree.find_all(exp.Join))
    where = tree.find(exp.Where)

    if not where or not joins:
        return bugs

    # Collect aliases from LEFT JOINs only
    left_join_aliases = set()
    for join in joins:
        if join.side and join.side.upper() == "LEFT":
            alias = join.find(exp.TableAlias)
            table = join.find(exp.Table)
            if alias:
                left_join_aliases.add(alias.name.lower())
            elif table:
                left_join_aliases.add(table.name.lower())

    if not left_join_aliases:
        return bugs

    # Check WHERE for right-table column references
    seen = set()
    for condition in where.find_all(exp.EQ, exp.NEQ, exp.Is):
        for col in condition.find_all(exp.Column):
            table_ref = col.table
            if table_ref and table_ref.lower() in left_join_aliases:
                key = str(condition)
                if key in seen:
                    continue
                seen.add(key)
                bugs.append({
                    "rule": "LEFT_JOIN_NULLIFIED",
                    "category": "LEFT JOIN NULLIFIED BY WHERE CLAUSE",
                    "severity": "high",
                    "confidence": "high",
                    "line_reference": f"WHERE {condition}",
                    "description": (
                        f"WHERE clause on right-table column '{col}' converts "
                        f"LEFT JOIN to INNER JOIN silently. "
                        f"Rows with no match in the right table are dropped."
                    ),
                    "impact": (
                        "Unmatched rows silently excluded. "
                        "Metrics like SUM(revenue) or COUNT(customers) will be understated."
                    ),
                    "fix": (
                        f"Move the filter into the JOIN ON clause:\n"
                        f"  LEFT JOIN ... ON ... AND {condition}"
                    )
                })

    return bugs


# ─────────────────────────────────────────
# RULE 2: NULL exclusion via != operator
# ─────────────────────────────────────────
def check_null_exclusion_via_neq(tree: exp.Expression) -> list:
    """
    Finds WHERE col != 'value' patterns.
    SQL treats NULL != 'value' as UNKNOWN, silently excluding NULL rows.
    """
    bugs = []
    where = tree.find(exp.Where)
    if not where:
        return bugs

    seen = set()
    for neq in where.find_all(exp.NEQ):
        left = neq.left
        right = neq.right

        if isinstance(left, exp.Column) and isinstance(right, (exp.Literal, exp.Null)):
            key = str(neq)
            if key in seen:
                    continue
            seen.add(key)
            bugs.append({
                "rule": "NULL_EXCLUSION_NEQ",
                "category": "NULL EXCLUSION VIA != OPERATOR",
                "severity": "medium",
                "confidence": "high",
                "line_reference": f"WHERE {neq}",
                "description": (
                    f"'{left}' != '{right}' silently excludes NULL rows. "
                    f"SQL evaluates NULL != value as UNKNOWN, not TRUE."
                ),
                "impact": (
                    f"All rows where '{left}' IS NULL are silently dropped. "
                    "Especially dangerous for status columns where NULL means pending/unknown."
                ),
                "fix": (
                    f"WHERE {left} != {right} OR {left} IS NULL"
                )
            })

    return bugs


# ─────────────────────────────────────────
# RULE 3: Divide by zero without NULLIF
# ─────────────────────────────────────────
def check_divide_by_zero(tree: exp.Expression) -> list:
    """
    Finds division where denominator is COUNT or SUM with no NULLIF protection.
    Skips if NULLIF already appears anywhere in the full division expression.
    Deduplicates by denominator SQL to prevent double-reporting.
    """
    bugs = []
    flagged = set()

    for div in tree.find_all(exp.Div):
        denom = div.right

        # If NULLIF appears anywhere in the entire division expression, skip
        full_sql = div.sql().upper()
        if "NULLIF" in full_sql:
            continue

        # Only flag COUNT or SUM denominators
        if not isinstance(denom, (exp.Count, exp.Sum)):
            continue

        # Deduplicate by denominator SQL
        denom_key = denom.sql()
        if denom_key in flagged:
            continue
        flagged.add(denom_key)

        bugs.append({
            "rule": "DIVIDE_BY_ZERO",
            "category": "DIVIDE BY ZERO WITHOUT NULLIF",
            "severity": "high",
            "confidence": "medium",
            "line_reference": div.sql(),
            "description": (
                f"Denominator '{denom.sql()}' could be zero with no NULLIF protection. "
                "Returns NULL silently in most warehouses, or crashes in strict mode."
            ),
            "impact": (
                "Averages, rates, or ratios return NULL silently for any group "
                "with zero rows. Looks like missing data, not a bug."
            ),
            "fix": (
                f"Wrap denominator with NULLIF:\n"
                f"  {div.left.sql()} / NULLIF({denom.sql()}, 0)"
            )
        })

    return bugs


# ─────────────────────────────────────────
# RULE 4: SELECT * usage
# ─────────────────────────────────────────
def check_select_star(tree: exp.Expression) -> list:
    """
    Finds SELECT * patterns.
    When upstream adds or reorders columns, SELECT * silently
    changes the shape of your output table.
    """
    bugs = []

    for select in tree.find_all(exp.Select):
        for expr in select.expressions:
            if isinstance(expr, exp.Star):
                bugs.append({
                    "rule": "SELECT_STAR",
                    "category": "SELECT STAR SCHEMA RISK",
                    "severity": "low",
                    "confidence": "high",
                    "line_reference": "SELECT *",
                    "description": (
                        "SELECT * picks up all columns from the source table. "
                        "If upstream adds, removes, or reorders columns, "
                        "the output schema changes silently."
                    ),
                    "impact": (
                        "Downstream models that reference specific column positions "
                        "or expect a fixed schema will break silently or return wrong data."
                    ),
                    "fix": (
                        "Explicitly list the columns you need:\n"
                        "  SELECT col1, col2, col3 FROM ..."
                    )
                })
                break  # One flag per SELECT block is enough

    return bugs


# ─────────────────────────────────────────
# RULE 5: Hardcoded date literals
# ─────────────────────────────────────────
def check_hardcoded_dates(tree: exp.Expression) -> list:
    """
    Finds hardcoded date string literals in WHERE clauses.
    These become stale and silently exclude data over time.
    """
    bugs = []
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    where = tree.find(exp.Where)
    if not where:
        return bugs

    seen = set()
    for literal in where.find_all(exp.Literal):
        val = literal.this
        if isinstance(val, str) and date_pattern.match(val):
            if val in seen:
                continue
            seen.add(val)
            bugs.append({
                "rule": "HARDCODED_DATE",
                "category": "HARDCODED DATE FILTER",
                "severity": "medium",
                "confidence": "high",
                "line_reference": f"WHERE ... '{val}'",
                "description": (
                    f"Hardcoded date '{val}' in WHERE clause. "
                    "This filter becomes stale and silently excludes data over time."
                ),
                "impact": (
                    f"Any model filtering from '{val}' is not measuring the full dataset. "
                    "A 'lifetime' metric filtered to a static date is not lifetime."
                ),
                "fix": (
                    "Replace with a dynamic date:\n"
                    "  WHERE date_col >= DATEADD(day, -365, CURRENT_DATE)\n"
                    "  or use a dbt variable: {{ var('start_date') }}"
                )
            })

    return bugs


# ─────────────────────────────────────────
# RULE 6: CROSS JOIN detection
# ─────────────────────────────────────────
def check_cross_joins(tree: exp.Expression) -> list:
    """
    Finds CROSS JOINs — cartesian products that explode row counts silently.
    """
    bugs = []

    for join in tree.find_all(exp.Join):
        kind = join.kind
        if kind and kind.upper() == "CROSS":
            table = join.find(exp.Table)
            bugs.append({
                "rule": "CROSS_JOIN",
                "category": "CROSS JOIN — CARTESIAN PRODUCT",
                "severity": "critical",
                "confidence": "high",
                "line_reference": f"CROSS JOIN {table}",
                "description": (
                    "CROSS JOIN produces a cartesian product — every row "
                    "in the left table paired with every row in the right table."
                ),
                "impact": (
                    "If left has 1M rows and right has 1K rows, "
                    "output has 1B rows. SUM(revenue) inflates by 1000×. "
                    "No error is thrown."
                ),
                "fix": (
                    "Replace CROSS JOIN with an appropriate JOIN type and ON condition, "
                    "or confirm this is intentional (e.g. date spine generation)."
                )
            })

    return bugs


# ─────────────────────────────────────────
# RULE 7: Integer division truncation
# ─────────────────────────────────────────
def check_integer_division(tree: exp.Expression) -> list:
    """
    Finds SUM(x) / COUNT(*) patterns where neither side has a float cast.
    These truncate decimals silently — 10/3 = 3 not 3.33.
    """
    bugs = []
    flagged = set()

    for div in tree.find_all(exp.Div):
        left = div.left
        right = div.right

        # Skip if NULLIF is present — means someone thought about this
        full_sql = div.sql().upper()
        if "NULLIF" in full_sql:
            continue

        # Skip if explicit float cast present
        if "1.0" in full_sql or "FLOAT" in full_sql or "CAST" in full_sql or "100.0" in full_sql:
            continue

        left_is_agg = isinstance(left, (exp.Sum, exp.Count))
        right_is_agg = isinstance(right, (exp.Sum, exp.Count))

        if not (left_is_agg or right_is_agg):
            continue

        key = div.sql()
        if key in flagged:
            continue
        flagged.add(key)

        bugs.append({
            "rule": "INTEGER_DIVISION",
            "category": "INTEGER DIVISION TRUNCATION",
            "severity": "medium",
            "confidence": "medium",
            "line_reference": div.sql(),
            "description": (
                f"Division '{div.sql()}' may truncate decimals. "
                "If both operands are integers, 10/3 = 3 not 3.33."
            ),
            "impact": (
                "Revenue averages, rates, and percentages silently rounded down. "
                "Compounds across millions of rows."
            ),
            "fix": (
                f"Multiply numerator by 1.0 to force float division:\n"
                f"  {left.sql()} * 1.0 / {right.sql()}"
            )
        })

    return bugs


# ─────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────
def run_ast_analysis(sql: str, model_name: str, dialect: str = "sqlite") -> dict:
    """
    Runs all deterministic AST rules against a SQL model.
    Returns a structured report.
    """
    tree = parse_sql(sql, dialect)

    if tree is None:
        return {
            "model_name": model_name,
            "overall_risk": "unknown",
            "summary": "AST parsing failed — falling back to LLM analysis only.",
            "bugs": [],
            "data_loss_risk": False,
            "estimated_rows_affected": "unknown",
            "safe_to_run": True,
            "source": "ast"
        }

    # Rules list — order matters, runs top to bottom
    rules = [
        check_left_join_nullified,
        check_null_exclusion_via_neq,
        check_divide_by_zero,
        check_integer_division,
        check_select_star,
        check_hardcoded_dates,
        check_cross_joins,
    ]

    all_bugs = []
    for rule in rules:
        try:
            found = rule(tree)
            all_bugs.extend(found)
        except Exception:
            pass  # Never crash the whole analysis on a rule failure

    # Final deduplication by line_reference + rule
    seen = set()
    deduped = []
    for bug in all_bugs:
        key = bug.get("line_reference", "") + bug.get("rule", "")
        if key not in seen:
            seen.add(key)
            deduped.append(bug)

    # Overall risk
    severities = [b["severity"] for b in deduped]
    if "critical" in severities:
        overall = "critical"
    elif "high" in severities:
        overall = "high"
    elif "medium" in severities:
        overall = "medium"
    elif "low" in severities:
        overall = "low"
    else:
        overall = "clean"

    data_loss = any(b["severity"] in ("critical", "high") for b in deduped)

    return {
        "model_name": model_name,
        "overall_risk": overall,
        "summary": (
            f"AST analysis found {len(deduped)} deterministic issue(s)."
            if deduped else "No deterministic bugs detected."
        ),
        "bugs": deduped,
        "data_loss_risk": data_loss,
        "estimated_rows_affected": "significant" if data_loss else "none",
        "safe_to_run": overall not in ("critical", "high"),
        "source": "ast"
    }


def analyze_all_models_ast(project_path: str, dialect: str = "sqlite") -> list:
    """
    Runs AST analysis on every SQL model in the dbt project.
    """
    models_path = Path(project_path) / "models"

    if not models_path.exists():
        print(f"⚠️  No models folder at {models_path}")
        return []

    sql_files = list(models_path.glob("**/*.sql"))
    if not sql_files:
        print("⚠️  No SQL models found.")
        return []

    print(f"\n⚡ AST analysis on {len(sql_files)} model(s)...\n")

    reports = []
    for sql_file in sql_files:
        model_name = sql_file.stem
        print(f"  → Parsing {model_name}...")
        with open(sql_file) as f:
            sql = f.read()
        report = run_ast_analysis(sql, model_name, dialect)
        reports.append(report)

    return reports