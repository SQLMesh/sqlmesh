# SPDX-License-Identifier: Apache-2.0
"""Immutable, engine-neutral context for SQL query execution."""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import datetime as dt
import json
import logging
import typing as t
from dataclasses import dataclass
from enum import Enum
from functools import cached_property

from sqlmesh.utils import CorrelationId, JobType

logger = logging.getLogger(__name__)

MAX_QUERY_EXECUTION_IDENTIFIER_BYTES = 1024
MAX_QUERY_EXECUTION_CONTEXT_BYTES = 8192
QUERY_EXECUTION_CONTEXT_PREFIX = "/* QUERY_CONTEXT: "


class QueryExecutionPhase(str, Enum):
    EXECUTE = "execute"
    MATERIALIZE = "materialize"
    AUDIT = "audit"
    DDL = "ddl"
    CLEANUP = "cleanup"
    METADATA = "metadata"
    UNKNOWN = "unknown"


class QueryExecutionOperation(str, Enum):
    EVALUATE = "evaluate"
    CREATE = "create"
    MIGRATE = "migrate"
    PROMOTE = "promote"
    DEMOTE = "demote"
    CLEANUP = "cleanup"
    AUDIT = "audit"
    UNKNOWN = "unknown"


class QueryExecutionRole(str, Enum):
    QUERY = "query"
    CREATE_TABLE = "create_table"
    INSERT = "insert"
    MERGE = "merge"
    REPLACE = "replace"
    AUDIT = "audit"
    DROP = "drop"
    INSPECT = "inspect"
    PRE_STATEMENT = "pre_statement"
    POST_STATEMENT = "post_statement"
    PROMOTE = "promote"
    DEMOTE = "demote"


def _validate_identifier(name: str, value: t.Any) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _validate_datetime(name: str, value: t.Optional[dt.datetime]) -> None:
    if value is None:
        return
    if not isinstance(value, dt.datetime):
        raise TypeError(f"{name} must be a datetime or None")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class ModelExecutionContext:
    evaluation_id: str
    model_name: str
    snapshot_id: str
    interval_start: t.Optional[dt.datetime] = None
    interval_end: t.Optional[dt.datetime] = None
    batch_index: t.Optional[int] = None
    operation_type: QueryExecutionOperation = QueryExecutionOperation.EVALUATE

    def __post_init__(self) -> None:
        _validate_identifier("evaluation_id", self.evaluation_id)
        _validate_identifier("model_name", self.model_name)
        _validate_identifier("snapshot_id", self.snapshot_id)
        if not isinstance(self.operation_type, QueryExecutionOperation):
            raise TypeError("operation_type must be a QueryExecutionOperation")
        _validate_datetime("interval_start", self.interval_start)
        _validate_datetime("interval_end", self.interval_end)
        if self.interval_start is not None:
            object.__setattr__(
                self, "interval_start", self.interval_start.astimezone(dt.timezone.utc)
            )
        if self.interval_end is not None:
            object.__setattr__(self, "interval_end", self.interval_end.astimezone(dt.timezone.utc))
        if self.interval_start is not None and self.interval_end is not None:
            if self.interval_start >= self.interval_end:
                raise ValueError("interval_start must be earlier than interval_end")
        if self.batch_index is not None:
            if type(self.batch_index) is not int:
                raise TypeError("batch_index must be an integer or None")
            if self.batch_index < 0:
                raise ValueError("batch_index must be nonnegative")


@dataclass(frozen=True)
class QueryExecutionContext:
    parent: t.Optional[CorrelationId] = None
    evaluation: t.Optional[ModelExecutionContext] = None
    phase: QueryExecutionPhase = QueryExecutionPhase.UNKNOWN
    role: QueryExecutionRole = QueryExecutionRole.QUERY
    schema_version: int = 1

    @cached_property
    def _serialized(self) -> t.Optional[str]:
        """Return the cached transport representation derived from this value."""
        return _serialize_query_execution_context(self)

    def __post_init__(self) -> None:
        if self.parent is not None:
            if not isinstance(self.parent, CorrelationId):
                raise TypeError("parent must be a CorrelationId or None")
            if not isinstance(self.parent.job_type, JobType):
                raise TypeError("parent.job_type must be a JobType")
            if self.parent.job_type not in (JobType.PLAN, JobType.RUN):
                raise ValueError("parent.job_type must be a plan or run job")
            _validate_identifier("parent.job_id", self.parent.job_id)
        if self.evaluation is not None and not isinstance(self.evaluation, ModelExecutionContext):
            raise TypeError("evaluation must be a ModelExecutionContext or None")
        if not isinstance(self.phase, QueryExecutionPhase):
            raise TypeError("phase must be a QueryExecutionPhase")
        if not isinstance(self.role, QueryExecutionRole):
            raise TypeError("role must be a QueryExecutionRole")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an integer")
        if self.schema_version != 1:
            raise ValueError("schema_version is fixed at 1")


_current_query_execution_context: contextvars.ContextVar[t.Optional[QueryExecutionContext]] = (
    contextvars.ContextVar("query_execution_context", default=None)
)
_query_execution_context_transport_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "query_execution_context_transport_active", default=False
)


