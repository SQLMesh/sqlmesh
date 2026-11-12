# SPDX-License-Identifier: Apache-2.0
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier
from typing import Any, Callable, Optional

import pytest
from sqlglot import parse_one

from sqlmesh.core.config.connection import DuckDBConnectionConfig
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.engine_adapter.athena import AthenaEngineAdapter
import sqlmesh.core.engine_adapter.base as engine_adapter_base
from sqlmesh.core.query_execution import (
    ModelExecutionContext,
    QueryExecutionContext,
    QueryExecutionPhase,
    QueryExecutionRole,
    get_query_execution_context,
)
from sqlmesh.utils import CorrelationId


class _QueryCancellation(BaseException):
    pass


def _context(evaluation_id: str = "eval-1") -> QueryExecutionContext:
    return QueryExecutionContext(
        evaluation=ModelExecutionContext(
            evaluation_id=evaluation_id,
            model_name="analytics.orders",
            snapshot_id="snapshot-3",
        ),
        phase=QueryExecutionPhase.MATERIALIZE,
        role=QueryExecutionRole.INSERT,
    )


def _executed_sql(adapter: Any) -> list[str]:
    return [call.args[0] for call in adapter.cursor.execute.call_args_list]


def _query_execution_payload(sql: str) -> Optional[dict[str, Any]]:
    prefix = "/* QUERY_CONTEXT: "
    start = sql.find(prefix)
    if start == -1:
        return None
    end = sql.index(" */ ", start)
    return json.loads(sql[start + len(prefix) : end])


def test_execute_attaches_provider_context_before_legacy_parent(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    parent = CorrelationId.from_plan_id("plan-8")
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=lambda: _context(),
        correlation_id=parent,
    )

    adapter.execute("SELECT 1")

    assert _executed_sql(adapter) == [
        '/* SQLMESH_PLAN: plan-8 */ /* QUERY_CONTEXT: {"schema_version":1,"emitter":"sqlmesh",'
        '"parent":{"system":"sqlmesh","type":"plan","id":"plan-8"},'
        '"workload":{"system":"sqlmesh","type":"model","name":"analytics.orders",'
        '"version":"snapshot-3"},"operation":{"id":"eval-1","type":"evaluate"},'
        '"query":{"phase":"materialize","role":"insert"}} */ SELECT 1'
    ]


def test_execute_uses_parent_only_context_when_provider_returns_none(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    parent = CorrelationId.from_run_id("run-7")
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=lambda: None,
        correlation_id=parent,
    )

    adapter.execute("SELECT 1")

    assert _executed_sql(adapter) == [
        '/* SQLMESH_RUN: run-7 */ /* QUERY_CONTEXT: {"schema_version":1,"emitter":"sqlmesh",'
        '"parent":{"system":"sqlmesh","type":"run","id":"run-7"},'
        '"query":{"phase":"unknown","role":"query"}} */ SELECT 1'
    ]


def test_adapter_correlation_marker_overrides_and_clears_provider_parent(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    stale_parent = CorrelationId.from_plan_id("stale-plan")
    first_parent = CorrelationId.from_plan_id("plan-8")
    second_parent = CorrelationId.from_run_id("run-9")
    context = QueryExecutionContext(
        parent=stale_parent,
        evaluation=_context().evaluation,
        phase=QueryExecutionPhase.MATERIALIZE,
        role=QueryExecutionRole.INSERT,
    )
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=lambda: context,
        correlation_id=first_parent,
    )

    adapter.execute("SELECT 1")
    adapter.correlation_id = second_parent
    adapter.execute("SELECT 2")
    adapter.correlation_id = None
    adapter.execute("SELECT 3")

    sql = _executed_sql(adapter)
    assert [query.startswith("/* SQLMESH_") for query in sql] == [True, True, False]
    assert sql[0].startswith("/* SQLMESH_PLAN: plan-8 */ /* QUERY_CONTEXT:")
    assert sql[1].startswith("/* SQLMESH_RUN: run-9 */ /* QUERY_CONTEXT:")
    payloads = [_query_execution_payload(query) for query in sql]
    assert [
        payload["parent"]["id"] if payload is not None and "parent" in payload else None
        for payload in payloads
    ] == ["plan-8", "run-9", None]
    assert [
        payload["parent"]["type"] if payload is not None and "parent" in payload else None
        for payload in payloads
    ] == ["plan", "run", None]


