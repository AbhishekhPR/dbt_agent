import json
import re
from pathlib import Path

from agent.signals import Signal

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        return False

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


BLAST_RADIUS_SIGNAL_CONFIDENCE = {
    "HIGH": 95,
    "MEDIUM": 85,
    "LOW": 75,
}

BLAST_RADIUS_SIGNAL_SCORES = {
    "HIGH": -25,
    "MEDIUM": -15,
    "LOW": -5,
}


# ─────────────────────────────────────────
# MANIFEST LOADER
# ─────────────────────────────────────────
def load_manifest(project_path: str) -> dict:
    """
    Loads dbt's manifest.json from the target folder.
    Returns empty dict if not found — not a fatal error.
    """
    manifest_path = Path(project_path) / "target" / "manifest.json"

    if not manifest_path.exists():
        return {}

    with open(manifest_path) as f:
        return json.load(f)


# ─────────────────────────────────────────
# MANIFEST-BASED GRAPH (ref() dependencies)
# ─────────────────────────────────────────
def build_dependency_graph_from_manifest(manifest: dict) -> dict:
    """
    Builds reverse dependency graph from dbt manifest.
    Only works for models using {{ ref() }} or {{ source() }}.

    Returns: { 'table_name': ['model_a', 'model_b'] }
    """
    graph = {}
    nodes = manifest.get("nodes", {})

    for node_id, node in nodes.items():
        if node.get("resource_type") != "model":
            continue

        model_name = node.get("name")
        depends_on = node.get("depends_on", {}).get("nodes", [])

        for dep in depends_on:
            parts = dep.split(".")
            if len(parts) >= 3:
                dep_name = parts[-1].lower()
                if dep_name not in graph:
                    graph[dep_name] = []
                if model_name not in graph[dep_name]:
                    graph[dep_name].append(model_name)

    return graph


# ─────────────────────────────────────────
# SQL-BASED GRAPH (raw table references)
# ─────────────────────────────────────────
def build_dependency_graph_from_sql(project_path: str) -> dict:
    """
    Builds dependency graph by reading SQL files directly.
    Finds table references in FROM and JOIN clauses.
    This catches raw table references that dbt manifest misses.

    Returns: { 'table_name': ['model_a', 'model_b'] }
    """
    from agent.ast_analyzer import strip_jinja

    models_path = Path(project_path) / "models"
    graph = {}

    if not models_path.exists():
        return graph

    # SQL keywords to ignore when parsing table names
    skip_words = {
        'select', 'where', 'on', 'and', 'or', 'not', 'null',
        'true', 'false', 'case', 'when', 'then', 'else', 'end',
        'inner', 'outer', 'left', 'right', 'full', 'cross',
        'lateral', 'values', 'with', 'as', 'distinct', 'group',
        'order', 'by', 'having', 'limit', 'offset', 'union',
        'all', 'except', 'intersect', 'into', 'set', 'update',
        'delete', 'insert', 'create', 'drop', 'alter', 'table',
        'view', 'index', 'schema', 'database', 'exists', 'if',
        'placeholder', 'using', 'natural', 'join'
    }

    table_pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)',
        re.IGNORECASE
    )

    for sql_file in models_path.glob("**/*.sql"):
        model_name = sql_file.stem
        with open(sql_file) as f:
            raw_sql = f.read()

        clean_sql = strip_jinja(raw_sql)

        for match in table_pattern.finditer(clean_sql):
            table_ref = match.group(1).lower()

            if table_ref in skip_words:
                continue

            # Skip if it looks like a CTE name (usually defined in WITH block)
            if table_ref == model_name.lower():
                continue

            if table_ref not in graph:
                graph[table_ref] = []
            if model_name not in graph[table_ref]:
                graph[table_ref].append(model_name)

    return graph


# ─────────────────────────────────────────
# MERGED GRAPH
# ─────────────────────────────────────────
def build_full_dependency_graph(project_path: str) -> dict:
    """
    Builds a complete dependency graph by merging:
    1. SQL-based resolution (catches raw table references)
    2. Manifest-based resolution (catches ref() and source() references)

    SQL-based is the primary source since most real models
    mix raw table names with dbt refs.
    """
    # SQL-based first — catches raw FROM/JOIN table names
    graph = build_dependency_graph_from_sql(project_path)

    # Merge manifest-based on top
    manifest = load_manifest(project_path)
    if manifest:
        manifest_graph = build_dependency_graph_from_manifest(manifest)
        for table, models in manifest_graph.items():
            if table not in graph:
                graph[table] = []
            for model in models:
                if model not in graph[table]:
                    graph[table].append(model)

    return graph


