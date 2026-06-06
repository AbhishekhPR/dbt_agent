import re
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List

from agent.metrics_store import METADATA_HISTORY_DB
from agent.sql_metadata_extractor import strip_jinja


Risk = Dict[str, str]


def detect_sql_risks(project_path: str, persist: bool = True) -> List[Risk]:
    """Scan dbt model SQL files for risky static transformation patterns."""
    project = Path(project_path)
    models_path = project / "models"
    if project.name == "models":
        models_path = project

    if not models_path.exists() or not models_path.is_dir():
        raise FileNotFoundError(f"Models path not found: {models_path}")

    risks: List[Risk] = []
    for sql_file in sorted(models_path.glob("**/*.sql")):
        sql = sql_file.read_text(encoding="utf-8")
        clean_sql = _normalize_sql(strip_jinja(sql))
        model_name = sql_file.stem
        file_path = _relative_file_path(sql_file, project)
        risks.extend(_detect_risks_for_model(clean_sql, model_name, file_path))

    if persist:
        _save_sql_risks(project.name, risks)

    return risks


def print_sql_risks(risks: List[Risk]) -> None:
    print("SQL Risk Report")
    if not risks:
        print("\nNo SQL risks found.")
        return

    for risk in risks:
        print()
        print(f"Model: {risk['model']}")
        print(f"Severity: {risk['severity'].upper()}")
        print(f"Risk: {risk['message']}")
        print(f"Evidence: {risk['evidence']}")
        print(f"Recommendation: {risk['recommendation']}")


def _detect_risks_for_model(sql: str, model_name: str, file_path: str) -> List[Risk]:
    rules: List[Callable[[str, str, str], List[Risk]]] = [
        _detect_left_join_filter_risks,
        _detect_select_star_risks,
        _detect_cross_join_risks,
        _detect_join_without_condition_risks,
        _detect_division_by_zero_risks,
        _detect_hardcoded_date_filter_risks,
        _detect_not_equal_filter_risks,
    ]
    risks: List[Risk] = []
    for rule in rules:
        risks.extend(rule(sql, model_name, file_path))
    return risks


def _risk(
    model_name: str,
    file_path: str,
    risk_type: str,
    severity: str,
    message: str,
    evidence: str,
    recommendation: str,
) -> Risk:
    return {
        "model": model_name,
        "file": file_path,
        "risk_type": risk_type,
        "severity": severity,
        "message": message,
        "evidence": _sanitize_evidence(evidence),
        "recommendation": recommendation,
    }


def _detect_left_join_filter_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    risks: List[Risk] = []
    where_clause = _where_clause(sql)
    if not where_clause:
        return risks

    aliases = _left_join_aliases(sql)
    for alias in aliases:
        conditions = _conditions_for_alias(where_clause, alias)
        for condition in conditions:
            if _condition_allows_null(condition, alias):
                continue
            risks.append(_risk(
                model_name,
                file_path,
                "left_join_filter_risk",
                "high",
                "LEFT JOIN may behave like INNER JOIN because WHERE filters the right-side table",
                f"WHERE {condition}",
                "Move the right-table filter into the JOIN condition or explicitly allow NULLs",
            ))
    return _dedupe_risks(risks)


def _detect_select_star_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    if not re.search(r"\bSELECT\s+(?:DISTINCT\s+)?(?:\*|[\w\"`]+\.\*)(?=\s|,|\bFROM\b)", sql, re.IGNORECASE):
        return []
    return [_risk(
        model_name,
        file_path,
        "select_star_risk",
        "medium",
        "SELECT * can cause downstream schema changes when upstream columns change",
        "SELECT *",
        "Select explicit columns",
    )]


def _detect_cross_join_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    if not re.search(r"\bCROSS\s+JOIN\b", sql, re.IGNORECASE):
        return []
    return [_risk(
        model_name,
        file_path,
        "cross_join_risk",
        "high",
        "CROSS JOIN can create Cartesian products and row-count explosions",
        "CROSS JOIN",
        "Confirm this is intentional or replace with a proper join condition",
    )]


