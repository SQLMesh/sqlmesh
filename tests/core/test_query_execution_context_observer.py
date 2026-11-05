# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextvars
import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.engine_adapter.athena import AthenaEngineAdapter
from sqlmesh.core.execution_observation import (
    action,
    console_observer_scope,
    observer_scope,
)
from sqlmesh.core.query_execution import (
    ModelExecutionContext,
    QueryExecutionContext,
    get_query_execution_context,
)
from sqlmesh.core.query_execution_context_observer import (
    should_enable_automatic_query_execution_context,
)
from sqlmesh.utils import CorrelationId


def _snapshot(name: str = "analytics.orders") -> Any:
    return SimpleNamespace(
        is_model=True,
        name=name,
        snapshot_id=SimpleNamespace(identifier=f"version-{name}"),
    )


def _interval(day: int) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(2024, 1, day, tzinfo=dt.timezone.utc)
    return start, start + dt.timedelta(days=1)


def _payload(sql: str) -> Optional[dict[str, Any]]:
    match = re.search(r"/\* QUERY_CONTEXT: (.*?) \*/", sql)
    return json.loads(match[1]) if match else None


def _last_sql(adapter: EngineAdapter) -> str:
    return adapter.cursor.execute.call_args.args[0]


def _run_model(adapter: EngineAdapter, day: int = 1) -> tuple[str, QueryExecutionContext]:
    with action(
        "model",
        "evaluate",
        snapshot=_snapshot(),
        interval=_interval(day),
        batch_index=day - 1,
    ):
        context = get_query_execution_context()
        assert context is not None
        adapter.execute("SELECT 1")
        return _last_sql(adapter), context


def test_default_console_installs_context_and_derives_nested_phases(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        correlation_id=CorrelationId.from_plan_id("plan-1"),
    )
    console = SimpleNamespace(get_execution_observer=lambda: None)
    events = []

    def observer(event, facts, error):
        events.append((event, dict(facts), get_query_execution_context(), error))

    console.get_execution_observer = lambda: observer
    with console_observer_scope(console, query_execution_context=True):
        with action(
            "model",
            "evaluate",
            snapshot=_snapshot(),
            interval=_interval(1),
            batch_index=0,
        ):
            model_context = get_query_execution_context()
            assert model_context is not None
            adapter.execute("SELECT 1")
            sql = _last_sql(adapter)
            with action("audit", "audit", audit_name="not_null"):
                adapter.execute("SELECT 2")
            audit_context = _payload(_last_sql(adapter))
            with action("physical", "create", snapshot=_snapshot()):
                adapter.execute("SELECT 3")
            materialize_context = _payload(_last_sql(adapter))
        assert get_query_execution_context() is None

    model_payload = _payload(sql)
    assert model_payload is not None
    evaluation = model_context.evaluation
    assert evaluation is not None
    assert sql.startswith("/* SQLMESH_PLAN: plan-1 */ /* QUERY_CONTEXT:")
    assert model_payload["operation"]["id"] == evaluation.evaluation_id
    assert model_payload["query"] == {"phase": "materialize", "role": "query"}
    assert audit_context is not None
    assert audit_context["operation"]["id"] == evaluation.evaluation_id
    assert audit_context["query"] == {"phase": "audit", "role": "audit"}
    assert materialize_context is not None
    assert materialize_context["operation"]["id"] == evaluation.evaluation_id
    assert materialize_context["query"] == {"phase": "ddl", "role": "query"}
    model_events = [event for event in events if event[1]["kind"] == "model"]
    assert [event[0] for event in model_events] == ["start", "finish"]
    assert all(event[2] == model_context for event in model_events)
    assert all(event[1]["batch_index"] == 0 for event in model_events)


