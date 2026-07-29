import re
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from agent.signals import Severity


@dataclass(frozen=True)
class FailureDiagnosis:
    category: str
    root_cause: str
    explanation: str
    evidence: tuple[str, ...]
    recommendation: str
    severity: Severity
    confidence: int
    affected_model: str | None = None
    affected_file: str | None = None
    affected_line: str | None = None
    data_loss_risk: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int):
            raise TypeError("confidence must be an integer")
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if not isinstance(self.severity, Severity):
            object.__setattr__(self, "severity", Severity(str(self.severity).upper()))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(deepcopy(dict(self.metadata))),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the canonical diagnosis at an explicit adapter boundary."""
        return {
            "category": self.category,
            "root_cause": self.root_cause,
            "explanation": self.explanation,
            "evidence": list(self.evidence),
            "recommendation": self.recommendation,
            "severity": self.severity.value,
            "confidence": self.confidence,
            "affected_model": self.affected_model,
            "affected_file": self.affected_file,
            "affected_line": self.affected_line,
            "data_loss_risk": self.data_loss_risk,
            "metadata": deepcopy(dict(self.metadata)),
        }


def diagnose_failure(
    error_log: str,
    model_sql: str,
    upstream_schema: str,
) -> FailureDiagnosis:
    """Deterministically classify common dbt and SQL failure messages."""
    original_error = error_log or ""
    normalized_error = original_error.lower()
    base_evidence = (original_error.strip(),) if original_error.strip() else ()

    if re.search(
        r"column\s+\"?[\w_]+\"?\s+does not exist"
        r"|column\s+[\w_]+\s+not found"
        r"|unknown column",
        normalized_error,
    ):
        match = re.search(r"column\s+\"?([\w_]+)\"?", original_error, re.I)
        column = match.group(1) if match else None
        evidence = base_evidence + _column_context_evidence(
            column,
            model_sql,
            upstream_schema,
        )
        return FailureDiagnosis(
            category="column_not_found",
            root_cause=(
                f"Referenced column {column} was not found"
                if column
                else "Referenced column was not found"
            ),
            explanation=(
                "The database rejected a column reference because that column "
                "was unavailable in the queried relation."
            ),
            evidence=evidence,
            recommendation=(
                "Check the upstream model or schema for a renamed or missing "
                "column, then update the SQL to use an available column."
            ),
            severity=Severity.HIGH,
            confidence=90,
        )

    if re.search(
        r"relation\s+\"?[\w\.]+\"?\s+does not exist"
        r"|table\s+[\w_]+\s+does not exist"
        r"|unknown relation",
        normalized_error,
    ):
        return FailureDiagnosis(
            category="table_not_found",
            root_cause="Referenced table or relation does not exist",
            explanation=(
                "The database could not resolve a table or relation referenced "
                "by the failing query."
            ),
            evidence=base_evidence,
            recommendation=(
                "Verify relation names, database and schema selection, and that "
                "required upstream models have run."
            ),
            severity=Severity.HIGH,
            confidence=90,
        )

    if "syntax error" in normalized_error or re.search(
        r"parse error|syntax error at or near",
        normalized_error,
    ):
        return FailureDiagnosis(
            category="syntax_error",
            root_cause="SQL syntax error in the model",
            explanation="The database parser rejected the submitted SQL.",
            evidence=base_evidence,
            recommendation=(
                "Inspect the SQL around the reported token or location and "
                "validate the statement with the target SQL dialect."
            ),
            severity=Severity.HIGH,
            confidence=90,
        )

    if re.search(
        r"cannot cast|invalid input syntax for|type mismatch|cannot convert",
        normalized_error,
    ):
        return FailureDiagnosis(
            category="type_mismatch",
            root_cause="Data type mismatch or invalid cast",
            explanation=(
                "A value or expression could not be converted to the required "
                "data type."
            ),
            evidence=base_evidence,
            recommendation=(
                "Check upstream column types and make the intended conversion "
                "explicit."
            ),
            severity=Severity.MEDIUM,
            confidence=70,
        )

    if re.search(
        r"permission denied|access denied|insufficient privileges",
        normalized_error,
    ):
        return FailureDiagnosis(
            category="permission_error",
            root_cause="Insufficient privileges to access the resource",
            explanation=(
                "The active database identity was denied the requested operation."
            ),
            evidence=base_evidence,
            recommendation=(
                "Verify warehouse credentials, role grants, and permissions for "
                "the referenced resource."
            ),
            severity=Severity.HIGH,
            confidence=90,
        )

    if re.search(
        r"column reference.*is ambiguous|ambiguous column",
        normalized_error,
    ):
        return FailureDiagnosis(
            category="ambiguous_column",
            root_cause="A column reference is ambiguous",
            explanation=(
                "More than one input relation exposes the referenced column name."
            ),
            evidence=base_evidence,
            recommendation=(
                "Qualify the column with its table or alias and disambiguate "
                "overlapping names."
            ),
            severity=Severity.MEDIUM,
            confidence=90,
        )

    if re.search(r"division by zero|divide.*zero", normalized_error):
        return FailureDiagnosis(
            category="division_by_zero",
            root_cause="Division by zero was encountered",
            explanation="A denominator evaluated to zero during query execution.",
            evidence=base_evidence,
            recommendation=(
                "Guard the denominator with NULLIF or explicit conditional logic."
            ),
            severity=Severity.HIGH,
            confidence=90,
        )

    if re.search(
        r"null value in column.*violates not-null constraint"
        r"|cannot be null"
        r"|not null constraint",
        normalized_error,
    ):
        return FailureDiagnosis(
            category="not_null_violation",
            root_cause="A NOT NULL constraint was violated",
            explanation=(
                "Incoming data contained a null value where the target requires "
                "a non-null value."
            ),
            evidence=base_evidence,
            recommendation=(
                "Ensure upstream transformations provide a value or revise the "
                "constraint if nulls are valid."
            ),
            severity=Severity.HIGH,
            confidence=90,
        )

    return FailureDiagnosis(
        category="unknown_error",
        root_cause="The failure did not match a known deterministic category",
        explanation=(
            "The available error text was insufficient for a more specific "
            "deterministic diagnosis."
        ),
        evidence=base_evidence,
        recommendation=(
            "Review the dbt error message and reproduce the failing model locally."
        ),
        severity=Severity.MEDIUM,
        confidence=40,
    )


def _column_context_evidence(
    column: str | None,
    model_sql: str,
    upstream_schema: str,
) -> tuple[str, ...]:
    if not column:
        return ()

    evidence: list[str] = []
    column_pattern = re.compile(rf"\b{re.escape(column)}\b", re.I)
    if model_sql and column_pattern.search(model_sql):
        evidence.append(f"Model SQL references column '{column}'.")

    normalized_schema = upstream_schema or ""
    if (
        re.search(r"(?im)^\s*columns?\s*:", normalized_schema)
        and not column_pattern.search(normalized_schema)
    ):
        evidence.append(
            f"Provided upstream schema does not list column '{column}'."
        )

    return tuple(evidence)