def _detect_join_without_condition_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    risks: List[Risk] = []
    for match in _join_matches(sql):
        join_text = match.group(0).strip()
        if re.match(r"\bCROSS\s+JOIN\b", join_text, re.IGNORECASE):
            continue
        if not re.search(r"\b(ON|USING)\b", join_text, re.IGNORECASE):
            risks.append(_risk(
                model_name,
                file_path,
                "join_without_condition_risk",
                "critical",
                "JOIN without ON or USING can create row multiplication",
                join_text,
                "Add an explicit ON or USING condition",
            ))
    return _dedupe_risks(risks)


def _detect_division_by_zero_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    risks: List[Risk] = []
    for expression in _division_expressions(sql):
        denominator = expression.split("/", 1)[1].strip()
        if denominator.upper().startswith("NULLIF("):
            continue
        risks.append(_risk(
            model_name,
            file_path,
            "division_by_zero_risk",
            "medium",
            "Division by zero risk",
            expression,
            "Use NULLIF around denominator",
        ))
    return _dedupe_risks(risks)


def _detect_hardcoded_date_filter_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    risks: List[Risk] = []
    where_clause = _where_clause(sql)
    if not where_clause:
        return risks

    pattern = re.compile(
        r"\b[\w\"`\.]*(?:date|_at|time)[\w\"`\.]*\s*(?:=|>=|>|<=|<|BETWEEN)\s*'?\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?'?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(where_clause):
        risks.append(_risk(
            model_name,
            file_path,
            "hardcoded_date_filter_risk",
            "medium",
            "Hardcoded date filters may silently exclude valid data over time",
            f"WHERE {match.group(0).strip()}",
            "Confirm the filter is intentional or parameterize it",
        ))
    return _dedupe_risks(risks)


def _detect_not_equal_filter_risks(sql: str, model_name: str, file_path: str) -> List[Risk]:
    risks: List[Risk] = []
    where_clause = _where_clause(sql)
    if not where_clause:
        return risks

    pattern = re.compile(r"\b[\w\"`\.]+\s*(?:!=|<>)\s*(?:'[^']*'|\"[^\"]*\"|[\w\.]+)", re.IGNORECASE)
    for match in pattern.finditer(where_clause):
        risks.append(_risk(
            model_name,
            file_path,
            "not_equal_filter_risk",
            "medium",
            "NOT EQUAL filters exclude NULLs silently",
            f"WHERE {match.group(0).strip()}",
            "Confirm NULL handling or use explicit logic",
        ))
    return _dedupe_risks(risks)


def _save_sql_risks(project_name: str, risks: List[Risk]) -> None:
    conn = sqlite3.connect(str(METADATA_HISTORY_DB))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sql_risks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                project_name TEXT,
                model_name TEXT,
                file_path TEXT,
                risk_type TEXT,
                severity TEXT,
                message TEXT,
                evidence TEXT,
                recommendation TEXT
            )
        """)
        conn.execute("DELETE FROM sql_risks WHERE project_name = ?", (project_name,))
        conn.executemany(
            """
            INSERT INTO sql_risks
            (project_name, model_name, file_path, risk_type, severity, message, evidence, recommendation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    project_name,
                    risk["model"],
                    risk["file"],
                    risk["risk_type"],
                    risk["severity"],
                    risk["message"],
                    risk["evidence"],
                    risk["recommendation"],
                )
                for risk in risks
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _normalize_sql(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"\s+", " ", sql).strip()


def _sanitize_evidence(evidence: str) -> str:
    text = evidence.strip()
    text = re.sub(r"'([^']*)'|\"([^\"]*)\"", _sanitize_quoted_literal, text)
    text = re.sub(
        r"(?<![\w.\]])\d+(?:\.\d+)?(?![\w.\[])",
        "[NUMBER_LITERAL]",
        text,
    )
    return text


def _sanitize_quoted_literal(match: re.Match) -> str:
    value = match.group(1) if match.group(1) is not None else match.group(2)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", value):
        return "[DATETIME_LITERAL]"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "[DATE_LITERAL]"
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        value,
    ):
        return "[UUID_LITERAL]"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "[EMAIL_LITERAL]"
    if _looks_like_secret(value):
        return "[SECRET_LITERAL]"
    return "[STRING_LITERAL]"