@pytest.mark.parametrize("has_provider", [False, True], ids=["provider-absent", "provider-valid"])
def test_invalid_adapter_parent_omits_rich_context_and_keeps_legacy_comment(
    has_provider: bool,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    provider = (lambda: _context()) if has_provider else None
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=provider,
        correlation_id=CorrelationId.from_plan_id(""),
    )

    adapter.execute("SELECT 1")

    assert _executed_sql(adapter) == ["/* SQLMESH_PLAN:  */ SELECT 1"]


def test_disabled_transport_never_calls_provider_or_changes_sql(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    calls = []

    def provider() -> Optional[QueryExecutionContext]:
        calls.append(True)
        raise AssertionError("disabled provider was called")

    parent = CorrelationId.from_plan_id("plan-8")
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=False,
        query_execution_context_provider=provider,
        correlation_id=parent,
    )

    adapter.execute("SELECT 1")

    assert _executed_sql(adapter) == [f"/* {parent} */ SELECT 1"]
    assert calls == []


def test_adapter_clone_inherits_provider_and_allows_explicit_clear(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=lambda: _context(),
    )

    inherited = adapter.with_settings()
    cleared = adapter.with_settings(query_execution_context_provider=None)
    inherited.execute("SELECT 1")
    cleared.execute("SELECT 2")

    sql = _executed_sql(inherited)
    assert "QUERY_CONTEXT" in sql[0]
    assert sql[1] == "SELECT 2"
    assert inherited.query_execution_context_provider is adapter.query_execution_context_provider
    assert cleared.query_execution_context_provider is None


def test_connection_config_propagates_flag_without_extra_driver_config():
    adapter = DuckDBConnectionConfig(query_execution_context=True).create_engine_adapter()

    assert adapter.query_execution_context is True
    assert "query_execution_context" not in adapter._extra_config
    assert adapter.query_execution_context_provider is None


def test_legacy_adapter_constructor_accepts_connection_and_clone_settings():
    class LegacyAdapter(EngineAdapter):
        def __init__(self, *args: Any, **kwargs: Any):
            assert "query_execution_context" not in kwargs
            assert "query_execution_context_provider" not in kwargs
            super().__init__(*args, **kwargs)

    class LegacyConnectionConfig(DuckDBConnectionConfig):
        @property
        def _engine_adapter(self) -> type[EngineAdapter]:
            return LegacyAdapter

    adapter = LegacyConnectionConfig(query_execution_context=True).create_engine_adapter()
    assert adapter.query_execution_context is True
    assert adapter.with_settings().query_execution_context is True
    assert adapter.with_settings(query_execution_context=False).query_execution_context is False
    assert adapter.with_settings(
        query_execution_context_provider=lambda: _context()
    ).query_execution_context_provider