# ─────────────────────────────────────────
# MODEL SQL READER
# ─────────────────────────────────────────
def get_model_sql(project_path: str, model_name: str) -> str:
    """
    Reads SQL for a model.
    Tries compiled SQL first, falls back to raw model SQL.
    """
    compiled_path = Path(project_path) / "target" / "compiled"
    for sql_file in compiled_path.glob(f"**/{model_name}.sql"):
        with open(sql_file) as f:
            return f.read()

    models_path = Path(project_path) / "models"
    for sql_file in models_path.glob(f"**/{model_name}.sql"):
        with open(sql_file) as f:
            return f.read()

    return ""


# ─────────────────────────────────────────
# COLUMN REFERENCE CHECKER
# ─────────────────────────────────────────
def check_column_reference(sql: str, column_name: str) -> bool:
    """
    Checks if a specific column name appears in a SQL model.
    Uses word-boundary matching to avoid false matches.
    e.g. 'status' should not match 'order_status'
    """
    pattern = re.compile(
        r'\b' + re.escape(column_name.lower()) + r'\b',
        re.IGNORECASE
    )
    return bool(pattern.search(sql))


# ─────────────────────────────────────────
# BLAST RADIUS CALCULATOR
# ─────────────────────────────────────────
def calculate_blast_radius(
    project_path: str,
    changed_table: str,
    changed_columns: list = None
) -> dict:
    """
    Given a changed table and optionally specific columns,
    returns the full blast radius — which models will break and why.

    Uses merged SQL + manifest dependency resolution.
    """
    dep_graph = build_full_dependency_graph(project_path)

    changed_table_lower = changed_table.lower()
    directly_affected_names = dep_graph.get(changed_table_lower, [])

    if not directly_affected_names:
        return {
            "changed_table": changed_table,
            "changed_columns": changed_columns or [],
            "directly_affected": [],
            "indirectly_affected": [],
            "total_affected": 0,
            "summary": f"No models depend on '{changed_table}' — safe to change."
        }

    # For each directly affected model check column references
    direct_details = []
    for model in directly_affected_names:
        sql = get_model_sql(project_path, model)
        referenced_cols = []
        risk = "high"

        if changed_columns and sql:
            referenced_cols = [
                col for col in changed_columns
                if check_column_reference(sql, col)
            ]
            # If specific columns changed but this model doesn't reference any of them
            if changed_columns and not referenced_cols:
                risk = "low"

        direct_details.append({
            "model": model,
            "risk": risk,
            "referenced_changed_columns": referenced_cols,
            "reason": (
                f"References column(s) {referenced_cols} from '{changed_table}'"
                if referenced_cols
                else f"Depends on table '{changed_table}'"
            )
        })

    # Find all indirectly affected models through recursive downstream traversal.
    indirectly_affected = []
    direct_model_lowers = {model.lower() for model in directly_affected_names}
    listed_indirect = set()
    visited = {changed_table_lower}
    queue = [
        (direct_model, [changed_table, direct_model])
        for direct_model in directly_affected_names
    ]

    while queue:
        current_model, path = queue.pop(0)
        current_lower = current_model.lower()
        if current_lower in visited:
            continue
        visited.add(current_lower)

        for downstream_model in dep_graph.get(current_lower, []):
            downstream_lower = downstream_model.lower()
            downstream_path = path + [downstream_model]

            if (
                downstream_lower not in direct_model_lowers
                and downstream_lower not in listed_indirect
            ):
                indirectly_affected.append({
                    "model": downstream_model,
                    "risk": "medium",
                    "dependency_path": downstream_path,
                    "reason": _format_dependency_reason(downstream_path),
                })
                listed_indirect.add(downstream_lower)

            if downstream_lower not in visited:
                queue.append((downstream_model, downstream_path))

    total = len(directly_affected_names) + len(indirectly_affected)

    return {
        "changed_table": changed_table,
        "changed_columns": changed_columns or [],
        "directly_affected": direct_details,
        "indirectly_affected": indirectly_affected,
        "total_affected": total,
        "summary": (
            f"{total} model(s) affected by changes to '{changed_table}': "
            f"{len(directly_affected_names)} direct, "
            f"{len(indirectly_affected)} indirect."
        )
    }


def _format_dependency_reason(path: list) -> str:
    """
    Formats an upstream-to-downstream path as a readable dependency chain.
    Example: raw -> model_a -> model_b becomes
    "Depends on model_a which depends on raw".
    """
    chain = list(reversed(path[:-1]))
    return "Depends on " + " which depends on ".join(chain)


