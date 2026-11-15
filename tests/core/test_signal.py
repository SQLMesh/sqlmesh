import pytest
from unittest.mock import MagicMock
from sqlglot import parse_one
from sqlmesh.core.context import ExecutionContext
from sqlmesh.core.model import SqlModel, IncrementalByTimeRangeKind
from sqlmesh.core.signal import freshness
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.utils.date import to_datetime, to_timestamp


@pytest.fixture
def test_snapshot(make_snapshot):
    model = SqlModel(
        name="test_model",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        cron="@daily",
        start="2023-01-01",
    )
    snap = make_snapshot(model)
    snap.categorize_as(SnapshotChangeCategory.BREAKING)
    snap.last_altered_ts = to_timestamp("2023-01-03")
    return snap


def test_freshness_signal_evaluates_restated_interval(test_snapshot):
    adapter = MagicMock()
    adapter.SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS = True

    context = ExecutionContext(
        engine_adapter=adapter,
        snapshots={"test_model": test_snapshot},
        is_restatement=False,
        parent_intervals=[],
        restated_interval=(to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    )

    batch = [
        (to_datetime("2023-01-01"), to_datetime("2023-01-02")),
    ]

    result = freshness(batch, test_snapshot, context)
    assert result == batch


def test_freshness_signal_mixed_batch_filters_unready_intervals(test_snapshot):
    adapter = MagicMock()
    adapter.SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS = True

    context = ExecutionContext(
        engine_adapter=adapter,
        snapshots={"test_model": test_snapshot},
        is_restatement=False,
        parent_intervals=[],
        restated_interval=(to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    )

    batch = [
        (to_datetime("2023-01-01"), to_datetime("2023-01-02")),
        (to_datetime("2023-01-05"), to_datetime("2023-01-06")),
    ]

    result = freshness(batch, test_snapshot, context)
    # Only the interval overlapping restated_interval should be ready
    assert result == [(to_datetime("2023-01-01"), to_datetime("2023-01-02"))]


def test_freshness_signal_returns_false_when_no_restatement_and_unaltered(test_snapshot):
    adapter = MagicMock()
    adapter.SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS = True

    context = ExecutionContext(
        engine_adapter=adapter,
        snapshots={"test_model": test_snapshot},
        is_restatement=False,
        parent_intervals=[],
        restated_interval=None,
    )

    batch = [
        (to_datetime("2023-01-01"), to_datetime("2023-01-02")),
    ]

    result = freshness(batch, test_snapshot, context)
    assert result is False


def test_freshness_signal_all_ready_when_is_restatement_is_true(test_snapshot):
    adapter = MagicMock()
    adapter.SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS = True

    context = ExecutionContext(
        engine_adapter=adapter,
        snapshots={"test_model": test_snapshot},
        is_restatement=True,
    )

    batch = [
        (to_datetime("2023-01-01"), to_datetime("2023-01-02")),
    ]

    result = freshness(batch, test_snapshot, context)
    assert result is True


def test_execution_context_with_variables_preserves_signal_context(test_snapshot):
    adapter = MagicMock()
    restated_interval = (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))
    parent_intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]

    context = ExecutionContext(
        engine_adapter=adapter,
        snapshots={"test_model": test_snapshot},
        is_restatement=False,
        parent_intervals=parent_intervals,
        restated_interval=restated_interval,
    )

    new_context = context.with_variables({"my_var": "val"})

    assert new_context.parent_intervals == parent_intervals
    assert new_context.restated_interval == restated_interval
    assert new_context.var("my_var") == "val"
