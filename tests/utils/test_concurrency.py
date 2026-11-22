import os
import subprocess
import sys
import textwrap
from contextvars import ContextVar, copy_context
from pathlib import Path

import pytest
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.snapshot import SnapshotId
from sqlmesh.utils.concurrency import (
    NodeExecutionFailedError,
    concurrent_apply_to_snapshots,
    concurrent_apply_to_values,
)


@pytest.mark.parametrize("tasks_num", [1, 2])
def test_concurrent_apply_to_snapshots(mocker: MockerFixture, tasks_num: int):
    snapshot_a = mocker.Mock()
    snapshot_a.snapshot_id = SnapshotId(name="model_a", identifier="snapshot_a")
    snapshot_a.parents = []

    snapshot_b = mocker.Mock()
    snapshot_b.snapshot_id = SnapshotId(name="model_b", identifier="snapshot_b")
    snapshot_b.parents = []

    snapshot_c = mocker.Mock()
    snapshot_c.snapshot_id = SnapshotId(name="model_c", identifier="snapshot_c")
    snapshot_c.parents = [snapshot_a.snapshot_id, snapshot_b.snapshot_id]

    snapshot_d = mocker.Mock()
    snapshot_d.snapshot_id = SnapshotId(name="model_d", identifier="snapshot_d")
    snapshot_d.parents = [snapshot_b.snapshot_id, snapshot_c.snapshot_id]

    processed_snapshots = []

    errors, skipped = concurrent_apply_to_snapshots(
        [snapshot_a, snapshot_b, snapshot_c, snapshot_d],
        lambda s: processed_snapshots.append(s),
        tasks_num,
    )

    assert len(processed_snapshots) == 4
    assert processed_snapshots[0] in (snapshot_a, snapshot_b)
    assert processed_snapshots[1] in (snapshot_a, snapshot_b)
    assert processed_snapshots[2] == snapshot_c
    assert processed_snapshots[3] == snapshot_d

    assert not errors
    assert not skipped


@pytest.mark.parametrize("tasks_num", [1, 2])
def test_concurrent_apply_to_snapshots_exception(mocker: MockerFixture, tasks_num: int):
    snapshot_a = mocker.Mock()
    snapshot_a.snapshot_id = SnapshotId(name="model_a", identifier="snapshot_a")
    snapshot_a.parents = []

    snapshot_b = mocker.Mock()
    snapshot_b.snapshot_id = SnapshotId(name="model_b", identifier="snapshot_b")
    snapshot_b.parents = []

    def raise_():
        raise RuntimeError("fail")

    with pytest.raises(NodeExecutionFailedError):
        concurrent_apply_to_snapshots(
            [snapshot_a, snapshot_b],
            lambda s: raise_(),
            tasks_num,
        )


@pytest.mark.parametrize("tasks_num", [1, 2])
def test_concurrent_apply_to_snapshots_return_failed_skipped(mocker: MockerFixture, tasks_num: int):
    snapshot_a = mocker.Mock()
    snapshot_a.snapshot_id = SnapshotId(name="model_a", identifier="snapshot_a")
    snapshot_a.parents = []

    snapshot_b = mocker.Mock()
    snapshot_b.snapshot_id = SnapshotId(name="model_b", identifier="snapshot_b")
    snapshot_b.parents = [snapshot_a.snapshot_id]

    snapshot_c = mocker.Mock()
    snapshot_c.snapshot_id = SnapshotId(name="model_c", identifier="snapshot_c")
    snapshot_c.parents = [snapshot_b.snapshot_id]

    snapshot_d = mocker.Mock()
    snapshot_d.snapshot_id = SnapshotId(name="model_d", identifier="snapshot_d")
    snapshot_d.parents = []

    snapshot_e = mocker.Mock()
    snapshot_e.snapshot_id = SnapshotId(name="model_e", identifier="snapshot_e")
    snapshot_e.parents = [snapshot_d.snapshot_id]

    def raise_(snapshot):
        if snapshot.snapshot_id.name == "model_a":
            raise RuntimeError("fail")

    errors, skipped = concurrent_apply_to_snapshots(
        [snapshot_a, snapshot_b, snapshot_c, snapshot_d, snapshot_e],
        lambda s: raise_(s),
        tasks_num,
        raise_on_error=False,
    )

    assert len(errors) == 1
    assert errors[0].node == snapshot_a.snapshot_id

    assert skipped == [snapshot_b.snapshot_id, snapshot_c.snapshot_id]


