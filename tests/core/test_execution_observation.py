# SPDX-License-Identifier: Apache-2.0
import logging
from dataclasses import dataclass, field
from contextvars import ContextVar

import pytest

from sqlmesh.core.execution_observation import (
    action,
    observer_scope,
    update_execution,
    execution_context_factory,
)


@dataclass
class Observer:
    events: list = field(default_factory=list)

    def __call__(self, phase, facts, error):
        self.events.append((phase, dict(facts), error))


def test_native_facts_have_no_telemetry_policy():
    observer = Observer()
    error = ValueError("view failed")
    with observer_scope(observer), pytest.raises(ValueError) as raised:
        with action("virtual", "promote", target="view"):
            update_execution(source_table="table")
            raise error
    assert raised.value is error
    assert observer.events == [
        ("start", {"kind": "virtual", "action": "promote", "target": "view"}, None),
        (
            "finish",
            {"kind": "virtual", "action": "promote", "target": "view", "source_table": "table"},
            error,
        ),
    ]


@pytest.mark.parametrize("phase", ["start", "finish"])
@pytest.mark.parametrize("failure", [None, ValueError("native failure"), KeyboardInterrupt()])
def test_observer_errors_cannot_change_workload_outcome(phase, failure):
    calls = []

    def observer(event, facts, error):
        calls.append(event)
        if event == phase:
            raise RuntimeError("observer failure")

    def workload():
        with observer_scope(observer), action("query", "execute"):
            if failure is not None:
                raise failure
            return 42

    if failure is None:
        assert workload() == 42
    else:
        with pytest.raises(type(failure)) as raised:
            workload()
        assert raised.value is failure
    assert calls == ["start", "finish"]


def test_disabled_observer_does_not_create_metadata():
    from sqlmesh.core.console import TerminalConsole

    assert TerminalConsole().get_execution_observer() is None
    with action("query", "execute") as metadata:
        assert metadata is None


def test_real_adapter_preserves_sql_and_errors(duck_conn, caplog):
    from sqlmesh.core.engine_adapter import DuckDBEngineAdapter

    adapter = DuckDBEngineAdapter(lambda: duck_conn)
    observer = Observer()
    sql = "SELECT\n  1 AS observed_value"
    with caplog.at_level(logging.DEBUG), observer_scope(observer):
        adapter.execute(sql)
        with pytest.raises(Exception):
            adapter.execute("SELECT * FROM missing_observed_table")
    queries = [event for event in observer.events if event[0] == "finish"]
    assert len(queries) == 2
    assert queries[0][2] is None
    assert queries[1][2] is not None
    assert all(
        event[1] == {"kind": "query", "action": "execute", "engine": "duckdb"} for event in queries
    )
    assert f"Executing SQL: {sql}" in [record.getMessage() for record in caplog.records]
    assert "Executing SQL: SELECT * FROM missing_observed_table" in [
        record.getMessage() for record in caplog.records
    ]


def test_native_dag_copies_consumer_context_not_predecessor_context():
    from sqlmesh.utils.concurrency import ConcurrentDAGExecutor
    from sqlmesh.utils.dag import DAG

    consumer_context = ContextVar("consumer_context", default=None)
    observer = Observer()
    dag = DAG()
    dag.add("second", {"first"})

    def execute(name):
        assert consumer_context.get() == "stage"
        consumer_context.set(name)
        with action("physical", "create", target=name):
            pass

    with observer_scope(observer):
        token = consumer_context.set("stage")
        try:
            errors, skipped = ConcurrentDAGExecutor(
                dag, execute, 2, False, execution_context_factory()
            ).run()
        finally:
            consumer_context.reset(token)
    assert not errors and not skipped
    assert len(observer.events) == 4


def test_concurrent_model_hooks_keep_batch_context_and_original_failures():
    """Real DAG workers retain independent batch scopes even when one fails."""
    from threading import Barrier, Lock, get_ident

    from sqlmesh.utils.concurrency import ConcurrentDAGExecutor
    from sqlmesh.utils.dag import DAG

    consumer_context = ContextVar("batch_context", default="plan")
    barrier = Barrier(2)
    lock = Lock()
    events = []
    failure = RuntimeError("model evaluation failed")
    dag = DAG()
    dag.add(0)
    dag.add(1)

    def observer(phase, facts, error):
        with lock:
            events.append((phase, dict(facts), error, get_ident(), consumer_context.get()))

    def execute(batch):
        assert consumer_context.get() == "plan"
        token = consumer_context.set(batch)
        try:
            with action("model", "evaluate", batch_index=batch, interval=(batch, batch + 1)):
                barrier.wait(timeout=5)
                if batch == 1:
                    raise failure
        finally:
            consumer_context.reset(token)

    with observer_scope(observer):
        errors, skipped = ConcurrentDAGExecutor(
            dag, execute, 2, False, execution_context_factory()
        ).run()
    assert len(errors) == 1 and errors[0].__cause__ is failure
    assert not skipped
    assert len({event[3] for event in events}) == 2
    for batch in (0, 1):
        batch_events = [event for event in events if event[1]["batch_index"] == batch]
        assert [event[0] for event in batch_events] == ["start", "finish"]
        assert all(event[4] == batch for event in batch_events)
        assert batch_events[-1][2] is (failure if batch == 1 else None)
    assert consumer_context.get() == "plan"


