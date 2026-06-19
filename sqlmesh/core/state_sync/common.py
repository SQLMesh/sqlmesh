from __future__ import annotations

import logging
import typing as t
from functools import wraps
import itertools
import abc

from dataclasses import dataclass

from pydantic_core.core_schema import ValidationInfo
from sqlglot import exp

from sqlmesh.utils.pydantic import PydanticModel, field_validator, validation_data
from sqlmesh.core.environment import Environment, EnvironmentStatements, EnvironmentNamingInfo
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotId,
    SnapshotTableCleanupTask,
    SnapshotTableInfo,
)

if t.TYPE_CHECKING:
    from sqlmesh.core.state_sync.base import Versions, StateReader

logger = logging.getLogger(__name__)

EXPIRED_SNAPSHOT_DEFAULT_BATCH_SIZE = 200


def transactional() -> t.Callable[[t.Callable], t.Callable]:
    def decorator(func: t.Callable) -> t.Callable:
        @wraps(func)
        def wrapper(self: t.Any, *args: t.Any, **kwargs: t.Any) -> t.Any:
            if not hasattr(self, "_transaction"):
                return func(self, *args, **kwargs)

            with self._transaction():
                return func(self, *args, **kwargs)

        return wrapper

    return decorator


T = t.TypeVar("T")


def chunk_iterable(iterable: t.Iterable[T], size: int = 10) -> t.Iterable[t.Iterable[T]]:
    iterator = iter(iterable)
    for first in iterator:
        yield itertools.chain([first], itertools.islice(iterator, size - 1))


class EnvironmentWithStatements(PydanticModel):
    environment: Environment
    statements: t.List[EnvironmentStatements] = []


@dataclass
class VersionsChunk:
    versions: Versions


class SnapshotsChunk:
    def __init__(self, items: t.Iterator[Snapshot]):
        self.items = items

    def __iter__(self) -> t.Iterator[Snapshot]:
        return self.items


class EnvironmentsChunk:
    def __init__(self, items: t.Iterator[EnvironmentWithStatements]):
        self.items = items

    def __iter__(self) -> t.Iterator[EnvironmentWithStatements]:
        return self.items


StateStreamContents = t.Union[VersionsChunk, SnapshotsChunk, EnvironmentsChunk]


class StateStream(abc.ABC):
    """
    Represents a stream of state either going into the StateSync (perhaps loaded from a file)
    or out of the StateSync (perhaps being dumped to a file)

    Iterating over the stream produces the following chunks:

        VersionsChunk: The versions of the objects contained in this StateStream
        SnapshotsChunk: Is itself an iterator that streams Snapshot objects. Note that they should be fully populated with any relevant Intervals
        EnvironmentsChunk: Is itself an iterator emitting a stream of Environments with any EnvironmentStatements attached

    The idea here is to give some structure to the stream and ensure that callers have the opportunity to process all its components while not
    needing to worry about the order they are emitted in
    """

    @abc.abstractmethod
    def __iter__(self) -> t.Iterator[StateStreamContents]:
        pass

    @classmethod
    def from_iterators(
        cls: t.Type["StateStream"],
        versions: Versions,
        snapshots: t.Iterator[Snapshot],
        environments: t.Iterator[EnvironmentWithStatements],
    ) -> "StateStream":
        class _StateStream(cls):  # type: ignore
            def __iter__(self) -> t.Iterator[StateStreamContents]:
                yield VersionsChunk(versions)

                yield SnapshotsChunk(snapshots)

                yield EnvironmentsChunk(environments)

        return _StateStream()