def test_concurrent_apply_to_snapshots_skip_each_node_only_once(mocker: MockerFixture):
    failed_snapshot = mocker.Mock()
    failed_snapshot.snapshot_id = SnapshotId(
        name="failed_snapshot_model", identifier="failed_snapshot"
    )
    failed_snapshot.parents = []

    snapshot_a = mocker.Mock()
    snapshot_a.snapshot_id = SnapshotId(name="model_a", identifier="snapshot_a")
    snapshot_a.parents = [failed_snapshot.snapshot_id]

    snapshot_b = mocker.Mock()
    snapshot_b.snapshot_id = SnapshotId(name="model_b", identifier="snapshot_b")
    snapshot_b.parents = [failed_snapshot.snapshot_id]

    # snapshot_c depends on both the failed snapshot and one of the skipped ones (snapshot_a)
    snapshot_c = mocker.Mock()
    snapshot_c.snapshot_id = SnapshotId(name="model_c", identifier="snapshot_c")
    snapshot_c.parents = [failed_snapshot.snapshot_id, snapshot_a.snapshot_id]

    def raise_(snapshot):
        if snapshot.snapshot_id.name == "failed_snapshot_model":
            raise RuntimeError("fail")

    errors, skipped = concurrent_apply_to_snapshots(
        [failed_snapshot, snapshot_a, snapshot_b, snapshot_c],
        lambda s: raise_(s),
        2,
        raise_on_error=False,
    )

    assert len(errors) == 1
    assert errors[0].node == failed_snapshot.snapshot_id

    assert set(skipped) == {snapshot_a.snapshot_id, snapshot_b.snapshot_id, snapshot_c.snapshot_id}


@pytest.mark.parametrize("tasks_num", [1, 3])
def test_concurrent_apply(tasks_num: int):
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    results = concurrent_apply_to_values(values, lambda x: x * 2, tasks_num)
    assert results == [x * 2 for x in values]


def _run_bounded_python(program: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(repo_root), env.get("PYTHONPATH")) if path
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_concurrent_apply_to_values_propagates_base_exception():
    _run_bounded_python(
        """
        from sqlmesh.utils.concurrency import concurrent_apply_to_values

        error = SystemExit("cancel value execution")

        def process(value):
            if value == 0:
                raise error
            return value

        try:
            concurrent_apply_to_values([0, 1], process, 2)
        except SystemExit as raised:
            assert raised is error
        else:
            raise AssertionError("the value worker cancellation was not propagated")
        """
    )


@pytest.mark.parametrize("raise_on_error", [True, False])
def test_concurrent_dag_propagates_base_exception_drains_workers_and_can_be_reused(
    raise_on_error: bool,
):
    _run_bounded_python(
        f"""
        from threading import Event, Thread

        from sqlmesh.utils.concurrency import ConcurrentDAGExecutor
        from sqlmesh.utils.dag import DAG

        dag = DAG()
        dag.add("cancel")
        dag.add("sibling")
        dag.add("dependent", ["sibling"])
        error = KeyboardInterrupt("cancel execution")
        release_sibling = Event()
        sibling_started = Event()
        called = []

        def execute(node):
            if node == "cancel":
                assert sibling_started.wait(timeout=3)
                raise error
            if node == "sibling":
                sibling_started.set()
                assert release_sibling.wait(timeout=6)
            called.append(node)

        executor = ConcurrentDAGExecutor(dag, execute, 2, {raise_on_error!r})
        run_errors = []

        def run():
            try:
                executor.run()
            except BaseException as ex:
                run_errors.append(ex)

        runner = Thread(target=run)
        runner.start()
        try:
            assert sibling_started.wait(timeout=3)
            try:
                executor._finished_future.result(timeout=3)
            except KeyboardInterrupt as raised:
                assert raised is error
            else:
                raise AssertionError("the worker cancellation was not propagated")
            assert runner.is_alive(), "run returned before the active worker drained"
        finally:
            release_sibling.set()

        runner.join(timeout=3)
        assert not runner.is_alive()
        assert run_errors == [error]
        assert called == ["sibling"]

        second_error = SystemExit("cancel reused executor")

        def cancel_again(node):
            raise second_error

        executor.fn = cancel_again
        try:
            executor.run()
        except SystemExit as raised:
            assert raised is second_error
        else:
            raise AssertionError("the reused executor did not propagate cancellation")
        """
    )