def test_read_only_identity_and_callback_reentry():
    events = []
    native = object()

    def observer(phase, facts, error):
        events.append((phase, facts, error))
        with pytest.raises(TypeError):
            facts["kind"] = "corrupt"
        update_execution(corrupt=True)
        with action("query", "observer-query"):
            update_execution(corrupt=True)

    with observer_scope(observer), action("model", "evaluate", snapshot=native):
        with action("query", "execute"):
            update_execution(late=True)
        update_execution(target="late table")
    assert [event[0] for event in events] == ["start", "start", "finish", "finish"]
    assert events[0][1] is events[-1][1]
    assert events[1][1] is events[2][1]
    assert events[-1][1]["snapshot"] is native
    assert events[-1][1]["target"] == "late table"
    assert "late" not in events[-1][1]
    assert all("corrupt" not in event[1] for event in events)


def test_explicit_disabled_scope_isolates_parent_metadata():
    observer = Observer()
    with observer_scope(observer), action("model", "evaluate"):
        with observer_scope(None), action("query", "execute"):
            update_execution(corrupt=True)
        update_execution(target="parent")
    assert len(observer.events) == 2
    assert observer.events[-1][1] == {"kind": "model", "action": "evaluate", "target": "parent"}


@pytest.mark.parametrize("tasks_num", [1, 2])
def test_context_admission_and_sibling_isolation(tasks_num):
    from contextvars import copy_context
    from sqlmesh.utils.concurrency import ConcurrentDAGExecutor, concurrent_apply_to_dag
    from sqlmesh.utils.dag import DAG

    consumer = ContextVar("admission", default="construction")
    dag = DAG()
    dag.add("second", {"first"})
    seen = []

    def execute(node):
        seen.append(consumer.get())
        consumer.set(node)

    executor = ConcurrentDAGExecutor(dag, execute, tasks_num, False, copy_context)
    token = consumer.set("first admission")
    try:
        executor.run()
        consumer.set("second admission")
        executor.run()
        concurrent_apply_to_dag(dag, execute, tasks_num, context_factory=copy_context)
        assert consumer.get() == "second admission"
    finally:
        consumer.reset(token)
    assert seen == ["first admission"] * 2 + ["second admission"] * 4


def test_console_scope_getter_failure_and_nested_reuse():
    from sqlmesh.core.execution_observation import console_observer_scope
    from unittest.mock import Mock

    console = Mock()
    console.get_execution_observer.side_effect = ValueError("getter failed")
    with console_observer_scope(console), action("query", "execute") as facts:
        assert facts is None
    observer = Observer()
    console.reset_mock(side_effect=True)
    console.get_execution_observer.return_value = observer
    with console_observer_scope(console):
        with console_observer_scope(console), action("model", "evaluate"):
            pass
    console.get_execution_observer.assert_called_once_with()
    assert len(observer.events) == 2
    console.reset_mock()
    with observer_scope(None), console_observer_scope(console):
        with action("query", "execute") as facts:
            assert facts is None
    console.get_execution_observer.assert_not_called()


def test_unobserved_context_factory_is_disabled():
    assert execution_context_factory() is None
    with observer_scope(None):
        assert execution_context_factory() is None


@pytest.mark.parametrize("phase", ["start", "finish"])
def test_callback_base_exception_propagates_and_scope_is_restored(phase):
    interrupt = KeyboardInterrupt()
    events = []

    def observer(event, facts, error):
        events.append(event)
        if event == phase:
            raise interrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        with observer_scope(observer), action("query", "execute"):
            pass
    assert raised.value is interrupt
    assert events == (["start"] if phase == "start" else ["start", "finish"])
    with action("query", "execute") as facts:
        assert facts is None
    observed = Observer()
    with observer_scope(observed), action("model", "evaluate"):
        update_execution(target="clean")
    assert len(observed.events) == 2
    assert observed.events[-1][1]["target"] == "clean"


