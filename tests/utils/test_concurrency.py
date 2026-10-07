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


def test_node_execution_failed_error_includes_cause():
    error = NodeExecutionFailedError(SnapshotId(name="model", identifier="snapshot"))
    error.__cause__ = RuntimeError("driver error")

    assert str(error) == ("Execution failed for node SnapshotId<model: snapshot>: driver error")


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
