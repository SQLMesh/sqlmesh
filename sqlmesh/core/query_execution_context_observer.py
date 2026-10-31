# SPDX-License-Identifier: Apache-2.0
"""Build model query context from the native execution observer boundary."""

from __future__ import annotations

import contextvars
import dataclasses
import typing as t
from dataclasses import dataclass

from sqlmesh.core.execution_observation import Observer, _dispatching, _observer
from sqlmesh.core.query_execution import (
    ModelExecutionContext,
    QueryExecutionContext,
    QueryExecutionOperation,
    QueryExecutionPhase,
    QueryExecutionRole,
    get_query_execution_context,
    query_execution_scope,
)
from sqlmesh.utils import random_id
from sqlmesh.utils.date import to_datetime

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter.base import EngineAdapter


@dataclass(frozen=True)
class _Frame:
    facts: t.Mapping[str, t.Any]
    scope: t.ContextManager[None]


class AutomaticQueryExecutionObserver:
    def __init__(self, observer: t.Optional[Observer] = None):
        self._observer = observer
        self._frames: contextvars.ContextVar[t.Tuple[_Frame, ...]] = contextvars.ContextVar(
            "automatic_query_execution_frames", default=()
        )

    def __call__(
        self,
        event: t.Literal["start", "finish"],
        facts: t.Mapping[str, t.Any],
        error: t.Optional[BaseException],
    ) -> None:
        if event == "start":
            frame = self._begin(facts)
            try:
                self._notify(event, facts, error)
            except BaseException as ex:
                self._restore(frame, ex)
                raise
            return

        matching_frame = self._matching_frame(facts)
        try:
            self._notify(event, facts, error)
        finally:
            if matching_frame is not None:
                self._restore(matching_frame, error)

    def _begin(self, facts: t.Mapping[str, t.Any]) -> _Frame:
        try:
            context = self._derive_context(facts)
        except Exception:
            context = None if facts.get("kind") == "model" else get_query_execution_context()

        scope = query_execution_scope(context)
        scope.__enter__()
        frame = _Frame(facts=facts, scope=scope)
        try:
            self._frames.set(self._frames.get() + (frame,))
        except BaseException:
            scope.__exit__(None, None, None)
            raise
        return frame

    def _matching_frame(self, facts: t.Mapping[str, t.Any]) -> t.Optional[_Frame]:
        frames = self._frames.get()
        if frames and frames[-1].facts is facts:
            return frames[-1]
        return None

    def _restore(self, frame: _Frame, error: t.Optional[BaseException]) -> None:
        frames = self._frames.get()
        if not frames or frames[-1] is not frame:
            return
        try:
            frame.scope.__exit__(
                type(error) if error is not None else None,
                error,
                error.__traceback__ if error is not None else None,
            )
        finally:
            self._frames.set(frames[:-1])

    def _notify(
        self,
        event: t.Literal["start", "finish"],
        facts: t.Mapping[str, t.Any],
        error: t.Optional[BaseException],
    ) -> None:
        if self._observer is None:
            return
        try:
            self._observer(event, facts, error)
        except Exception:
            pass

    def _derive_context(self, facts: t.Mapping[str, t.Any]) -> t.Optional[QueryExecutionContext]:
        current = get_query_execution_context()
        kind = facts.get("kind")
        action = facts.get("action")
        if kind == "model":
            snapshot = facts.get("snapshot")
            if snapshot is None or not snapshot.is_model:
                return None
            interval = facts.get("interval")
            interval_start, interval_end = interval if interval is not None else (None, None)
            audit_only = action == "audit_only" or facts.get("audit_only", False)
            return QueryExecutionContext(
                evaluation=ModelExecutionContext(
                    evaluation_id=random_id(),
                    model_name=snapshot.name,
                    snapshot_id=snapshot.snapshot_id.identifier,
                    interval_start=to_datetime(interval_start)
                    if interval_start is not None
                    else None,
                    interval_end=to_datetime(interval_end) if interval_end is not None else None,
                    batch_index=facts.get("batch_index"),
                    operation_type=(
                        QueryExecutionOperation.AUDIT
                        if audit_only
                        else QueryExecutionOperation.EVALUATE
                    ),
                ),
                phase=QueryExecutionPhase.AUDIT if audit_only else QueryExecutionPhase.MATERIALIZE,
                role=QueryExecutionRole.AUDIT if audit_only else QueryExecutionRole.QUERY,
            )
        if current is None:
            return None
        if kind == "audit":
            return dataclasses.replace(
                current, phase=QueryExecutionPhase.AUDIT, role=QueryExecutionRole.AUDIT
            )
        if kind in ("physical", "virtual"):
            return dataclasses.replace(
                current,
                phase=(
                    QueryExecutionPhase.MATERIALIZE
                    if action == "materialize"
                    else QueryExecutionPhase.DDL
                ),
                role=QueryExecutionRole.QUERY,
            )
        return current


def get_automatic_query_execution_context() -> t.Optional[QueryExecutionContext]:
    if _dispatching.get():
        return None
    if isinstance(_observer.get(), AutomaticQueryExecutionObserver):
        return get_query_execution_context()
    return None


def should_enable_automatic_query_execution_context(
    adapters: t.Iterable[EngineAdapter],
) -> bool:
    return any(
        adapter.query_execution_context
        and adapter.ATTACH_CORRELATION_ID
        and adapter.query_execution_context_provider is None
        for adapter in adapters
    )


def with_automatic_query_execution_observer(
    observer: t.Optional[Observer],
) -> AutomaticQueryExecutionObserver:
    if isinstance(observer, AutomaticQueryExecutionObserver):
        return observer
    return AutomaticQueryExecutionObserver(observer)