def test_disabled_and_observed_adapter_sql_and_side_effects_match(duck_conn, mocker):
    from sqlmesh.core.engine_adapter import DuckDBEngineAdapter

    adapter = DuckDBEngineAdapter(lambda: duck_conn)
    execute = mocker.spy(adapter, "_execute")
    sql_log = mocker.spy(adapter, "_log_sql")
    sql = "CREATE OR REPLACE TABLE observed_effect AS SELECT 42 AS value"
    calls = []
    for observer in (None, Observer()):
        execute.reset_mock()
        sql_log.reset_mock()
        with observer_scope(observer):
            adapter.execute(sql)
        calls.append((execute.call_args_list, sql_log.call_args_list))
        assert duck_conn.execute("SELECT value FROM observed_effect").fetchall() == [(42,)]
    assert calls[0] == calls[1]
    assert len(calls[0][0]) == 1


def test_default_dag_does_not_capture_context(mocker):
    from sqlmesh.utils.concurrency import ConcurrentDAGExecutor, concurrent_apply_to_dag
    from sqlmesh.utils.dag import DAG

    copy = mocker.patch("contextvars.copy_context", side_effect=AssertionError("unexpected copy"))
    dag = DAG()
    dag.add("node")
    for workers in (1, 2):
        concurrent_apply_to_dag(dag, lambda _: None, workers)
        executor = ConcurrentDAGExecutor(dag, lambda _: None, workers, False)
        executor.run()
        assert executor._context is None
    copy.assert_not_called()


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("registration", ["console", "explicit", "disabled", "broken"])
def test_plan_and_scheduler_registration(mocker, make_snapshot, workers, registration):
    from contextlib import nullcontext
    from sqlglot import parse_one
    from sqlmesh.core.console import TerminalConsole
    from sqlmesh.core.environment import EnvironmentNamingInfo
    from sqlmesh.core.model import SqlModel, FullKind
    from sqlmesh.core.plan.evaluator import BuiltInPlanEvaluator
    from sqlmesh.core.plan import stages
    from sqlmesh.core.scheduler import Scheduler
    from sqlmesh.core.snapshot import DeployabilityIndex, SnapshotChangeCategory
    from sqlmesh.utils.date import to_timestamp

    snapshot = make_snapshot(
        SqlModel(name="test.model", kind=FullKind(), query=parse_one("SELECT 1 AS a"))
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    console = TerminalConsole()
    observer = Observer()
    getter = mocker.patch.object(console, "get_execution_observer", return_value=observer)
    if registration == "broken":
        getter.side_effect = RuntimeError("getter failure")
    native = mocker.MagicMock()
    native.get_snapshots_to_create.return_value = []
    native.set_correlation_id.return_value = native
    scheduler = Scheduler(
        [snapshot], native, mocker.MagicMock(), None, max_workers=workers, console=console
    )
    workload = []
    consumer = ContextVar("plan_consumer", default=None)

    def evaluate(*args, **kwargs):
        if registration in ("console", "explicit"):
            assert consumer.get() == "stage"
        with action("query", "execute"):
            workload.append(True)
        return []

    mocker.patch.object(scheduler, "evaluate", side_effect=evaluate)
    evaluator = BuiltInPlanEvaluator(
        mocker.MagicMock(), native, mocker.Mock(), None, console=console
    )
    stage = stages.BeforeAllStage(statements=[], all_snapshots=[])
    mocker.patch("sqlmesh.core.plan.evaluator.stages.build_plan_stages", return_value=[stage])
    mocker.patch("sqlmesh.core.plan.evaluator.analytics.collector")

    def run_scheduler(*args):
        token = consumer.set("stage")
        try:
            errors, skipped = scheduler.run_merged_intervals(
                merged_intervals={
                    snapshot: [(to_timestamp("2024-01-01"), to_timestamp("2024-01-02"))]
                },
                deployability_index=DeployabilityIndex.all_deployable(),
                environment_naming_info=EnvironmentNamingInfo(),
            )
            assert not errors and not skipped
        finally:
            consumer.reset(token)

    mocker.patch.object(evaluator, "visit_before_all_stage", side_effect=run_scheduler)
    scope = (
        observer_scope(observer if registration == "explicit" else None)
        if registration in ("explicit", "disabled")
        else nullcontext()
    )
    with scope:
        evaluator.evaluate(mocker.Mock(plan_id="observed-plan"))
    assert workload == [True]
    assert getter.call_count == (1 if registration in ("console", "broken") else 0)
    if registration in ("console", "explicit"):
        assert [(phase, facts["kind"]) for phase, facts, _ in observer.events] == [
            ("start", "stage"),
            ("start", "model"),
            ("start", "query"),
            ("finish", "query"),
            ("finish", "model"),
            ("finish", "stage"),
        ]
    else:
        assert observer.events == []
