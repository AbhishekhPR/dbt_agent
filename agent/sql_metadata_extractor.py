import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import sqlglot
    from sqlglot import parse_one
    import sqlglot.expressions as exp
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False

try:
    from agent.metrics_store import METADATA_HISTORY_DB
except ImportError:
    METADATA_HISTORY_DB = Path(__file__).resolve().parent.parent / "metadata_history.db"

SQL_METADATA_TABLE = "sql_metadata"
AGGREGATION_FUNCTIONS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND"}
WINDOW_FUNCTIONS = {"ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD"}


# ─────────────────────────────────────────
# SQL PREPROCESSING
# ─────────────────────────────────────────

def strip_jinja(sql: str) -> str:
    sql = re.sub(r"\{\{[^}]*config[^}]*\}\}", "", sql, flags=re.DOTALL)
    sql = re.sub(r"\{\{\s*ref\(['\"](\w+)['\"]\)\s*\}\}", r"\1", sql)
    sql = re.sub(
        r"\{\{\s*source\(['\"](\w+)['\"],\s*['\"](\w+)['\"]\)\s*\}\}",
        r"\1__\2",
        sql,
        flags=re.DOTALL,
    )
    sql = re.sub(r"\{\{.*?\}\}", "placeholder", sql, flags=re.DOTALL)
    sql = re.sub(r"\{#.*?#\}", "", sql, flags=re.DOTALL)
    sql = re.sub(r"\{%.*?%\}", "", sql, flags=re.DOTALL)
    return sql.strip()


def parse_sql(sql: str, dialect: str = "sqlite") -> Optional[Any]:
    if not HAS_SQLGLOT:
        return None

    try:
        clean_sql = strip_jinja(sql)
        return parse_one(clean_sql, dialect=dialect)
    except Exception:
        return None


def _normalize_identifier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip().strip('"').strip('`').strip("'")


def _expression_name(node: Any) -> str:
    if hasattr(node, "name") and node.name:
        return node.name.upper()
    return type(node).__name__.upper()


def _function_name(node: Any) -> Optional[str]:
    sql = node.sql().upper() if hasattr(node, "sql") else ""
    match = re.match(r"\s*([A-Z_]+)\s*\(", sql)
    if match:
        return match.group(1)
    return _expression_name(node)


def _uniq(items: List[str]) -> List[str]:
    return list(dict.fromkeys([item for item in items if item]))


# ─────────────────────────────────────────
# SQLGLot AST extraction helpers
# ─────────────────────────────────────────

def _extract_source_tables_ast(tree: Any) -> List[str]:
    tables = []
    for table in tree.find_all(exp.Table):
        name = _normalize_identifier(table.name)
        if name:
            tables.append(name)
    return _uniq(tables)


def _extract_joins_ast(tree: Any) -> List[Dict[str, str]]:
    joins = []
    for join in tree.find_all(exp.Join):
        table_node = join.find(exp.Table)
        if not table_node:
            continue
        table_name = _normalize_identifier(table_node.name)
        join_type = (join.side or join.kind or "").upper()
        condition = None
        on_expr = join.args.get("on")
        if on_expr is not None:
            condition = on_expr.sql()
        joins.append({
            "join_type": join_type or "JOIN",
            "table": table_name,
            "condition": condition or "",
        })
    return joins


def _flatten_conditions(expression: Any) -> List[Any]:
    if expression is None:
        return []

    op_name = type(expression).__name__.upper()
    if op_name in ("AND", "OR"):
        left = getattr(expression, "left", None)
        right = getattr(expression, "right", None)
        return _flatten_conditions(left) + _flatten_conditions(right)

    return [expression]


def _extract_filters_ast(tree: Any) -> List[str]:
    where = tree.find(exp.Where)
    if not where or where.this is None:
        return []

    filters = []
    for condition in _flatten_conditions(where.this):
        sql = condition.sql()
        if sql:
            filters.append(sql)
    return _uniq(filters)