def test_unsupported_adapter_skips_provider_and_warns_once(
    caplog,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    calls = []

    def provider() -> QueryExecutionContext:
        calls.append(True)
        return _context()

    adapter = make_mocked_engine_adapter(
        AthenaEngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=provider,
    )

    with caplog.at_level("WARNING"):
        adapter.execute("SELECT 1")
        adapter.execute("SELECT 2")

    assert _executed_sql(adapter) == ["SELECT 1", "SELECT 2"]
    assert calls == []
    assert caplog.text.count("does not support generic SQL comment transport") == 1


def test_provider_failure_is_omitted_and_later_call_recovers(
    caplog,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    attempts = []

    def provider() -> QueryExecutionContext:
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("sensitive warehouse identifier")
        return _context()

    parent = CorrelationId.from_run_id("run-7")
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=provider,
        correlation_id=parent,
    )

    with caplog.at_level("WARNING"):
        adapter.execute("SELECT 1")
        adapter.execute("SELECT 2")

    sql = _executed_sql(adapter)
    assert sql[0] == "/* SQLMESH_RUN: run-7 */ SELECT 1"
    assert sql[1].startswith("/* SQLMESH_RUN: run-7 */ /* QUERY_CONTEXT:")
    payload = _query_execution_payload(sql[1])
    assert payload is not None
    assert payload["parent"] == {"system": "sqlmesh", "type": "run", "id": "run-7"}
    assert payload["operation"]["id"] == "eval-1"
    assert attempts == [True, True]
    assert "provider failed" in caplog.text
    assert "sensitive warehouse identifier" not in caplog.text


def test_serialization_failure_is_omitted_and_later_call_recovers(
    caplog,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
    monkeypatch,
):
    real_serializer = engine_adapter_base.serialize_query_execution_context
    attempts = []

    def fail_once(context: QueryExecutionContext) -> Optional[str]:
        attempts.append(context)
        if len(attempts) == 1:
            raise RuntimeError("sensitive serializer detail")
        return real_serializer(context)

    monkeypatch.setattr(engine_adapter_base, "serialize_query_execution_context", fail_once)
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=lambda: _context(),
        correlation_id=CorrelationId.from_run_id("run-7"),
    )

    with caplog.at_level("WARNING"):
        adapter.execute("SELECT 1")
        adapter.execute("SELECT 2")

    sql = _executed_sql(adapter)
    assert sql[0] == "/* SQLMESH_RUN: run-7 */ SELECT 1"
    assert sql[1].startswith("/* SQLMESH_RUN: run-7 */ /* QUERY_CONTEXT:")
    payload = _query_execution_payload(sql[1])
    assert payload is not None
    assert payload["operation"]["id"] == "eval-1"
    assert len(attempts) == 2
    assert "serialization failed" in caplog.text
    assert "sensitive serializer detail" not in caplog.text


def test_provider_runs_once_for_each_statement(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    attempts: list[QueryExecutionContext] = []

    def provider() -> QueryExecutionContext:
        context = _context(f"eval-{len(attempts)}")
        attempts.append(context)
        return context

    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=provider,
    )

    adapter.execute([parse_one("SELECT 1"), parse_one("SELECT 2")])

    assert len(attempts) == 2
    payloads = [_query_execution_payload(query) for query in _executed_sql(adapter)]
    assert all(payload is not None for payload in payloads)
    assert [payload["operation"]["id"] for payload in payloads if payload is not None] == [
        "eval-0",
        "eval-1",
    ]


def test_provider_returning_invalid_type_omits_comment(
    caplog,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
    )

    def invalid_provider() -> Any:
        return object()

    adapter.query_execution_context_provider = invalid_provider

    with caplog.at_level("WARNING"):
        adapter.execute("SELECT 1")

    assert _executed_sql(adapter) == ["SELECT 1"]
    assert "invalid context" in caplog.text


@pytest.mark.parametrize(
    "exception_type",
    [KeyboardInterrupt, SystemExit, _QueryCancellation],
    ids=["keyboard-interrupt", "system-exit", "custom-base-exception"],
)
@pytest.mark.parametrize(
    "nested",
    [False, True],
    ids=["adapter-owned-transaction", "nested-under-owner"],
)
def test_provider_base_exception_rolls_back_and_adapter_recovers(
    exception_type: type[BaseException],
    nested: bool,
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    cancellation = exception_type("cancel query")
    attempts = []

    def provider() -> QueryExecutionContext:
        attempts.append(True)
        if len(attempts) == 1:
            raise cancellation
        return _context("eval-after-cancellation")

    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=provider,
    )

    with pytest.raises(BaseException) as raised:
        if nested:
            with adapter.transaction():
                adapter.execute("SELECT 1")
        else:
            adapter.execute("SELECT 1")

    assert raised.value is cancellation
    assert get_query_execution_context() is None
    assert adapter.cursor.execute.call_count == 0
    assert adapter.cursor.rollback.call_count == 1
    assert not adapter._connection_pool.is_transaction_active

    adapter.execute("SELECT 2")

    sql = _executed_sql(adapter)
    assert len(sql) == 1
    assert sql[0].endswith("SELECT 2")
    payload = _query_execution_payload(sql[0])
    assert payload is not None
    assert payload["operation"]["id"] == "eval-after-cancellation"
    assert adapter.cursor.begin.call_count == 2
    assert adapter.cursor.commit.call_count == 1
    assert adapter.cursor.rollback.call_count == 1
    assert not adapter._connection_pool.is_transaction_active
    assert attempts == [True, True]


