# SPDX-License-Identifier: Apache-2.0
"""Owned ordinary-run rewrites. This local prototype supports PostgreSQL state only."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import typing as t
from sqlmesh.utils.date import TimeLike
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core.snapshot import Snapshot


class RewriteBlockedError(SQLMeshError):
    pass


class RewriteStatus(str, Enum):
    ACTIVE = "active"
    TERMINAL = "terminal"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class RewriteAttempt:
    attempt_id: str
    owner: str
    name: str
    identifier: str
    version: str
    target: str
    start: int
    end: int
    status: RewriteStatus


class RewriteProtection(t.Protocol):
    def execution(self, owner: str) -> t.ContextManager[None]: ...
    def bind_warehouse(self, owner: str, path: str) -> None: ...
    def check_owner(self, owner: str) -> None: ...
    def check_attempt(self, a: RewriteAttempt) -> None: ...
    def list_unresolved(self) -> list[RewriteAttempt]: ...
    def begin(
        self,
        owner: str,
        snapshot: Snapshot,
        start: TimeLike,
        end: TimeLike,
        attempt_id: str | None = None,
    ) -> RewriteAttempt: ...
    def terminal(self, a: RewriteAttempt) -> None: ...
    def finish(self, a: RewriteAttempt, snapshot: Snapshot) -> None: ...
    def blocked(self, message: str) -> RewriteBlockedError: ...
    def reject_lifecycle(self) -> None: ...


# The evaluator checks this thread-local capability before materialization.
evaluation_attempt: ContextVar[RewriteAttempt | None] = ContextVar(
    "evaluation_attempt", default=None
)
execution_owner: ContextVar[str | None] = ContextVar("execution_owner", default=None)
