import signal
import threading
import time
import typing as t

import pytest
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.snapshot import SnapshotId
from sqlmesh.utils.concurrency import (
    NodeExecutionFailedError,
    concurrent_apply_to_dag,
    concurrent_apply_to_snapshots,
    concurrent_apply_to_values,
)
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.windows import IS_WINDOWS


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


@pytest.mark.skipif(IS_WINDOWS, reason="signal.pthread_kill isn't available on Windows")
@pytest.mark.parametrize(
    "apply",
    [
        lambda fn: concurrent_apply_to_values([1, 2, 3, 4], fn, 2),
        lambda fn: concurrent_apply_to_dag(DAG({1: set(), 2: set(), 3: {1}, 4: {2}}), fn, 2),
    ],
    ids=["values", "dag"],
)
def test_interrupt_without_on_interrupt_waits_for_running_tasks(apply: t.Callable):
    started = []
    finished = []
    started_lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def fn(value: int) -> None:
        with started_lock:
            started.append(value)
            if len(started) == 2:
                both_started.set()
        assert release.wait(timeout=10)
        finished.append(value)

    def interrupt_main_thread_then_release() -> None:
        both_started.wait(timeout=10)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
        time.sleep(0.1)
        release.set()

    previous_sigint_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        threading.Thread(target=interrupt_main_thread_then_release).start()
        with pytest.raises(KeyboardInterrupt):
            apply(fn)
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        release.set()

    # The running tasks were waited for before the interrupt propagated, and the pending ones
    # never started.
    assert sorted(finished) == [1, 2]
    assert sorted(started) == [1, 2]


@pytest.mark.skipif(IS_WINDOWS, reason="signal.pthread_kill isn't available on Windows")
@pytest.mark.parametrize(
    "apply",
    [
        lambda fn, on_interrupt: concurrent_apply_to_values(
            [1, 2, 3, 4], fn, 2, on_interrupt=on_interrupt
        ),
        lambda fn, on_interrupt: concurrent_apply_to_dag(
            DAG({1: set(), 2: set(), 3: {1}, 4: {2}}), fn, 2, on_interrupt=on_interrupt
        ),
    ],
    ids=["values", "dag"],
)
def test_interrupt_calls_on_interrupt_then_waits_for_running_tasks(apply: t.Callable):
    started = []
    finished = []
    started_lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def fn(value: int) -> None:
        with started_lock:
            started.append(value)
            if len(started) == 2:
                both_started.set()
        assert release.wait(timeout=10)
        finished.append(value)

    def interrupt_main_thread() -> None:
        both_started.wait(timeout=10)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

    previous_sigint_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        threading.Thread(target=interrupt_main_thread).start()
        with pytest.raises(KeyboardInterrupt):
            apply(fn, release.set)
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        release.set()

    # The running tasks were released by `on_interrupt` and waited for before the interrupt
    # propagated, and the pending ones never started.
    assert sorted(finished) == [1, 2]
    assert sorted(started) == [1, 2]


def test_error_does_not_call_on_interrupt(mocker: MockerFixture):
    on_interrupt = mocker.Mock()

    def fn(value: int) -> None:
        raise RuntimeError("failed")

    with pytest.raises(NodeExecutionFailedError):
        concurrent_apply_to_dag(DAG({1: set()}), fn, 2, on_interrupt=on_interrupt)

    on_interrupt.assert_not_called()
