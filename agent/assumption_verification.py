import copy
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

from agent.signals import Severity, Signal


@dataclass
class AssumptionCheck:
    check_id: str
    kpi_name: str
    model_name: str
    column_name: str | None
    invariant: str
    check_type: str
    sql: str
    evaluated: bool = False
    status: str = "not_evaluated"
    passed: bool | None = None
    violation_count: int | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serializable(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AssumptionCheck":
        values = dict(payload or {})
        values["metadata"] = dict(values.get("metadata") or {})
        return cls(**values)


@dataclass
class AssumptionVerificationReport:
    checks: list[AssumptionCheck] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serializable(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AssumptionVerificationReport":
        data = dict(payload or {})
        return cls(
            checks=[
                AssumptionCheck.from_dict(check)
                for check in list(data.get("checks") or [])
            ],
            metadata=dict(data.get("metadata") or {}),
        )


def build_assumption_verification_report(
    *,
    contracts,
    project_context=None,
    connection=None,
) -> AssumptionVerificationReport:
    """Generate executable SQL checks from semantic contracts.

    The generated checks are deterministic and warehouse-adapter free. When a
    DB-API compatible connection is provided, checks are executed immediately;
    otherwise they remain available as SQL with status "not_evaluated".
    """
    contract_list = copy.deepcopy(list(contracts or []))
    context = copy.deepcopy(project_context or {})
    models_by_name = _models_by_name(context)
    checks = []

    for contract in contract_list:
        checks.extend(_checks_for_contract(contract, models_by_name))

    checks = _deduplicate_checks(checks)
    if connection is not None:
        checks = [_evaluate_check(check, connection) for check in checks]

    evaluated_count = len([check for check in checks if check.evaluated])
    failed_count = len([check for check in checks if check.status == "failed"])
    error_count = len([check for check in checks if check.status == "error"])
    return AssumptionVerificationReport(
        checks=checks,
        metadata={
            "check_count": len(checks),
            "evaluated_count": evaluated_count,
            "failed_count": failed_count,
            "error_count": error_count,
            "evaluated": connection is not None,
        },
    )


def to_signal(report: AssumptionVerificationReport | dict | None) -> Signal | None:
    normalized_report = _coerce_report(report)
    if normalized_report is None:
        return None

    checks = list(normalized_report.checks or [])
    evaluated_checks = [check for check in checks if check.evaluated]
    if not evaluated_checks:
        return None

    failed_checks = [check for check in evaluated_checks if check.status == "failed"]
    error_checks = [check for check in evaluated_checks if check.status == "error"]

    if failed_checks:
        severity = Severity.HIGH
        confidence = 95
        score = -30
        reasons = [_failure_reason(check) for check in failed_checks]
    elif error_checks:
        severity = Severity.MEDIUM
        confidence = 85
        score = -15
        reasons = [_error_reason(check) for check in error_checks]
    else:
        severity = Severity.LOW
        confidence = 90
        score = 0
        reasons = ["All evaluated assumption checks passed"]

    metadata = {
        **dict(normalized_report.metadata or {}),
        "check_count": len(checks),
        "evaluated_count": len(evaluated_checks),
        "failed_count": len(failed_checks),
        "error_count": len(error_checks),
        "evaluated": True,
        "failed_checks": [check.check_id for check in failed_checks],
        "error_checks": [check.check_id for check in error_checks],
        "failed_assumptions": [_assumption_summary(check) for check in failed_checks],
        "error_assumptions": [_assumption_summary(check) for check in error_checks],
    }

    return Signal(
        component="assumption_verification",
        severity=severity,
        confidence=confidence,
        score=score,
        reasons=reasons,
        metadata=metadata,
    )


def _checks_for_contract(contract: Any, models_by_name: dict[str, dict]) -> list[AssumptionCheck]:
    kpi_name = _contract_value(contract, "kpi_name") or "Unknown KPI"
    related_models = _ordered_unique(_contract_value(contract, "related_models", []) or [])
    related_columns = _ordered_unique(_contract_value(contract, "related_columns", []) or [])
    invariants = _ordered_unique(
        [
            *list(_contract_value(contract, "invariants", []) or []),
            *list(_contract_value(contract, "assumptions", []) or []),
        ]
    )
    checks = []

    for model_name in related_models:
        checks.append(_model_not_empty_check(kpi_name, model_name))
        for column_name in _identifier_columns(model_name, models_by_name):
            checks.append(
                _column_check(
                    kpi_name=kpi_name,
                    model_name=model_name,
                    column_name=column_name,
                    invariant="not null",
                    check_type="not_null",
                    predicate=f"{_sql_identifier(column_name)} IS NULL",
                )
            )

    for invariant in invariants:
        normalized = _normalise(invariant)
        if _is_non_negative_invariant(normalized):
            checks.extend(
                _non_negative_checks(
                    kpi_name,
                    related_models,
                    related_columns,
                    models_by_name,
                    str(invariant),
                )
            )
        elif _is_percentage_invariant(normalized):
            checks.extend(
                _percentage_checks(
                    kpi_name,
                    related_models,
                    related_columns,
                    models_by_name,
                    str(invariant),
                )
            )
        elif _is_not_null_invariant(normalized):
            for model_name in related_models:
                for column_name in related_columns or _identifier_columns(model_name, models_by_name):
                    checks.append(
                        _column_check(
                            kpi_name=kpi_name,
                            model_name=model_name,
                            column_name=column_name,
                            invariant=str(invariant),
                            check_type="not_null",
                            predicate=f"{_sql_identifier(column_name)} IS NULL",
                        )
                    )

    return checks


def _non_negative_checks(
    kpi_name: str,
    related_models: list[str],
    related_columns: list[str],
    models_by_name: dict[str, dict],
    invariant: str,
) -> list[AssumptionCheck]:
    checks = []
    for model_name in related_models:
        columns = _numeric_candidate_columns(model_name, related_columns, models_by_name)
        for column_name in columns:
            checks.append(
                _column_check(
                    kpi_name=kpi_name,
                    model_name=model_name,
                    column_name=column_name,
                    invariant=invariant,
                    check_type="non_negative",
                    predicate=f"{_sql_identifier(column_name)} < 0",
                )
            )
    return checks


def _percentage_checks(
    kpi_name: str,
    related_models: list[str],
    related_columns: list[str],
    models_by_name: dict[str, dict],
    invariant: str,
) -> list[AssumptionCheck]:
    checks = []
    for model_name in related_models:
        columns = _percentage_candidate_columns(model_name, related_columns, models_by_name)
        for column_name in columns:
            identifier = _sql_identifier(column_name)
            checks.append(
                _column_check(
                    kpi_name=kpi_name,
                    model_name=model_name,
                    column_name=column_name,
                    invariant=invariant,
                    check_type="percentage_range",
                    predicate=f"{identifier} < 0 OR {identifier} > 100",
                )
            )
    return checks


def _model_not_empty_check(kpi_name: str, model_name: str) -> AssumptionCheck:
    sql = (
        "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END AS violation_count "
        f"FROM {_sql_identifier(model_name)}"
    )
    return AssumptionCheck(
        check_id=_check_id(kpi_name, model_name, None, "model_not_empty"),
        kpi_name=str(kpi_name),
        model_name=str(model_name),
        column_name=None,
        invariant="model_not_empty",
        check_type="model_not_empty",
        sql=sql,
        metadata={"description": "KPI-related model should not be empty."},
    )


def _column_check(
    *,
    kpi_name: str,
    model_name: str,
    column_name: str,
    invariant: str,
    check_type: str,
    predicate: str,
) -> AssumptionCheck:
    sql = (
        f"SELECT COUNT(*) AS violation_count FROM {_sql_identifier(model_name)} "
        f"WHERE {predicate}"
    )
    return AssumptionCheck(
        check_id=_check_id(kpi_name, model_name, column_name, check_type),
        kpi_name=str(kpi_name),
        model_name=str(model_name),
        column_name=str(column_name),
        invariant=str(invariant),
        check_type=str(check_type),
        sql=sql,
        metadata={"predicate": predicate},
    )


def _evaluate_check(check: AssumptionCheck, connection) -> AssumptionCheck:
    evaluated = copy.deepcopy(check)
    evaluated.evaluated = True
    try:
        cursor = connection.execute(evaluated.sql)
        row = cursor.fetchone()
        violation_count = int(row[0]) if row is not None and row[0] is not None else 0
        evaluated.violation_count = violation_count
        evaluated.passed = violation_count == 0
        evaluated.status = "passed" if evaluated.passed else "failed"
    except Exception as error:  # pragma: no cover - exercised by adapters in the wild
        evaluated.status = "error"
        evaluated.passed = None
        evaluated.violation_count = None
        evaluated.error = str(error)
    return evaluated


def _coerce_report(report: AssumptionVerificationReport | dict | None) -> AssumptionVerificationReport | None:
    if report is None:
        return None
    if isinstance(report, AssumptionVerificationReport):
        return report
    if isinstance(report, dict):
        return AssumptionVerificationReport.from_dict(report)
    return None


def _failure_reason(check: AssumptionCheck) -> str:
    count = check.violation_count if check.violation_count is not None else "unknown"
    noun = "violation" if count == 1 else "violations"
    return f"{check.kpi_name} assumption failed: {_assumption_summary(check)} ({count} {noun})"


def _error_reason(check: AssumptionCheck) -> str:
    return f"{check.kpi_name} assumption check errored: {_assumption_summary(check)}"


def _assumption_summary(check: AssumptionCheck) -> str:
    subject = (
        f"{check.model_name}.{check.column_name}"
        if check.column_name
        else str(check.model_name)
    )
    return f"{subject} {_assumption_label(check)}"


def _assumption_label(check: AssumptionCheck) -> str:
    if check.check_type == "model_not_empty":
        return "has rows"
    if check.check_type == "non_negative":
        return "never negative"
    if check.check_type == "percentage_range":
        return "between 0 and 100"
    if check.check_type == "not_null":
        return "not null"
    return str(check.invariant or check.check_type or "verified")


def _models_by_name(project_context: dict) -> dict[str, dict]:
    models = {}
    for model in list(project_context.get("models") or []):
        if not isinstance(model, dict) or not model.get("name"):
            continue
        models[str(model["name"])] = copy.deepcopy(model)
    return models


def _identifier_columns(model_name: str, models_by_name: dict[str, dict]) -> list[str]:
    columns = _model_columns(model_name, models_by_name)
    return [
        column
        for column in columns
        if _normalise(column).endswith("_id") or _normalise(column) == "id"
    ]


def _numeric_candidate_columns(
    model_name: str,
    related_columns: list[str],
    models_by_name: dict[str, dict],
) -> list[str]:
    available = _model_columns(model_name, models_by_name)
    candidates = [
        column
        for column in related_columns
        if _column_is_available(column, available) and _looks_numeric(column)
    ]
    if candidates:
        return _ordered_unique(candidates)
    return [
        column
        for column in available
        if _looks_numeric(column)
    ]


def _percentage_candidate_columns(
    model_name: str,
    related_columns: list[str],
    models_by_name: dict[str, dict],
) -> list[str]:
    available = _model_columns(model_name, models_by_name)
    candidates = [
        column
        for column in related_columns
        if _column_is_available(column, available) and _looks_percentage(column)
    ]
    if candidates:
        return _ordered_unique(candidates)
    return [
        column
        for column in available
        if _looks_percentage(column)
    ]


def _model_columns(model_name: str, models_by_name: dict[str, dict]) -> list[str]:
    model = models_by_name.get(str(model_name)) or {}
    raw_columns = model.get("columns") or []
    if isinstance(raw_columns, dict):
        return [str(column) for column in raw_columns.keys()]
    return [
        str(column.get("name") if isinstance(column, dict) else column)
        for column in list(raw_columns or [])
        if column
    ]


def _column_is_available(column: str, available: list[str]) -> bool:
    if not available:
        return True
    available_names = {item.casefold() for item in available}
    return str(column).casefold() in available_names


def _looks_numeric(column: str) -> bool:
    text = _normalise(column)
    return any(
        token in text
        for token in [
            "revenue",
            "amount",
            "total",
            "value",
            "gmv",
            "mrr",
            "arr",
            "price",
            "cost",
            "rate",
            "percent",
            "percentage",
        ]
    )


def _looks_percentage(column: str) -> bool:
    text = _normalise(column)
    return any(token in text for token in ["rate", "percent", "percentage", "pct"])


def _contract_value(contract: Any, key: str, default=None):
    if isinstance(contract, dict):
        return contract.get(key, default)
    return getattr(contract, key, default)


def _is_non_negative_invariant(normalized: str) -> bool:
    return normalized in {"never_negative", "non_negative"} or "negative" in normalized


def _is_percentage_invariant(normalized: str) -> bool:
    return (
        "between_0_and_100" in normalized
        or "0_and_100" in normalized
        or ("percent" in normalized and "100" in normalized)
    )


def _is_not_null_invariant(normalized: str) -> bool:
    return "not_null" in normalized or "non_null" in normalized or "cannot_be_null" in normalized


def _deduplicate_checks(checks: list[AssumptionCheck]) -> list[AssumptionCheck]:
    deduped = []
    seen = set()
    for check in checks:
        key = (check.model_name, check.column_name, check.check_type, check.sql)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(check)
    return deduped


def _check_id(kpi_name: str, model_name: str, column_name: str | None, check_type: str) -> str:
    parts = [kpi_name, model_name, column_name or "model", check_type]
    slug = "_".join(_normalise(part) for part in parts if part)
    return re.sub(r"_+", "_", slug).strip("_")


def _sql_identifier(value: str) -> str:
    text = str(value)
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text):
        return text
    return '"' + text.replace('"', '""') + '"'


def _normalise(value: Any) -> str:
    text = str(value).lower()
    chars = []
    previous_was_separator = False
    for char in text:
        if char.isalnum():
            chars.append(char)
            previous_was_separator = False
        elif not previous_was_separator:
            chars.append("_")
            previous_was_separator = True
    return "".join(chars).strip("_")


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in list(values or []):
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _serializable(value):
    if is_dataclass(value):
        return _serializable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _serializable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value
