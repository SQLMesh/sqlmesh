from __future__ import annotations

import typing as t
from enum import auto
from pathlib import Path

from sqlglot import exp
from sqlglot.helper import AutoName

if t.TYPE_CHECKING:
    from requests.models import Response

    from sqlmesh.core.model import Model
    from sqlmesh.core.schema_diff import TableAlterOperation


class ErrorLevel(AutoName):
    IGNORE = auto()
    WARN = auto()
    RAISE = auto()


class SQLMeshError(Exception):
    pass


class ConfigError(SQLMeshError):
    location: t.Optional[Path] = None

    def __init__(self, message: str | Exception, location: t.Optional[Path] = None) -> None:
        super().__init__(message)
        if location:
            self.location = Path(location) if isinstance(location, str) else location


class BaseMissingReferenceError(ConfigError):
    def __init__(self, ref: str) -> None:
        self.ref = ref


class MissingModelError(BaseMissingReferenceError):
    """Raised when a model that is referenced is missing."""


class MissingSourceError(BaseMissingReferenceError):
    """Raised when a source that is referenced is missing."""


class MissingDependencyError(SQLMeshError):
    """Local environment is missing a required dependency for the given operation"""


class MacroEvalError(SQLMeshError):
    pass


class PlanError(SQLMeshError):
    pass


class NoChangesPlanError(PlanError):
    pass


class UncategorizedPlanError(PlanError):
    pass


class ConflictingPlanError(PlanError):
    pass


class MissingContextException(Exception):
    pass


class SnapshotVersionError(SQLMeshError):
    pass


class MagicError(SQLMeshError):
    pass


class AuditConfigError(ConfigError):
    pass


class StateMigrationError(SQLMeshError):
    pass


class AuditError(SQLMeshError):
    def __init__(
        self,
        audit_name: str,
        audit_args: t.Dict[t.Any, t.Any],
        count: int,
        query: exp.Query,
        model: t.Optional[Model] = None,
        # the dialect of the engine adapter that evaluated the audit query
        adapter_dialect: t.Optional[str] = None,
    ) -> None:
        self.audit_name = audit_name
        self.audit_args = audit_args
        self.model = model
        self.count = count
        self.query = query
        self.adapter_dialect = adapter_dialect

        super().__init__(
            f"'{self.audit_name}' audit error: {self.count} {'row' if self.count == 1 else 'rows'} failed"
        )

    @property
    def model_name(self) -> t.Optional[str]:
        return self.model.name if self.model else None

    def sql(self, dialect: t.Optional[str] = None, **opts: t.Any) -> str:
        """
        Returns the rendered audit query that failed.

        Args:
            dialect: the dialect of the output SQL string, by default,
                     this will use the dialect of the engine adapter that ran the query.
            opts: other `sqlglot.generator.Generator` options.

        Returns:
            The SQL string.
        """
        return self.query.sql(dialect=dialect or self.adapter_dialect, **opts)


class NodeAuditsErrors(SQLMeshError):
    def __init__(self, errors: t.List[AuditError]) -> None:
        self.errors = errors

        super().__init__(f"Audits failed: {', '.join([e.audit_name for e in errors])}")


class TestError(SQLMeshError):
    __test__ = False  # prevent pytest trying to collect this as a test class
    pass


class DestructiveChangeError(SQLMeshError):
    pass


class AdditiveChangeError(SQLMeshError):
    pass


class MigrationNotSupportedError(SQLMeshError):
    pass


class NotificationTargetError(SQLMeshError):
    pass


