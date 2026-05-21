import typing as t
from unittest.mock import call

import pytest
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.config import EnvironmentSuffixTarget
from sqlmesh.core import constants as c
from sqlmesh.core.dialect import parse_one, schema_
from sqlmesh.core.engine_adapter import create_engine_adapter
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import (
    ModelKindName,
    SqlModel,
)
from sqlmesh.core.model.definition import ExternalModel
from sqlmesh.core.snapshot import (
    SnapshotChangeCategory,
)
from sqlmesh.core.state_sync import (
    EngineAdapterStateSync,
)
from sqlmesh.core.janitor import cleanup_expired_views, delete_expired_snapshots
from sqlmesh.utils.date import now_timestamp

pytestmark = pytest.mark.slow


@pytest.fixture
def state_sync(duck_conn, tmp_path):
    state_sync = EngineAdapterStateSync(
        create_engine_adapter(lambda: duck_conn, "duckdb"),
        schema=c.SQLMESH,
        cache_dir=tmp_path / c.CACHE,
    )
    state_sync.migrate()
    return state_sync


def test_cleanup_expired_views(mocker: MockerFixture, make_snapshot: t.Callable):
    adapter = mocker.MagicMock()
    adapter.dialect = None
    snapshot_a = make_snapshot(SqlModel(name="catalog.schema.a", query=parse_one("select 1, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_b = make_snapshot(SqlModel(name="catalog.schema.b", query=parse_one("select 1, ds")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)
    # Make sure that we don't drop schemas from external models
    snapshot_external_model = make_snapshot(
        ExternalModel(name="catalog.external_schema.external_table", kind=ModelKindName.EXTERNAL)
    )
    snapshot_external_model.categorize_as(SnapshotChangeCategory.BREAKING)
    schema_environment = Environment(
        name="test_environment",
        suffix_target=EnvironmentSuffixTarget.SCHEMA,
        snapshots=[
            snapshot_a.table_info,
            snapshot_b.table_info,
            snapshot_external_model.table_info,
        ],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        catalog_name_override="catalog_override",
    )
    snapshot_c = make_snapshot(SqlModel(name="catalog.schema.c", query=parse_one("select 1, ds")))
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_d = make_snapshot(SqlModel(name="catalog.schema.d", query=parse_one("select 1, ds")))
    snapshot_d.categorize_as(SnapshotChangeCategory.BREAKING)
    table_environment = Environment(
        name="test_environment",
        suffix_target=EnvironmentSuffixTarget.TABLE,
        snapshots=[
            snapshot_c.table_info,
            snapshot_d.table_info,
            snapshot_external_model.table_info,
        ],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        catalog_name_override="catalog_override",
    )
    cleanup_expired_views(adapter, {}, [schema_environment, table_environment])
    assert adapter.drop_schema.called
    assert adapter.drop_view.called
    assert adapter.drop_schema.call_args_list == [
        call(
            schema_("schema__test_environment", "catalog_override"),
            ignore_if_not_exists=True,
            cascade=True,
        )
    ]
    assert sorted(adapter.drop_view.call_args_list) == [
        call("catalog_override.schema.c__test_environment", ignore_if_not_exists=True),
        call("catalog_override.schema.d__test_environment", ignore_if_not_exists=True),
    ]


@pytest.mark.parametrize(
    "suffix_target", [EnvironmentSuffixTarget.SCHEMA, EnvironmentSuffixTarget.TABLE]
)
def test_cleanup_expired_views_collects_failures(
    mocker: MockerFixture, make_snapshot: t.Callable, suffix_target: EnvironmentSuffixTarget
):
    adapter = mocker.MagicMock()
    adapter.dialect = None
    adapter.drop_schema.side_effect = Exception("Failed to drop the schema")
    adapter.drop_view.side_effect = Exception("Failed to drop the view")

    snapshot = make_snapshot(
        SqlModel(name="test_catalog.test_schema.test_model", query=parse_one("select 1, ds"))
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    schema_environment = Environment(
        name="test_environment",
        suffix_target=suffix_target,
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        catalog_name_override="catalog_override",
    )

    # Janitor is now best-effort: failures are returned, not raised.
    failures = cleanup_expired_views(adapter, {}, [schema_environment])
    assert len(failures) == 1
    assert "Failed to drop the expired environment" in failures[0]

    if suffix_target == EnvironmentSuffixTarget.SCHEMA:
        assert adapter.drop_schema.called
    else:
        assert adapter.drop_view.called


def test_cleanup_expired_views_continues_past_failures(
    mocker: MockerFixture, make_snapshot: t.Callable
):
    adapter = mocker.MagicMock()
    adapter.dialect = None

    snapshot_failing = make_snapshot(
        SqlModel(name="catalog.schema.failing", query=parse_one("select 1"))
    )
    snapshot_failing.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_succeeding = make_snapshot(
        SqlModel(name="catalog.schema.succeeding", query=parse_one("select 1"))
    )
    snapshot_succeeding.categorize_as(SnapshotChangeCategory.BREAKING)

    failing_env = Environment(
        name="failing_env",
        suffix_target=EnvironmentSuffixTarget.TABLE,
        snapshots=[snapshot_failing.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="p",
        previous_plan_id="p",
    )
    succeeding_env = Environment(
        name="succeeding_env",
        suffix_target=EnvironmentSuffixTarget.TABLE,
        snapshots=[snapshot_succeeding.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="p",
        previous_plan_id="p",
    )

    def drop_view_side_effect(view, ignore_if_not_exists=True):
        if "failing" in str(view):
            raise Exception("boom")

    adapter.drop_view.side_effect = drop_view_side_effect

    failures = cleanup_expired_views(adapter, {}, [failing_env, succeeding_env])

    # Both drops were attempted
    assert adapter.drop_view.call_count == 2

    # Only the failing one is reported
    assert len(failures) == 1
    assert "failing" in failures[0]


def test_delete_expired_snapshots_common_function_batching(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable, mocker: MockerFixture
):
    """Test that the common delete_expired_snapshots function properly pages through batches and deletes them."""
    from sqlmesh.core.state_sync.common import ExpiredBatchRange, RowBoundary, LimitBoundary
    from unittest.mock import MagicMock

    now_ts = now_timestamp()

    # Create 5 expired snapshots with different timestamps
    snapshots = []
    for idx in range(5):
        snapshot = make_snapshot(
            SqlModel(
                name=f"model_{idx}",
                query=parse_one("select 1 as a, ds"),
            ),
        )
        snapshot.ttl = "in 10 seconds"
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
        snapshot.updated_ts = now_ts - (20000 + idx * 1000)
        snapshots.append(snapshot)

    state_sync.push_snapshots(snapshots)

    # Spy on get_expired_snapshots and delete_expired_snapshots methods
    get_expired_spy = mocker.spy(state_sync, "get_expired_snapshots")
    delete_expired_spy = mocker.spy(state_sync, "delete_expired_snapshots")

    # Mock snapshot evaluator
    mock_evaluator = MagicMock()
    mock_evaluator.cleanup = MagicMock()

    # Run delete_expired_snapshots with batch_size=2
    delete_expired_snapshots(
        state_sync,
        mock_evaluator,
        current_ts=now_ts,
        batch_size=2,
    )

    # Verify get_expired_snapshots was called the correct number of times:
    # - 3 batches (2+2+1): each batch triggers 2 calls (one from iter_expired_snapshot_batches, one from delete_expired_snapshots)
    # - Plus 1 final call that returns empty to exit the loop
    # Total: 3 * 2 + 1 = 7 calls
    assert get_expired_spy.call_count == 7

    # Verify the progression of batch_range calls from the iter_expired_snapshot_batches loop
    # (calls at indices 0, 2, 4, 6 are from iter_expired_snapshot_batches)
    # (calls at indices 1, 3, 5 are from delete_expired_snapshots in facade.py)
    calls = get_expired_spy.call_args_list

    # First call from iterator should have a batch_range starting from the beginning
    first_call_kwargs = calls[0][1]
    assert "batch_range" in first_call_kwargs
    first_range = first_call_kwargs["batch_range"]
    assert isinstance(first_range, ExpiredBatchRange)
    assert isinstance(first_range.start, RowBoundary)
    assert isinstance(first_range.end, LimitBoundary)
    assert first_range.end.batch_size == 2
    assert first_range.start.updated_ts == 0
    assert first_range.start.name == ""
    assert first_range.start.identifier == ""

    # Third call (second batch from iterator) should have a batch_range from the first batch's range
    third_call_kwargs = calls[2][1]
    assert "batch_range" in third_call_kwargs
    second_range = third_call_kwargs["batch_range"]
    assert isinstance(second_range, ExpiredBatchRange)
    assert isinstance(second_range.start, RowBoundary)
    assert isinstance(second_range.end, LimitBoundary)
    assert second_range.end.batch_size == 2
    # Should have progressed from the first batch
    assert second_range.start.updated_ts > 0
    assert second_range.start.name == '"model_3"'

    # Fifth call (third batch from iterator) should have a batch_range from the second batch's range
    fifth_call_kwargs = calls[4][1]
    assert "batch_range" in fifth_call_kwargs
    third_range = fifth_call_kwargs["batch_range"]
    assert isinstance(third_range, ExpiredBatchRange)
    assert isinstance(third_range.start, RowBoundary)
    assert isinstance(third_range.end, LimitBoundary)
    assert third_range.end.batch_size == 2
    # Should have progressed from the second batch
    assert third_range.start.updated_ts >= second_range.start.updated_ts
    assert third_range.start.name == '"model_1"'

    # Seventh call (final call from iterator) should have a batch_range from the third batch's range
    seventh_call_kwargs = calls[6][1]
    assert "batch_range" in seventh_call_kwargs
    fourth_range = seventh_call_kwargs["batch_range"]
    assert isinstance(fourth_range, ExpiredBatchRange)
    assert isinstance(fourth_range.start, RowBoundary)
    assert isinstance(fourth_range.end, LimitBoundary)
    assert fourth_range.end.batch_size == 2
    # Should have progressed from the third batch
    assert fourth_range.start.updated_ts >= third_range.start.updated_ts
    assert fourth_range.start.name == '"model_0"'

    # Verify delete_expired_snapshots was called 3 times (once per batch)
    assert delete_expired_spy.call_count == 3

    # Verify each delete call used a batch_range
    delete_calls = delete_expired_spy.call_args_list

    # First call should have a batch_range matching the first batch
    first_delete_kwargs = delete_calls[0][1]
    assert "batch_range" in first_delete_kwargs
    first_delete_range = first_delete_kwargs["batch_range"]
    assert isinstance(first_delete_range, ExpiredBatchRange)
    assert isinstance(first_delete_range.start, RowBoundary)
    assert first_delete_range.start.updated_ts == 0
    assert isinstance(first_delete_range.end, RowBoundary)
    assert first_delete_range.end.updated_ts == second_range.start.updated_ts
    assert first_delete_range.end.name == second_range.start.name
    assert first_delete_range.end.identifier == second_range.start.identifier

    second_delete_kwargs = delete_calls[1][1]
    assert "batch_range" in second_delete_kwargs
    second_delete_range = second_delete_kwargs["batch_range"]
    assert isinstance(second_delete_range, ExpiredBatchRange)
    assert isinstance(second_delete_range.start, RowBoundary)
    assert second_delete_range.start.updated_ts == 0
    assert isinstance(second_delete_range.end, RowBoundary)
    assert second_delete_range.end.updated_ts == third_range.start.updated_ts
    assert second_delete_range.end.name == third_range.start.name
    assert second_delete_range.end.identifier == third_range.start.identifier

    third_delete_kwargs = delete_calls[2][1]
    assert "batch_range" in third_delete_kwargs
    third_delete_range = third_delete_kwargs["batch_range"]
    assert isinstance(third_delete_range, ExpiredBatchRange)
    assert isinstance(third_delete_range.start, RowBoundary)
    assert third_delete_range.start.updated_ts == 0
    assert isinstance(third_delete_range.end, RowBoundary)
    assert third_delete_range.end.updated_ts == fourth_range.start.updated_ts
    assert third_delete_range.end.name == fourth_range.start.name
    assert third_delete_range.end.identifier == fourth_range.start.identifier
    # Verify the cleanup method was called for each batch that had cleanup tasks
    assert mock_evaluator.cleanup.call_count >= 1

    # Verify all snapshots were deleted in the end
    remaining = state_sync.get_snapshots(snapshots)
    assert len(remaining) == 0