@pytest.mark.parametrize(("action_name", "audit_only"), [("audit_only", False), ("evaluate", True)])
def test_audit_only_context_tags_nested_audits_and_restores(
    action_name: str,
    audit_only: bool,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    observed = []

    def observer(event, facts, error):
        if facts["kind"] == "model":
            observed.append((event, facts["action"], get_query_execution_context()))

    console = SimpleNamespace(get_execution_observer=lambda: observer)
    with console_observer_scope(console, query_execution_context=True):
        with action(
            "model",
            action_name,
            snapshot=_snapshot(),
            interval=_interval(1),
            batch_index=0,
            audit_only=audit_only,
        ):
            outer = get_query_execution_context()
            assert outer is not None
            adapter.execute("SELECT audited model")
            model_sql = _last_sql(adapter)
            with action("audit", "audit", audit_name="not_null"):
                adapter.execute("SELECT audit")
                audit_sql = _last_sql(adapter)
            assert get_query_execution_context() == outer
        assert get_query_execution_context() is None

    evaluation = outer.evaluation
    assert evaluation is not None
    assert evaluation.operation_type.value == "audit"
    model_payload = _payload(model_sql)
    audit_payload = _payload(audit_sql)
    assert model_payload is not None
    assert audit_payload is not None
    assert model_payload["operation"]["id"] == evaluation.evaluation_id
    assert audit_payload["operation"]["id"] == evaluation.evaluation_id
    assert model_payload["operation"]["type"] == "audit"
    assert model_payload["query"] == {"phase": "audit", "role": "audit"}
    assert audit_payload["query"] == {"phase": "audit", "role": "audit"}
    assert [event for event, _, _ in observed] == ["start", "finish"]
    assert all(context == outer for _, _, context in observed)


def test_explicit_disabled_scope_suppresses_transport_then_outer_context_resumes(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    console = SimpleNamespace(get_execution_observer=lambda: None)

    with console_observer_scope(console, query_execution_context=True):
        with action(
            "model",
            "evaluate",
            snapshot=_snapshot(),
            interval=_interval(1),
            batch_index=0,
        ):
            outer = get_query_execution_context()
            assert outer is not None
            with observer_scope(None):
                adapter.execute("SELECT 1")
                disabled_sql = _last_sql(adapter)
                assert get_query_execution_context() == outer
                with console_observer_scope(console, query_execution_context=True):
                    with action(
                        "model",
                        "evaluate",
                        snapshot=_snapshot("analytics.inner"),
                        interval=_interval(2),
                        batch_index=1,
                    ):
                        assert get_query_execution_context() == outer
                        adapter.execute("SELECT nested disabled")
                        nested_disabled_sql = _last_sql(adapter)
            adapter.execute("SELECT 2")
            resumed_sql = _last_sql(adapter)

    assert _payload(disabled_sql) is None
    assert _payload(nested_disabled_sql) is None
    resumed = _payload(resumed_sql)
    assert resumed is not None
    assert outer.evaluation is not None
    assert resumed["operation"]["id"] == outer.evaluation.evaluation_id


def test_nonmodel_and_malformed_nested_model_clear_then_restore_outer_context(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    console = SimpleNamespace(get_execution_observer=lambda: None)
    with console_observer_scope(console, query_execution_context=True):
        with action(
            "model",
            "evaluate",
            snapshot=_snapshot(),
            interval=_interval(1),
            batch_index=0,
        ):
            outer = get_query_execution_context()
            assert outer is not None

            with action(
                "model",
                "evaluate",
                snapshot=SimpleNamespace(is_model=False),
                interval=_interval(2),
                batch_index=1,
            ):
                assert get_query_execution_context() is None
                adapter.execute("SELECT nonmodel")
                nonmodel_sql = _last_sql(adapter)
            assert get_query_execution_context() == outer

            with action(
                "model",
                "evaluate",
                snapshot=_snapshot("analytics.malformed"),
                interval=_interval(3),
                batch_index=-1,
            ):
                assert get_query_execution_context() is None
                adapter.execute("SELECT malformed model")
                malformed_sql = _last_sql(adapter)
            assert get_query_execution_context() == outer

            with action("stage", "unclassified"):
                assert get_query_execution_context() == outer
                adapter.execute("SELECT outer context")
                outer_sql = _last_sql(adapter)

        assert get_query_execution_context() is None

    assert _payload(nonmodel_sql) is None
    assert _payload(malformed_sql) is None
    outer_payload = _payload(outer_sql)
    assert outer_payload is not None
    assert outer.evaluation is not None
    assert outer_payload["operation"]["id"] == outer.evaluation.evaluation_id


@pytest.mark.parametrize("provider_kind", ["value", "none"])
def test_custom_provider_remains_authoritative(
    provider_kind: str,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    automatic_adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    if provider_kind == "value":
        custom = QueryExecutionContext(
            evaluation=ModelExecutionContext("custom-id", "custom.model", "custom-version")
        )
        adapter = make_mocked_engine_adapter(
            EngineAdapter,
            query_execution_context=True,
            query_execution_context_provider=lambda: custom,
        )
    else:
        adapter = make_mocked_engine_adapter(
            EngineAdapter,
            query_execution_context=True,
            query_execution_context_provider=lambda: None,
        )
    console = SimpleNamespace(get_execution_observer=lambda: None)
    assert should_enable_automatic_query_execution_context([automatic_adapter, adapter])

    with (
        console_observer_scope(console, query_execution_context=True),
        action(
            "model",
            "evaluate",
            snapshot=_snapshot(),
            interval=_interval(1),
            batch_index=0,
        ),
    ):
        adapter.execute("SELECT 1")
        sql = _last_sql(adapter)

    payload = _payload(sql)
    if provider_kind == "value":
        assert payload is not None
        assert payload["operation"]["id"] == "custom-id"
    else:
        assert payload is None


def test_auto_producer_requires_enabled_supported_adapter_without_provider(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    custom = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=lambda: None,
    )
    disabled = make_mocked_engine_adapter(EngineAdapter, query_execution_context=False)
    unsupported = make_mocked_engine_adapter(AthenaEngineAdapter, query_execution_context=True)

    assert not should_enable_automatic_query_execution_context([custom, disabled, unsupported])


def test_callback_reentry_does_not_emit_automatic_context(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    console = SimpleNamespace(get_execution_observer=None)
    callback_sql = []
    callback_contexts = []

    def observer(event, facts, error):
        if facts["kind"] == "model":
            callback_contexts.append(get_query_execution_context())
            adapter.execute("SELECT callback")
            callback_sql.append(_last_sql(adapter))

    console.get_execution_observer = lambda: observer
    with console_observer_scope(console, query_execution_context=True):
        regular_sql, model_context = _run_model(adapter)

    assert len(callback_sql) == 2
    assert all(_payload(sql) is None for sql in callback_sql)
    assert all(context == model_context for context in callback_contexts)
    payload = _payload(regular_sql)
    assert payload is not None, regular_sql
    assert model_context.evaluation is not None
    assert payload["operation"]["id"] == model_context.evaluation.evaluation_id


def test_worker_frames_are_isolated_for_a_shared_adapter(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    console = SimpleNamespace(get_execution_observer=lambda: None)
    barrier = Barrier(2)

    def evaluate(day: int):
        with action(
            "model",
            "evaluate",
            snapshot=_snapshot(f"analytics.model_{day}"),
            interval=_interval(day),
            batch_index=day - 1,
        ):
            context = get_query_execution_context()
            assert context is not None
            barrier.wait(timeout=5)
            sql = adapter._attach_query_execution_context(f"SELECT {day}")
            barrier.wait(timeout=5)
            return context, _payload(sql)

    with console_observer_scope(console, query_execution_context=True):
        contexts = [contextvars.copy_context(), contextvars.copy_context()]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda pair: pair[0].run(evaluate, pair[1]), zip(contexts, (1, 2)))
            )

    ids = [context.evaluation.evaluation_id for context, _ in results]
    assert len(set(ids)) == 2
    assert [payload["operation"]["id"] for _, payload in results] == ids
    assert {payload["workload"]["name"] for _, payload in results} == {
        "analytics.model_1",
        "analytics.model_2",
    }


@pytest.mark.parametrize("callback_phase", ["start", "finish"])
def test_base_exception_callback_restores_context(callback_phase: str):
    interrupt = KeyboardInterrupt()
    console = SimpleNamespace(get_execution_observer=lambda: None)
    events = []

    def observer(event, facts, error):
        events.append((event, get_query_execution_context()))
        if event == callback_phase:
            raise interrupt

    console.get_execution_observer = lambda: observer
    with console_observer_scope(console, query_execution_context=True):
        with pytest.raises(KeyboardInterrupt) as raised:
            with action(
                "model",
                "evaluate",
                snapshot=_snapshot(),
                interval=_interval(1),
                batch_index=0,
            ):
                assert callback_phase == "finish"
        assert raised.value is interrupt
        assert get_query_execution_context() is None
    assert [event for event, _ in events] == (
        ["start"] if callback_phase == "start" else ["start", "finish"]
    )
    assert events[0][1] is not None
    assert all(context == events[0][1] for _, context in events)


def test_observer_failure_and_workload_cancellation_restore_context():
    error = KeyboardInterrupt()
    contexts = []
    console = SimpleNamespace(get_execution_observer=lambda: None)

    def observer(event, facts, failure):
        contexts.append((event, get_query_execution_context(), failure))
        raise RuntimeError("observer failure")

    console.get_execution_observer = lambda: observer
    with console_observer_scope(console, query_execution_context=True):
        with pytest.raises(KeyboardInterrupt) as raised:
            with action(
                "model",
                "evaluate",
                snapshot=_snapshot(),
                interval=_interval(1),
                batch_index=0,
            ):
                raise error
        assert raised.value is error
        assert get_query_execution_context() is None

    assert [event for event, _, _ in contexts] == ["start", "finish"]
    assert contexts[0][1] == contexts[1][1]
    assert contexts[1][2] is error