class ApiError(SQLMeshError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class ApiClientError(ApiError):
    pass


class ApiServerError(ApiError):
    pass


class NotFoundError(ApiClientError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 404)


class CICDBotError(SQLMeshError):
    pass


class ParsetimeAdapterCallError(SQLMeshError):
    pass


class EngineAdapterError(SQLMeshError):
    pass


class QueryHistoryPermissionError(EngineAdapterError):
    def __init__(
        self,
        message: str,
        remediation: t.Optional[str] = None,
        docs_url: t.Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.remediation = remediation
        self.docs_url = docs_url

    def __str__(self) -> str:
        # The CLI only surfaces str(exc), so fold the remediation and docs into the message.
        parts = [super().__str__()]
        if self.remediation:
            parts.append(f"\n\nTo grant access, run:\n  {self.remediation}")
        if self.docs_url:
            parts.append(f"\n\nDocs: {self.docs_url}")
        return "".join(parts)


class UnsupportedCatalogOperationError(EngineAdapterError):
    pass


class CircuitBreakerError(SQLMeshError):
    def __init__(self) -> None:
        super().__init__("Circuit breaker triggered.")


class PythonModelEvalError(SQLMeshError):
    pass


class MissingDefaultCatalogError(SQLMeshError):
    pass


class LinterError(SQLMeshError):
    pass


class SignalEvalError(SQLMeshError):
    """Errors when evaluating a signal that is because of a user mistake and not a SQLMesh bug."""

    pass


def raise_config_error(
    msg: str,
    location: t.Optional[Path] = None,
    error_type: t.Type[ConfigError] = ConfigError,
) -> None:
    if location:
        raise error_type(f"{msg} at '{location}'", location)
    raise error_type(msg, location=location)


def raise_for_status(response: Response) -> None:
    if response.status_code == 404:
        raise NotFoundError(response.text)
    if 400 <= response.status_code < 500:
        raise ApiClientError(response.text, response.status_code)
    if 500 <= response.status_code < 600:
        raise ApiServerError(response.text, response.status_code)


def _format_schema_change_msg(
    snapshot_name: str,
    is_destructive: bool,
    alter_operations: t.List[TableAlterOperation],
    dialect: str,
    error: bool = True,
) -> str:
    """
    Common function to format schema change messages.

    Args:
        snapshot_name: Name of the model/snapshot
        is_destructive: if change is destructive else it would be additive
        alter_operations: List of table alter operations
        dialect: SQL dialect for formatting
        error: Whether this is an error or warning
    """
    from sqlmesh.core.schema_diff import get_dropped_column_names, get_additive_column_names

    change_type = "destructive" if is_destructive else "additive"
    setting_name = "on_destructive_change" if is_destructive else "on_additive_change"
    action_verb = "drops" if is_destructive else "adds"
    cli_flag = "--allow-destructive-model" if is_destructive else "--allow-additive-model"

    column_names = (
        get_dropped_column_names(alter_operations)
        if is_destructive
        else get_additive_column_names(alter_operations)
    )
    column_str = "', '".join(column_names)
    column_msg = (
        f" that {action_verb} column{'s' if column_names and len(column_names) > 1 else ''} '{column_str}'"
        if column_str
        else ""
    )

    # Format ALTER expressions
    alter_expr_msg = "\n\nSchema changes:\n  " + "\n  ".join(
        [alter.expression.sql(dialect) for alter in alter_operations]
    )

    # Main warning message
    warning_msg = (
        f"Plan requires {change_type} change to forward-only model '{snapshot_name}'s schema"
    )

    if error:
        permissive_values = "`warn`, `allow`, or `ignore`"
        cli_part = f" or include the model in the plan's `{cli_flag}` option"
        err_msg = f"\n\nTo allow the {change_type} change, set the model's `{setting_name}` setting to {permissive_values}{cli_part}.\n"
    else:
        err_msg = ""

    return f"\n{warning_msg}{column_msg}.{alter_expr_msg}{err_msg}"


def format_destructive_change_msg(
    snapshot_name: str,
    alter_expressions: t.List[TableAlterOperation],
    dialect: str,
    error: bool = True,
) -> str:
    return _format_schema_change_msg(
        snapshot_name=snapshot_name,
        is_destructive=True,
        alter_operations=alter_expressions,
        dialect=dialect,
        error=error,
    )


def format_additive_change_msg(
    snapshot_name: str,
    alter_operations: t.List[TableAlterOperation],
    dialect: str,
    error: bool = True,
) -> str:
    return _format_schema_change_msg(
        snapshot_name=snapshot_name,
        is_destructive=False,
        alter_operations=alter_operations,
        dialect=dialect,
        error=error,
    )
