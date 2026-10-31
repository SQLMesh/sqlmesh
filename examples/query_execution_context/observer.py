# SPDX-License-Identifier: Apache-2.0
"""Use native observation events to supply query-context IDs."""

from __future__ import annotations

import contextvars
import dataclasses
import typing as t

from sqlmesh.core.execution_observation import Observer
from sqlmesh.core.query_execution import (
    ModelExecutionContext,
    QueryExecutionContext,
    QueryExecutionOperation,
    QueryExecutionPhase,
    QueryExecutionRole,
)
from sqlmesh.utils import random_id
from sqlmesh.utils.date import to_datetime


@dataclasses.dataclass(frozen=True)
class _Frame:
    facts: t.Mapping[str, t.Any]
    token: contextvars.Token[t.Optional[QueryExecutionContext]]


class QueryContextObserver:
    def __init__(self, observer: t.Optional[Observer] = None):
        self._observer = observer
        self._context: contextvars.ContextVar[t.Optional[QueryExecutionContext]] = (
            contextvars.ContextVar("example_query_context", default=None)
        )
        self._frames: contextvars.ContextVar[t.Tuple[_Frame, ...]] = contextvars.ContextVar(
            "example_query_context_frames", default=()
        )

    def current_query_context(self) -> t.Optional[QueryExecutionContext]:
        return self._context.get()

    def __call__(
        self,
        event: t.Literal["start", "finish"],
        facts: t.Mapping[str, t.Any],
        error: t.Optional[BaseException],
    ) -> None:
        if event == "start":
            context = self._derive_context(facts)
            token = self._context.set(context)
            self._frames.set(self._frames.get() + (_Frame(facts, token),))
            try:
                self._notify(event, facts, error)
            except BaseException:
                self._restore(facts)
                raise
        else:
            try:
                self._notify(event, facts, error)
            finally:
                self._restore(facts)

    def _notify(
        self,
        event: t.Literal["start", "finish"],
        facts: t.Mapping[str, t.Any],
        error: t.Optional[BaseException],
    ) -> None:
        if self._observer is not None:
            try:
                self._observer(event, facts, error)
            except Exception:
                pass

    def _restore(self, facts: t.Mapping[str, t.Any]) -> None:
        frames = self._frames.get()
        assert frames[-1].facts is facts
        self._context.reset(frames[-1].token)
        self._frames.set(frames[:-1])

    def _derive_context(self, facts: t.Mapping[str, t.Any]) -> t.Optional[QueryExecutionContext]:
        current = self._context.get()
        kind, action = facts["kind"], facts["action"]
        if kind == "model":
            snapshot = facts["snapshot"]
            if not snapshot.is_model:
                return None
            start, end = facts["interval"]
            return QueryExecutionContext(
                evaluation=ModelExecutionContext(
                    evaluation_id=random_id(),
                    model_name=snapshot.name,
                    snapshot_id=snapshot.snapshot_id.identifier,
                    interval_start=to_datetime(start),
                    interval_end=to_datetime(end),
                    batch_index=facts["batch_index"],
                    operation_type=QueryExecutionOperation.AUDIT
                    if action == "audit_only"
                    else QueryExecutionOperation.EVALUATE,
                ),
                phase=QueryExecutionPhase.AUDIT
                if action == "audit_only"
                else QueryExecutionPhase.MATERIALIZE,
                role=QueryExecutionRole.AUDIT
                if action == "audit_only"
                else QueryExecutionRole.QUERY,
            )
        if current is not None and kind == "audit":
            return dataclasses.replace(
                current, phase=QueryExecutionPhase.AUDIT, role=QueryExecutionRole.AUDIT
            )
        if current is not None and kind in ("physical", "virtual"):
            return dataclasses.replace(
                current,
                phase=QueryExecutionPhase.MATERIALIZE
                if action == "materialize"
                else QueryExecutionPhase.DDL,
                role=QueryExecutionRole.QUERY,
            )
        return current