def _looks_like_secret(value: str) -> bool:
    compact = re.sub(r"[\s_-]", "", value)
    return len(compact) >= 24 and bool(re.search(r"[A-Za-z]", compact)) and bool(re.search(r"\d", compact))


def _relative_file_path(sql_file: Path, project_path: Path) -> str:
    try:
        return sql_file.relative_to(project_path).as_posix()
    except ValueError:
        return sql_file.as_posix()


def _where_clause(sql: str) -> str:
    match = re.search(
        r"\bWHERE\b\s+(.*?)(?=\bGROUP\s+BY\b|\bHAVING\b|\bQUALIFY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _left_join_aliases(sql: str) -> List[str]:
    aliases: List[str] = []
    pattern = re.compile(
        r"\bLEFT(?:\s+OUTER)?\s+JOIN\s+[\w\"`\.]+\s+(?:AS\s+)?([\w\"`]+)\s+\bON\b",
        re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        aliases.append(match.group(1).strip('"`'))
    return list(dict.fromkeys(aliases))


def _conditions_for_alias(where_clause: str, alias: str) -> List[str]:
    conditions = re.split(r"\s+AND\s+", where_clause, flags=re.IGNORECASE)
    alias_pattern = re.compile(rf"\b{re.escape(alias)}\.", re.IGNORECASE)
    return [condition.strip(" ();") for condition in conditions if alias_pattern.search(condition)]


def _condition_allows_null(condition: str, alias: str) -> bool:
    return re.search(rf"\b{re.escape(alias)}\.[\w\"`]+\s+IS\s+NULL\b", condition, re.IGNORECASE) is not None


def _join_matches(sql: str):
    return re.finditer(
        r"\b(?:LEFT|RIGHT|INNER|FULL|OUTER|NATURAL|CROSS)?\s*JOIN\b\s+.*?(?=\b(?:LEFT|RIGHT|INNER|FULL|OUTER|NATURAL|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bHAVING\b|\bQUALIFY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE,
    )


def _division_expressions(sql: str) -> List[str]:
    expressions: List[str] = []
    for index, char in enumerate(sql):
        if char != "/":
            continue
        if _inside_quoted_string(sql, index):
            continue
        left_start = _expression_start(sql, index - 1)
        right_end = _expression_end(sql, index + 1)
        if left_start is None or right_end is None:
            continue
        expression = sql[left_start:right_end].strip()
        if expression:
            expressions.append(expression)
    return expressions


def _inside_quoted_string(sql: str, index: int) -> bool:
    single = False
    double = False
    for char in sql[:index]:
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
    return single or double


def _expression_start(sql: str, index: int) -> int | None:
    while index >= 0 and sql[index].isspace():
        index -= 1
    if index < 0:
        return None

    if sql[index] == ")":
        open_index = _matching_open_paren(sql, index)
        if open_index is None:
            return None
        name_start = open_index - 1
        while name_start >= 0 and sql[name_start].isspace():
            name_start -= 1
        while name_start >= 0 and _identifier_char(sql[name_start]):
            name_start -= 1
        return name_start + 1

    while index >= 0 and _identifier_char(sql[index]):
        index -= 1
    return index + 1


def _expression_end(sql: str, index: int) -> int | None:
    length = len(sql)
    while index < length and sql[index].isspace():
        index += 1
    if index >= length:
        return None

    name_start = index
    while index < length and _identifier_char(sql[index]):
        index += 1
    while index < length and sql[index].isspace():
        index += 1

    if index < length and sql[index] == "(":
        close_index = _matching_close_paren(sql, index)
        return close_index + 1 if close_index is not None else None

    return index if index > name_start else None


def _matching_open_paren(sql: str, close_index: int) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        if sql[index] == ")":
            depth += 1
        elif sql[index] == "(":
            depth -= 1
            if depth == 0:
                return index
    return None


def _matching_close_paren(sql: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _identifier_char(char: str) -> bool:
    return char.isalnum() or char in {"_", ".", '"', "`", "*"}


def _dedupe_risks(risks: List[Risk]) -> List[Risk]:
    seen = set()
    unique: List[Risk] = []
    for risk in risks:
        key = (risk["model"], risk["risk_type"], risk["evidence"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(risk)
    return unique