def test_provider_cancellation_does_not_open_transaction_when_adapter_disables_them(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    class NonTransactionalEngineAdapter(EngineAdapter):
        SUPPORTS_TRANSACTIONS = False

    cancellation = _QueryCancellation("cancel without transaction")
    attempts = []

    def provider() -> QueryExecutionContext:
        attempts.append(True)
        if len(attempts) == 1:
            raise cancellation
        return _context("eval-after-cancellation")

    adapter = make_mocked_engine_adapter(
        NonTransactionalEngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=provider,
    )

    with pytest.raises(_QueryCancellation) as raised:
        adapter.execute("SELECT 1")

    assert raised.value is cancellation
    assert adapter.cursor.execute.call_count == 0
    assert adapter.cursor.begin.call_count == 0
    assert adapter.cursor.rollback.call_count == 0
    assert adapter.cursor.commit.call_count == 0

    adapter.execute("SELECT 2")

    sql = _executed_sql(adapter)
    assert len(sql) == 1
    assert sql[0].endswith("SELECT 2")
    assert adapter.cursor.begin.call_count == 0
    assert adapter.cursor.rollback.call_count == 0
    assert adapter.cursor.commit.call_count == 0
    assert attempts == [True, True]


def test_disabled_query_context_driver_cancellation_rolls_back_and_recovers(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    cancellation = _QueryCancellation("driver cancelled query")
    execute_attempts = []

    def execute(sql: str) -> None:
        execute_attempts.append(sql)
        if len(execute_attempts) == 1:
            raise cancellation

    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=False)
    adapter.cursor.execute.side_effect = execute

    with pytest.raises(_QueryCancellation) as raised:
        adapter.execute("SELECT 1")

    assert raised.value is cancellation
    assert execute_attempts == ["SELECT 1"]
    assert adapter.cursor.rollback.call_count == 1
    assert not adapter._connection_pool.is_transaction_active

    adapter.execute("SELECT 2")

    assert execute_attempts == ["SELECT 1", "SELECT 2"]
    assert _executed_sql(adapter) == ["SELECT 1", "SELECT 2"]
    assert adapter.cursor.begin.call_count == 2
    assert adapter.cursor.commit.call_count == 1
    assert adapter.cursor.rollback.call_count == 1
    assert not adapter._connection_pool.is_transaction_active


def test_provider_reentry_skips_nested_rich_context_across_adapters(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    nested = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)
    attempts = []
    adapter = make_mocked_engine_adapter(EngineAdapter, query_execution_context=True)

    def provider() -> QueryExecutionContext:
        attempts.append(True)
        nested.execute("SELECT 2")
        return _context()

    nested.query_execution_context_provider = provider
    adapter.query_execution_context_provider = provider
    adapter.execute("SELECT 1")

    assert _executed_sql(nested) == ["SELECT 2"]
    assert "QUERY_CONTEXT" in _executed_sql(adapter)[0]
    assert attempts == [True]


def test_concurrent_provider_calls_keep_their_worker_contexts(
    make_mocked_engine_adapter: Callable[..., EngineAdapter],
):
    current: ContextVar[Optional[QueryExecutionContext]] = ContextVar(
        "test_query_execution_context", default=None
    )
    barrier = Barrier(2)
    adapter = make_mocked_engine_adapter(
        EngineAdapter,
        query_execution_context=True,
        query_execution_context_provider=current.get,
    )

    def execute(evaluation_id: str) -> None:
        context = _context(evaluation_id)
        token = current.set(context)
        try:
            barrier.wait(timeout=5)
            adapter.execute("SELECT 1")
        finally:
            current.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, value) for value in ("eval-a", "eval-b")]
        for future in futures:
            future.result(timeout=10)

    sql = _executed_sql(adapter)
    assert len(sql) == 2
    payloads = [_query_execution_payload(query) for query in sql]
    assert all(payload is not None for payload in payloads)
    assert {payload["operation"]["id"] for payload in payloads if payload is not None} == {
        "eval-a",
        "eval-b",
    }
