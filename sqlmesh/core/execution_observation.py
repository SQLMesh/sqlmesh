# SPDX-License-Identifier: Apache-2.0
"""Optional native lifecycle hooks, independent of telemetry storage or policy.

Call sites report start before native work and finish in finally, including the
original error. Consumers must return promptly. Ordinary observer exceptions never
replace workload results. The scope only routes hooks and late native metadata;
execution IDs, timing, parent correlation and status mapping belong to consumers.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import typing as t
from types import MappingProxyType

Observer = t.Callable[
    [t.Literal["start", "finish"], t.Mapping[str, t.Any], t.Optional[BaseException]], None
]
_dispatching: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "execution_observer_dispatch", default=False
)
_scoped: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "execution_observer_scoped", default=False
)
_observer: contextvars.ContextVar[t.Optional[Observer]] = contextvars.ContextVar(
    "execution_observer", default=None
)
_metadata: contextvars.ContextVar[t.Optional[t.Dict[str, t.Any]]] = contextvars.ContextVar(
    "execution_observation_metadata", default=None
)


def update_execution(**metadata: t.Any) -> None:
    """Supply native facts discovered inside the current hook boundary."""
    current = _metadata.get()
    if current is not None and not _dispatching.get():
        current.update(metadata)


@contextlib.contextmanager
def observer_scope(observer: t.Optional[Observer]) -> t.Iterator[None]:
    token = _observer.set(observer)
    scope_token = _scoped.set(True)
    metadata_token = _metadata.set(None)
    try:
        yield
    finally:
        _metadata.reset(metadata_token)
        _scoped.reset(scope_token)
        _observer.reset(token)


def _notify(
    observer: Observer,
    phase: t.Literal["start", "finish"],
    metadata: t.Mapping[str, t.Any],
    error: t.Optional[BaseException] = None,
) -> None:
    token = _dispatching.set(True)
    try:
        observer(phase, metadata, error)
    except Exception:
        # Hooks are observational: do not alter execution or recursively log a
        # telemetry failure. Consumers own their capture-loss reporting.
        pass
    finally:
        _dispatching.reset(token)


@contextlib.contextmanager
def action(kind: str, name: str, **metadata: t.Any) -> t.Iterator[t.Optional[t.Dict[str, t.Any]]]:
    observer = _observer.get()
    if observer is None or _dispatching.get():
        yield None
        return
    data = {**metadata, "kind": kind, "action": name}
    view = MappingProxyType(data)
    token = _metadata.set(data)
    try:
        _notify(observer, "start", view)
        error: t.Optional[BaseException] = None
        try:
            yield data
        except BaseException as ex:
            error = ex
            raise
        finally:
            _notify(observer, "finish", view, error)
    finally:
        _metadata.reset(token)


def observe_snapshot(kind: str, name: str) -> t.Callable:
    """Hook evaluator methods whose first argument after self is snapshot.

    Verified callers pass Snapshot as that argument (positional or keyword).
    Pass the native object unchanged; no reflection, identity formatting, skip
    classification or snapshot interpretation is performed by this hook.
    """

    def decorate(function: t.Callable) -> t.Callable:
        @functools.wraps(function)
        def wrapped(self: t.Any, snapshot: t.Any, *args: t.Any, **kwargs: t.Any) -> t.Any:
            with action(kind, name, snapshot=snapshot):
                return function(self, snapshot, *args, **kwargs)

        return wrapped

    return decorate


def execution_context_factory() -> t.Optional[t.Callable[[], contextvars.Context]]:
    """Opt in to worker context propagation only during active observation."""
    return (
        contextvars.copy_context if _observer.get() is not None and not _dispatching.get() else None
    )


@contextlib.contextmanager
def console_observer_scope(console: t.Any) -> t.Iterator[None]:
    """Resolve once at the outer boundary; an explicit None scope disables hooks."""
    if _scoped.get():
        yield
        return
    try:
        observer = console.get_execution_observer()
    except Exception:
        observer = None
    with observer_scope(observer):
        yield