def get_query_execution_context() -> t.Optional[QueryExecutionContext]:
    """Return the query execution context active in this context, if any."""
    return _current_query_execution_context.get()


def _query_execution_context_transport_is_active() -> bool:
    return _query_execution_context_transport_active.get()


@contextlib.contextmanager
def _query_execution_context_transport_scope() -> t.Iterator[None]:
    token = _query_execution_context_transport_active.set(True)
    try:
        yield
    finally:
        _query_execution_context_transport_active.reset(token)


@contextlib.contextmanager
def query_execution_scope(context: QueryExecutionContext) -> t.Iterator[None]:
    """Set a query execution context for this scope and restore it on exit."""
    if not isinstance(context, QueryExecutionContext):
        raise TypeError("context must be a QueryExecutionContext")
    token = _current_query_execution_context.set(context)
    try:
        yield
    finally:
        _current_query_execution_context.reset(token)


@contextlib.contextmanager
def query_phase_scope(phase: QueryExecutionPhase, role: QueryExecutionRole) -> t.Iterator[None]:
    """Temporarily derive the active context with a new phase and query role."""
    if not isinstance(phase, QueryExecutionPhase):
        raise TypeError("phase must be a QueryExecutionPhase")
    if not isinstance(role, QueryExecutionRole):
        raise TypeError("role must be a QueryExecutionRole")
    current = get_query_execution_context()
    if current is None:
        yield
        return
    token = _current_query_execution_context.set(
        dataclasses.replace(current, phase=phase, role=role)
    )
    try:
        yield
    finally:
        _current_query_execution_context.reset(token)


def serialize_query_execution_context(
    context: QueryExecutionContext,
) -> t.Optional[str]:
    """Serialize a context as one complete, bounded SQL comment.

    The transport has no truncation mode: a context with overlong identifiers
    or an oversized encoded payload is omitted and logged without its contents.
    """
    if not isinstance(context, QueryExecutionContext):
        raise TypeError("context must be a QueryExecutionContext")

    return context._serialized


def _serialize_query_execution_context(
    context: QueryExecutionContext,
) -> t.Optional[str]:
    payload: t.Dict[str, t.Any] = {"schema_version": context.schema_version, "emitter": "sqlmesh"}
    if context.parent is not None:
        payload["parent"] = {
            "system": "sqlmesh",
            "type": "plan" if context.parent.job_type is JobType.PLAN else "run",
            "id": context.parent.job_id,
        }
    if context.evaluation is not None:
        evaluation = context.evaluation
        payload["workload"] = {
            "system": "sqlmesh",
            "type": "model",
            "name": evaluation.model_name,
            "version": evaluation.snapshot_id,
        }
        payload["operation"] = {
            "id": evaluation.evaluation_id,
            "type": evaluation.operation_type.value,
        }
        if evaluation.interval_start is not None:
            payload["operation"]["interval_start"] = evaluation.interval_start.astimezone(
                dt.timezone.utc
            ).isoformat()
        if evaluation.interval_end is not None:
            payload["operation"]["interval_end"] = evaluation.interval_end.astimezone(
                dt.timezone.utc
            ).isoformat()
        if evaluation.batch_index is not None:
            payload["operation"]["batch_index"] = evaluation.batch_index
    payload["query"] = {"phase": context.phase.value, "role": context.role.value}

    identifiers = []
    if context.parent is not None:
        identifiers.append(context.parent.job_id)
    if context.evaluation is not None:
        identifiers.extend(
            (
                context.evaluation.evaluation_id,
                context.evaluation.model_name,
                context.evaluation.snapshot_id,
            )
        )
    identifier_sizes: t.List[int] = []
    has_unencodable_identifier = False
    for value in identifiers:
        try:
            identifier_sizes.append(len(value.encode("utf-8")))
        except UnicodeEncodeError:
            has_unencodable_identifier = True
    if has_unencodable_identifier or any(
        size > MAX_QUERY_EXECUTION_IDENTIFIER_BYTES for size in identifier_sizes
    ):
        if has_unencodable_identifier:
            logger.warning(
                "Omitting SQLMesh query execution context: identifier cannot be encoded as UTF-8 "
                "(identifier transport limit %s UTF-8 bytes)",
                MAX_QUERY_EXECUTION_IDENTIFIER_BYTES,
            )
        else:
            logger.warning(
                "Omitting SQLMesh query execution context: identifier is %s UTF-8 bytes "
                "(transport limit %s UTF-8 bytes)",
                max(identifier_sizes),
                MAX_QUERY_EXECUTION_IDENTIFIER_BYTES,
            )
        return None

    # JSON escapes controls and quotes; escaping slash prevents either comment
    # delimiter in an identifier from affecting the surrounding SQL comment.
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("/", r"\u002f")
    comment = f"{QUERY_EXECUTION_CONTEXT_PREFIX}{encoded} */ "
    comment_bytes = len(comment.encode("utf-8"))
    if comment_bytes > MAX_QUERY_EXECUTION_CONTEXT_BYTES:
        logger.warning(
            "Omitting SQLMesh query execution context: comment is %s UTF-8 bytes "
            "(transport limit %s UTF-8 bytes)",
            comment_bytes,
            MAX_QUERY_EXECUTION_CONTEXT_BYTES,
        )
        return None
    return comment