def _extract_group_by_ast(tree: Any) -> List[str]:
    group = tree.find(exp.Group)
    if not group:
        return []
    return [expr.sql() for expr in group.expressions if expr is not None]


def _extract_output_columns_ast(tree: Any) -> List[str]:
    select = tree.find(exp.Select)
    if not select:
        return []

    output_columns = []
    for expr in select.expressions:
        alias = None
        if isinstance(expr, exp.Alias):
            alias = _normalize_identifier(expr.alias)
        elif hasattr(expr, "alias"):
            alias = _normalize_identifier(expr.alias)

        if alias:
            output_columns.append(alias)
            continue

        if isinstance(expr, exp.Column):
            output_columns.append(expr.sql())
            continue

        output_columns.append(expr.sql())

    return _uniq(output_columns)


def _extract_aggregations_ast(tree: Any) -> List[str]:
    seen = set()
    for node in tree.walk():
        name = _function_name(node)
        if name in AGGREGATION_FUNCTIONS:
            seen.add(name)
    return sorted(seen)


def _extract_window_functions_ast(tree: Any) -> List[str]:
    seen = set()
    for node in tree.find_all(exp.Window):
        function = getattr(node, "this", None)
        if function is not None:
            name = _function_name(function)
            if name in WINDOW_FUNCTIONS:
                seen.add(name)
    return sorted(seen)


# ─────────────────────────────────────────
# Regex fallback extraction helpers
# ─────────────────────────────────────────

def _split_select_list(select_sql: str) -> List[str]:
    items: List[str] = []
    current = []
    depth = 0
    for char in select_sql:
        if char == "(" :
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)

        if char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    if current:
        items.append("".join(current).strip())
    return items