class ExpiredBatchRange(PydanticModel):
    start: RowBoundary
    end: t.Union[RowBoundary, LimitBoundary]

    @classmethod
    def init_batch_range(cls, batch_size: int) -> ExpiredBatchRange:
        return ExpiredBatchRange(
            start=RowBoundary.lowest_boundary(),
            end=LimitBoundary(batch_size=batch_size),
        )

    @classmethod
    def all_batch_range(cls) -> ExpiredBatchRange:
        return ExpiredBatchRange(
            start=RowBoundary.lowest_boundary(),
            end=RowBoundary.highest_boundary(),
        )

    @classmethod
    def _expanded_tuple_comparison(
        cls,
        columns: t.List[exp.Column],
        values: t.List[t.Union[exp.Literal, exp.Neg]],
        operator: t.Type[exp.Expr],
    ) -> exp.Condition:
        """Generate expanded tuple comparison that works across all SQL engines.

        Converts tuple comparisons like (a, b, c) OP (x, y, z) into an expanded form
        that's compatible with all SQL engines, since native tuple comparisons have
        inconsistent support across engines (especially DuckDB, MySQL, SQLite).

        Repro of problem with DuckDB:
            "SELECT * FROM VALUES(1,'2') as test(a,b) WHERE ((a, b) > (1, 'foo')) AND ((a, b) <= (10, 'baz'))"

        Args:
            columns: List of column expressions to compare
            values: List of value expressions to compare against
            operator: The comparison operator class (exp.GT, exp.GTE, exp.LT, exp.LTE)

        Examples:
            (a, b, c) > (x, y, z) expands to:
                a > x OR (a = x AND b > y) OR (a = x AND b = y AND c > z)

            (a, b, c) <= (x, y, z) expands to:
                a < x OR (a = x AND b < y) OR (a = x AND b = y AND c <= z)

            (a, b, c) >= (x, y, z) expands to:
                a > x OR (a = x AND b > y) OR (a = x AND b = y AND c >= z)

        Returns:
            An expanded OR expression representing the tuple comparison
        """
        if operator not in (exp.GT, exp.GTE, exp.LT, exp.LTE):
            raise ValueError(f"Unsupported operator: {operator}. Use GT, GTE, LT, or LTE.")

        # For <= and >=, we use the strict operator for all but the last column
        # e.g., (a, b) <= (x, y) becomes: a < x OR (a = x AND b <= y)
        # For < and >, we use the strict operator throughout
        # e.g., (a, b) > (x, y) becomes: a > x OR (a = x AND b > x)
        strict_operator: t.Type[exp.Expr]
        final_operator: t.Type[exp.Expr]

        if operator in (exp.LTE, exp.GTE):
            # For inclusive operators (<=, >=), use strict form for intermediate columns
            # but keep inclusive form for the last column
            strict_operator = exp.LT if operator == exp.LTE else exp.GT
            final_operator = operator  # Keep LTE/GTE for last column
        else:
            # For strict operators (<, >), use them throughout
            strict_operator = operator
            final_operator = operator

        conditions: t.List[exp.Expr] = []
        for i in range(len(columns)):
            # Build equality conditions for all columns before current
            equality_conditions = [exp.EQ(this=columns[j], expression=values[j]) for j in range(i)]

            # Use the final operator for the last column, strict for others
            comparison_op = final_operator if i == len(columns) - 1 else strict_operator
            comparison_condition = comparison_op(this=columns[i], expression=values[i])

            if equality_conditions:
                conditions.append(exp.and_(*equality_conditions, comparison_condition))
            else:
                conditions.append(comparison_condition)

        return exp.or_(*conditions) if len(conditions) > 1 else t.cast(exp.Condition, conditions[0])

    @property
    def where_filter(self) -> exp.Condition:
        # Use expanded tuple comparisons for cross-engine compatibility
        # Native tuple comparisons like (a, b) > (x, y) don't work reliably across all SQL engines
        columns = [
            exp.column("updated_ts"),
            exp.column("name"),
            exp.column("identifier"),
        ]
        start_values = [
            exp.Literal.number(self.start.updated_ts),
            exp.Literal.string(self.start.name),
            exp.Literal.string(self.start.identifier),
        ]

        start_condition = self._expanded_tuple_comparison(columns, start_values, exp.GT)

        range_filter: exp.Condition
        if isinstance(self.end, RowBoundary):
            end_values = [
                exp.Literal.number(self.end.updated_ts),
                exp.Literal.string(self.end.name),
                exp.Literal.string(self.end.identifier),
            ]
            end_condition = self._expanded_tuple_comparison(columns, end_values, exp.LTE)
            range_filter = exp.and_(start_condition, end_condition)
        else:
            range_filter = start_condition
        return range_filter


class RowBoundary(PydanticModel):
    updated_ts: int
    name: str
    identifier: str

    @classmethod
    def lowest_boundary(cls) -> RowBoundary:
        return RowBoundary(updated_ts=0, name="", identifier="")

    @classmethod
    def highest_boundary(cls) -> RowBoundary:
        # 9999-12-31T23:59:59.999Z in epoch milliseconds
        return RowBoundary(updated_ts=253_402_300_799_999, name="", identifier="")


class LimitBoundary(PydanticModel):
    batch_size: int

    @classmethod
    def init_batch_boundary(cls, batch_size: int) -> LimitBoundary:
        return LimitBoundary(batch_size=batch_size)


class PromotionResult(PydanticModel):
    added: t.List[SnapshotTableInfo]
    removed: t.List[SnapshotTableInfo]
    removed_environment_naming_info: t.Optional[EnvironmentNamingInfo]

    @field_validator("removed_environment_naming_info")
    def _validate_removed_environment_naming_info(
        cls, v: t.Optional[EnvironmentNamingInfo], info: ValidationInfo
    ) -> t.Optional[EnvironmentNamingInfo]:
        if v and not validation_data(info).get("removed"):
            raise ValueError("removed_environment_naming_info must be None if removed is empty")
        return v


class ExpiredSnapshotBatch(PydanticModel):
    """A batch of expired snapshots to be cleaned up."""

    expired_snapshot_ids: t.Set[SnapshotId]
    cleanup_tasks: t.List[SnapshotTableCleanupTask]
    batch_range: ExpiredBatchRange


def iter_expired_snapshot_batches(
    state_reader: StateReader,
    *,
    current_ts: int,
    ignore_ttl: bool = False,
    batch_size: t.Optional[int] = None,
) -> t.Iterator[ExpiredSnapshotBatch]:
    """Yields expired snapshot batches.

    Args:
        state_reader: StateReader instance to query expired snapshots from.
        current_ts: Timestamp used to evaluate expiration.
        ignore_ttl: If True, include snapshots regardless of TTL (only checks if unreferenced).
        batch_size: Maximum number of snapshots to fetch per batch.
    """

    batch_size = batch_size if batch_size is not None else EXPIRED_SNAPSHOT_DEFAULT_BATCH_SIZE
    batch_range = ExpiredBatchRange.init_batch_range(batch_size=batch_size)

    while True:
        batch = state_reader.get_expired_snapshots(
            current_ts=current_ts,
            ignore_ttl=ignore_ttl,
            batch_range=batch_range,
        )

        if batch is None:
            return

        yield batch

        assert isinstance(batch.batch_range.end, RowBoundary), (
            "Only RowBoundary is supported for pagination currently"
        )
        batch_range = ExpiredBatchRange(
            start=batch.batch_range.end,
            end=LimitBoundary(batch_size=batch_size),
        )