def to_signal(blast_radius_result: dict) -> Signal:
    severity = _blast_radius_signal_severity(blast_radius_result)
    affected_models = _affected_model_names(blast_radius_result)
    neutral = not affected_models

    metadata = {
        "changed_model": (
            blast_radius_result.get("changed_model")
            or blast_radius_result.get("changed_table")
        ),
        "affected_models": affected_models,
        "downstream_model_count": blast_radius_result.get(
            "total_affected",
            len(affected_models),
        ),
    }
    for field in ("dashboard_count", "blast_radius_score", "dependency_depth"):
        if field in blast_radius_result:
            metadata[field] = blast_radius_result.get(field)

    return Signal(
        component="blast_radius",
        severity=severity,
        confidence=BLAST_RADIUS_SIGNAL_CONFIDENCE[severity],
        score=0 if neutral else BLAST_RADIUS_SIGNAL_SCORES[severity],
        reasons=(
            []
            if neutral
            else _blast_radius_signal_reasons(blast_radius_result, affected_models)
        ),
        metadata=metadata,
    )


def _blast_radius_signal_severity(result: dict) -> str:
    explicit = result.get("severity") or result.get("risk_level")
    if explicit:
        return str(explicit).upper()

    risks = [
        str(item.get("risk", "")).upper()
        for item in (
            result.get("directly_affected", [])
            + result.get("indirectly_affected", [])
        )
        if item.get("risk")
    ]
    if "HIGH" in risks:
        return "HIGH"
    if "MEDIUM" in risks:
        return "MEDIUM"
    return "LOW"


def _affected_model_names(result: dict) -> list[str]:
    names = []
    for item in result.get("directly_affected", []) + result.get("indirectly_affected", []):
        model = item.get("model")
        if model and model not in names:
            names.append(model)
    return names


def _blast_radius_signal_reasons(result: dict, affected_models: list[str]) -> list[str]:
    reasons = []
    if affected_models:
        reasons.append("Downstream models affected")
    if any("dashboard" in model.lower() for model in affected_models):
        reasons.append("Executive dashboard affected")
    if any("critical" in model.lower() or "executive" in model.lower() for model in affected_models):
        reasons.append("Critical business model affected")
    if any(
        len(item.get("dependency_path", [])) >= 3
        for item in result.get("indirectly_affected", [])
    ):
        reasons.append("Large dependency chain detected")
    return reasons


# ─────────────────────────────────────────
# PRETTY PRINTER
# ─────────────────────────────────────────
def print_blast_radius(report: dict):
    """Pretty prints the blast radius report."""

    if "error" in report:
        print(report['error'])
        return

    changed_table = report.get("changed_table")
    changed_cols = report.get("changed_columns", [])
    total = report.get("total_affected", 0)

    print(f"\n{'━' * 55}")
    print(f"  Blast radius for: {changed_table}")
    if changed_cols:
        print(f"  Changed columns:  {', '.join(changed_cols)}")
    print(f"  Total affected:   {total} model(s)")
    print(f"{'━' * 55}")

    if total == 0:
        print(f"\n  No models depend on '{changed_table}' — safe to change.\n")
        return

    direct = report.get("directly_affected", [])
    if direct:
        print(f"\n  🔴 Directly affected ({len(direct)}):\n")
        for item in direct:
            risk_emoji = "🔴" if item["risk"] == "high" else "🟡"
            cols = item.get("referenced_changed_columns", [])
            print(f"    {risk_emoji} {item['model']}")
            print(f"       {item['reason']}")
            if cols:
                print(f"       Will break on: {', '.join(cols)}")
            print()

    indirect = report.get("indirectly_affected", [])
    if indirect:
        print(f"  🟠 Indirectly affected ({len(indirect)}):\n")
        for item in indirect:
            print(f"    🟠 {item['model']}")
            print(f"       {item['reason']}")
            print()

    print(f"  Summary: {report.get('summary')}\n")


# ─────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────
def run_blast_radius(project_path: str, table: str, columns: str = None):
    """Main entry point called from CLI."""
    changed_cols = [c.strip() for c in columns.split(",")] if columns else []

    print(f"\nCalculating blast radius for '{table}'...")
    if changed_cols:
        print(f"   Changed columns: {changed_cols}")

    report = calculate_blast_radius(project_path, table, changed_cols)
    print_blast_radius(report)
    return report