def _extract_source_tables_regex(sql: str) -> List[str]:
    source_tables = []
    match = re.search(r"\bFROM\b\s+(.*?)(?:\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if match:
        from_clause = match.group(1)
        parts = re.split(r"\bJOIN\b", from_clause, flags=re.IGNORECASE)
        for part in parts:
            tokens = part.strip().split()
            if tokens:
                table = tokens[0].strip(",; ")
                if table.upper() not in {"SELECT", "FROM"}:
                    source_tables.append(table)
    for join_match in re.finditer(r"\b(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s+JOIN\s+([\w\.\"]+)", sql, flags=re.IGNORECASE):
        source_tables.append(_normalize_identifier(join_match.group(1)))
    return _uniq(source_tables)


def _extract_joins_regex(sql: str) -> List[Dict[str, str]]:
    joins = []
    pattern = re.compile(
        r"\b(LEFT|RIGHT|INNER|FULL|CROSS|OUTER|NATURAL)?\s*JOIN\s+([\w\.\"]+)(?:\s+\w+)?\s+ON\s+(.*?)(?=\b(?:LEFT|RIGHT|INNER|FULL|CROSS|OUTER|NATURAL)?\s*JOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(sql):
        join_type = (match.group(1) or "JOIN").strip().upper()
        table = _normalize_identifier(match.group(2))
        condition = match.group(3).strip()
        joins.append({"join_type": join_type, "table": table, "condition": condition})
    return joins


def _extract_filters_regex(sql: str) -> List[str]:
    match = re.search(r"\bWHERE\b\s+(.*?)(?=\bGROUP\b|\bORDER\b|\bLIMIT\b|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    where_clause = match.group(1).strip()
    conditions = re.split(r"\s+AND\s+|\s+OR\s+", where_clause, flags=re.IGNORECASE)
    return _uniq([cond.strip() for cond in conditions if cond.strip()])


def _extract_group_by_regex(sql: str) -> List[str]:
    match = re.search(r"\bGROUP\s+BY\b\s+(.*?)(?=\bORDER\b|\bLIMIT\b|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    expressions = _split_select_list(match.group(1))
    return _uniq([expr.strip() for expr in expressions if expr.strip()])


def _extract_output_columns_regex(sql: str) -> List[str]:
    match = re.search(r"\bSELECT\b\s+(.*?)\bFROM\b", sql, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    select_clause = match.group(1)
    columns = []
    for item in _split_select_list(select_clause):
        alias_match = re.search(r"\bAS\b\s+([\w_]+)$", item, flags=re.IGNORECASE)
        if alias_match:
            columns.append(alias_match.group(1))
            continue
        tokens = item.strip().split()
        if len(tokens) >= 2 and tokens[-2].upper() != "AS":
            columns.append(_normalize_identifier(tokens[-1]))
            continue
        columns.append(item.strip())
    return _uniq(columns)


def _extract_aggregations_regex(sql: str) -> List[str]:
    found = re.findall(r"\b(COUNT|SUM|AVG|MIN|MAX|ROUND)\b", sql, flags=re.IGNORECASE)
    return sorted({name.upper() for name in found})


def _extract_window_functions_regex(sql: str) -> List[str]:
    found = re.findall(r"\b(ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD)\b", sql, flags=re.IGNORECASE)
    return sorted({name.upper() for name in found})


# ─────────────────────────────────────────
# Metadata extraction
# ─────────────────────────────────────────

def extract_metadata_from_sql(sql: str, model_name: str, dialect: str = "sqlite") -> Dict[str, Any]:
    tree = parse_sql(sql, dialect)
    if tree is not None:
        metadata = {
            "model_name": model_name,
            "source_tables": _extract_source_tables_ast(tree),
            "joins": _extract_joins_ast(tree),
            "filters": _extract_filters_ast(tree),
            "group_by": _extract_group_by_ast(tree),
            "output_columns": _extract_output_columns_ast(tree),
            "aggregations": _extract_aggregations_ast(tree),
            "window_functions": _extract_window_functions_ast(tree),
        }
    else:
        clean_sql = strip_jinja(sql)
        metadata = {
            "model_name": model_name,
            "source_tables": _extract_source_tables_regex(clean_sql),
            "joins": _extract_joins_regex(clean_sql),
            "filters": _extract_filters_regex(clean_sql),
            "group_by": _extract_group_by_regex(clean_sql),
            "output_columns": _extract_output_columns_regex(clean_sql),
            "aggregations": _extract_aggregations_regex(clean_sql),
            "window_functions": _extract_window_functions_regex(clean_sql),
        }

    return metadata


def _init_sql_metadata_table(conn: sqlite3.Connection):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {SQL_METADATA_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            model_name TEXT,
            metadata_json TEXT
        )
    """)
    conn.commit()


def _save_metadata(model_name: str, metadata: Dict[str, Any]):
    conn = sqlite3.connect(str(METADATA_HISTORY_DB))
    _init_sql_metadata_table(conn)
    conn.execute(
        f"INSERT INTO {SQL_METADATA_TABLE} (model_name, metadata_json) VALUES (?, ?)",
        (model_name, json.dumps(metadata, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def extract_sql_metadata(models_path: str, dialect: str = "sqlite") -> List[Dict[str, Any]]:
    base_path = Path(models_path)
    if base_path.name != "models" and (base_path / "models").exists():
        base_path = base_path / "models"

    if not base_path.exists() or not base_path.is_dir():
        raise FileNotFoundError(f"Models path not found: {base_path}")

    sql_files = sorted(base_path.glob("**/*.sql"))
    metadata_list: List[Dict[str, Any]] = []
    for sql_file in sql_files:
        model_name = sql_file.stem
        with open(sql_file, "r", encoding="utf-8") as f:
            sql = f.read()

        metadata = extract_metadata_from_sql(sql, model_name, dialect)
        _save_metadata(model_name, metadata)
        metadata_list.append(metadata)

    return metadata_list