def test_concurrent_dag_fail_fast_stops_queued_work_while_aggregate_mode_continues():
    _run_bounded_python(
        """
        from threading import Event, Thread

        from sqlmesh.utils.concurrency import ConcurrentDAGExecutor
        from sqlmesh.utils.dag import DAG

        def make_dag():
            dag = DAG()
            dag.add("fail")
            dag.add("running")
            dag.add("queued")
            dag.add("failed_child", ["fail"])
            dag.add("running_child", ["running"])
            dag.add("queued_child", ["queued"])
            return dag

        failure = RuntimeError("fail node")
        release_running = Event()
        running_started = Event()
        queued_started = Event()
        called = []

        def execute(node):
            if node == "fail":
                assert running_started.wait(timeout=3)
                raise failure
            if node == "running":
                running_started.set()
                assert release_running.wait(timeout=6)
            if node == "queued":
                queued_started.set()
            called.append(node)

        executor = ConcurrentDAGExecutor(make_dag(), execute, 2, True)
        run_errors = []

        def run_fail_fast():
            try:
                executor.run()
            except BaseException as ex:
                run_errors.append(ex)

        runner = Thread(target=run_fail_fast)
        runner.start()
        try:
            assert running_started.wait(timeout=3)
            try:
                executor._finished_future.result(timeout=3)
            except Exception as raised:
                assert raised.node == "fail"
                assert raised.__cause__ is failure
            else:
                raise AssertionError("fail-fast execution did not raise")
            assert runner.is_alive(), "run returned before the active worker drained"
            assert not queued_started.is_set(), "queued work started after fail-fast"
        finally:
            release_running.set()

        runner.join(timeout=3)
        assert not runner.is_alive()
        assert len(run_errors) == 1
        assert run_errors[0].node == "fail"
        assert run_errors[0].__cause__ is failure
        assert called == ["running"]

        release_running = Event()
        running_started = Event()
        queued_started = Event()
        called = []
        executor = ConcurrentDAGExecutor(make_dag(), execute, 2, False)
        run_results = []

        def run_aggregate():
            run_results.append(executor.run())

        runner = Thread(target=run_aggregate)
        runner.start()
        try:
            assert running_started.wait(timeout=3)
            assert queued_started.wait(timeout=3), "aggregate mode stopped independent work"
        finally:
            release_running.set()

        runner.join(timeout=3)
        assert not runner.is_alive()
        errors, skipped = run_results[0]
        assert len(errors) == 1
        assert errors[0].node == "fail"
        assert errors[0].__cause__ is failure
        assert skipped == ["failed_child"]
        assert set(called) == {"running", "queued", "running_child", "queued_child"}
        """
    )


@pytest.mark.parametrize("tasks_num", [1, 3])
def test_concurrent_apply_to_values_copies_admission_context(tasks_num: int):
    value = ContextVar("admission", default="worker")
    token = value.set("admitted")

    def process(item: int) -> str:
        assert value.get() == "admitted"
        value.set(str(item))
        return value.get()

    try:
        assert concurrent_apply_to_values(
            [1, 2, 3], process, tasks_num, context_factory=copy_context
        ) == ["1", "2", "3"]
        assert value.get() == "admitted"
        default_results = concurrent_apply_to_values([1, 2, 3], lambda _: value.get(), tasks_num)
        expected = "admitted" if tasks_num == 1 else "worker"
        assert default_results == [expected, expected, expected]
    finally:
        value.reset(token)


@pytest.mark.parametrize("tasks_num", [1, 2])
def test_context_admission_and_sibling_isolation(tasks_num: int):
    from sqlmesh.utils.concurrency import ConcurrentDAGExecutor, concurrent_apply_to_dag
    from sqlmesh.utils.dag import DAG

    consumer = ContextVar("admission", default="construction")
    dag: DAG[str] = DAG()
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


@pytest.mark.parametrize("phase", ["start", "finish"])
def test_concurrent_dag_propagates_observer_base_exception_and_can_be_reused(phase: str):
    _run_bounded_python(
        f"""
        from threading import Event, Thread

        from sqlmesh.core.execution_observation import action, observer_scope
        from sqlmesh.utils.concurrency import ConcurrentDAGExecutor
        from sqlmesh.utils.dag import DAG

        dag = DAG()
        dag.add("cancel")
        dag.add("sibling")
        dag.add("dependent", ["sibling"])
        error = KeyboardInterrupt("cancel execution")
        release_sibling = Event()
        sibling_started = Event()
        called = []
        failing_phase = {phase!r}

        def observe(current_phase, facts, callback_error):
            if current_phase == failing_phase:
                raise error

        def execute(node):
            if node == "cancel":
                assert sibling_started.wait(timeout=3)
                with observer_scope(observe), action("query", "execute"):
                    pass
            if node == "sibling":
                sibling_started.set()
                assert release_sibling.wait(timeout=6)
            called.append(node)

        executor = ConcurrentDAGExecutor(dag, execute, 2, True)
        run_errors = []

        def run():
            try:
                executor.run()
            except BaseException as ex:
                run_errors.append(ex)

        runner = Thread(target=run)
        runner.start()
        assert sibling_started.wait(timeout=3)
        try:
            try:
                executor._finished_future.result(timeout=3)
            except KeyboardInterrupt as raised:
                assert raised is error
            else:
                raise AssertionError("the worker cancellation was not propagated")
        finally:
            release_sibling.set()

        runner.join(timeout=3)
        assert not runner.is_alive()
        assert run_errors == [error]
        assert "dependent" not in called

        second_error = SystemExit("cancel reused executor")

        def cancel_again(node):
            raise second_error

        executor.fn = cancel_again
        try:
            executor.run()
        except SystemExit as raised:
            assert raised is second_error
        else:
            raise AssertionError("the reused executor did not propagate cancellation")
        """
    )
