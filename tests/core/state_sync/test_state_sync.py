import json
import logging
import re
import typing as t
from unittest.mock import call, patch

import duckdb  # noqa: TID253
import pandas as pd  # noqa: TID253
import pytest
import time_machine
from pytest_mock.plugin import MockerFixture
from sqlglot import exp

from sqlmesh.core import constants as c
from sqlmesh.core.config import EnvironmentSuffixTarget
from sqlmesh.core.dialect import parse_one
from sqlmesh.core.engine_adapter import create_engine_adapter
from sqlmesh.core.environment import Environment, EnvironmentStatements
from sqlmesh.core.model import (
    FullKind,
    IncrementalByTimeRangeKind,
    Seed,
    SeedKind,
    SeedModel,
    SqlModel,
)
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotChangeCategory,
    SnapshotId,
    SnapshotIntervals,
    SnapshotNameVersion,
    SnapshotTableCleanupTask,
    missing_intervals,
)
from sqlmesh.core.state_sync import (
    CachingStateSync,
    EngineAdapterStateSync,
)
from sqlmesh.core.state_sync.base import (
    SCHEMA_VERSION,
    SQLGLOT_VERSION,
    Versions,
)
from sqlmesh.core.state_sync.common import (
    ExpiredBatchRange,
    LimitBoundary,
    PromotionResult,
    RowBoundary,
)
from sqlmesh.utils.date import now_timestamp, to_datetime, to_timestamp
from sqlmesh.utils.errors import SQLMeshError, StateMigrationError

pytestmark = pytest.mark.slow


def _get_cleanup_tasks(
    state_sync: EngineAdapterStateSync,
    *,
    limit: int = 1000,
    ignore_ttl: bool = False,
) -> t.List[SnapshotTableCleanupTask]:
    batch = state_sync.get_expired_snapshots(
        ignore_ttl=ignore_ttl,
        batch_range=ExpiredBatchRange.init_batch_range(batch_size=limit),
    )
    return [] if batch is None else batch.cleanup_tasks


@pytest.fixture
def state_sync(duck_conn, tmp_path):
    state_sync = EngineAdapterStateSync(
        create_engine_adapter(lambda: duck_conn, "duckdb"),
        schema=c.SQLMESH,
        cache_dir=tmp_path / c.CACHE,
    )
    state_sync.migrate()
    return state_sync


@pytest.fixture
def snapshots(make_snapshot: t.Callable) -> t.List[Snapshot]:
    return [
        make_snapshot(
            SqlModel(
                name="a",
                query=parse_one("select 1, ds"),
            ),
            version="a",
        ),
        make_snapshot(
            SqlModel(
                name="b",
                query=parse_one("select 2, ds"),
            ),
            version="b",
        ),
    ]


def compare_snapshot_intervals(x: SnapshotIntervals) -> str:
    return x.identifier or ""


def promote_snapshots(
    state_sync: EngineAdapterStateSync,
    snapshots: t.List[Snapshot],
    environment: str,
    no_gaps: bool = False,
    no_gaps_snapshot_names: t.Optional[t.Set[str]] = None,
    environment_suffix_target: EnvironmentSuffixTarget = EnvironmentSuffixTarget.SCHEMA,
    environment_catalog_mapping: t.Optional[t.Dict[re.Pattern, str]] = None,
) -> PromotionResult:
    env = Environment.from_environment_catalog_mapping(
        environment_catalog_mapping or {},
        name=environment,
        suffix_target=environment_suffix_target,
        snapshots=[snapshot.table_info for snapshot in snapshots],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
    )
    return state_sync.promote(
        env, no_gaps_snapshot_names=no_gaps_snapshot_names if no_gaps else set()
    )


def delete_versions(state_sync: EngineAdapterStateSync) -> None:
    state_sync.engine_adapter.drop_table(state_sync.version_state.versions_table)


def test_push_snapshots(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        )
    )
    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select 2, ds"),
        )
    )

    with pytest.raises(
        SQLMeshError,
        match=r".*has not been versioned.*",
    ):
        state_sync.push_snapshots([snapshot_a, snapshot_b])

    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_b.version = "2"

    state_sync.push_snapshots([snapshot_a, snapshot_b])

    assert state_sync.get_snapshots([snapshot_a.snapshot_id, snapshot_b.snapshot_id]) == {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }

    logger = logging.getLogger("sqlmesh.core.state_sync.db.facade")
    with patch.object(logger, "error") as mock_logger:
        state_sync.push_snapshots([snapshot_a])
        assert str({snapshot_a.snapshot_id}) == mock_logger.call_args[0][1]
        state_sync.push_snapshots([snapshot_a, snapshot_b])
        assert str({snapshot_a.snapshot_id, snapshot_b.snapshot_id}) == mock_logger.call_args[0][1]

    # test serialization
    state_sync.push_snapshots(
        [
            make_snapshot(
                SqlModel(
                    name="a",
                    kind=FullKind(),
                    query=parse_one(
                        """
            select 'x' + ' ' as y,
                    "z" + '\' as z,
        """
                    ),
                ),
                version="1",
            )
        ]
    )


def test_duplicates(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
        version="1",
    )
    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
        version="1",
    )
    snapshot_c = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
        version="1",
    )
    snapshot_b.updated_ts = snapshot_a.updated_ts + 1
    snapshot_c.updated_ts = 0
    state_sync.push_snapshots([snapshot_a])
    state_sync.snapshot_state.push_snapshots([snapshot_a])
    state_sync.snapshot_state.push_snapshots([snapshot_b])
    state_sync.snapshot_state.push_snapshots([snapshot_c])
    state_sync.snapshot_state.clear_cache()
    assert (
        state_sync.get_snapshots([snapshot_a])[snapshot_a.snapshot_id].updated_ts
        == snapshot_b.updated_ts
    )


def test_snapshots_exists(state_sync: EngineAdapterStateSync, snapshots: t.List[Snapshot]) -> None:
    state_sync.push_snapshots(snapshots)
    snapshot_ids = {snapshot.snapshot_id for snapshot in snapshots}
    assert state_sync.snapshots_exist(snapshot_ids) == snapshot_ids


@pytest.fixture
def get_snapshot_intervals(state_sync) -> t.Callable[[Snapshot], t.Optional[SnapshotIntervals]]:
    def _get_snapshot_intervals(snapshot: Snapshot) -> t.Optional[SnapshotIntervals]:
        intervals = state_sync.interval_state.get_snapshot_intervals([snapshot])
        return intervals[0] if intervals else None

    return _get_snapshot_intervals


def test_add_interval(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot])

    state_sync.add_interval(snapshot, "2020-01-01", "20200101")
    assert get_snapshot_intervals(snapshot).intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-02")),
    ]

    state_sync.add_interval(snapshot, "20200101", to_datetime("2020-01-04"))
    assert get_snapshot_intervals(snapshot).intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-04")),
    ]

    state_sync.add_interval(snapshot, to_datetime("2020-01-05"), "2020-01-10")
    assert get_snapshot_intervals(snapshot).intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-04")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-11")),
    ]

    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    state_sync.add_interval(snapshot, to_datetime("2020-01-16"), "2020-01-20", is_dev=True)
    intervals = get_snapshot_intervals(snapshot)
    assert intervals.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-04")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-11")),
    ]
    assert intervals.dev_intervals == [
        (to_timestamp("2020-01-16"), to_timestamp("2020-01-21")),
    ]


def test_add_interval_partial(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot])

    state_sync.add_interval(snapshot, "2023-01-01", to_timestamp("2023-01-01") + 1000)
    assert get_snapshot_intervals(snapshot) is None

    state_sync.add_interval(snapshot, "2023-01-01", to_timestamp("2023-01-02") + 1000)
    assert get_snapshot_intervals(snapshot).intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    ]


def test_remove_interval(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )
    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 2::INT, '2022-01-01'::TEXT AS ds"),
        ),
        version="a",
    )
    state_sync.push_snapshots([snapshot_a, snapshot_b])
    state_sync.add_interval(snapshot_a, "2020-01-01", "2020-01-10")
    state_sync.add_interval(snapshot_b, "2020-01-11", "2020-01-30")

    num_of_removals = 4
    for _ in range(num_of_removals):
        state_sync.remove_intervals(
            [(snapshot_a, snapshot_a.inclusive_exclusive("2020-01-15", "2020-01-17"))],
            remove_shared_versions=True,
        )

    remove_records_count = state_sync.engine_adapter.fetchone(
        "SELECT COUNT(*) FROM sqlmesh._intervals WHERE name = '\"a\"' AND version = 'a' AND is_removed"
    )[0]  # type: ignore
    assert remove_records_count == num_of_removals * 2  # 2 * snapshots

    snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])

    assert snapshots[snapshot_a.snapshot_id].intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-15")),
        (to_timestamp("2020-01-18"), to_timestamp("2020-01-31")),
    ]
    assert snapshots[snapshot_b.snapshot_id].intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-15")),
        (to_timestamp("2020-01-18"), to_timestamp("2020-01-31")),
    ]


def test_remove_interval_missing_snapshot(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )
    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 2::INT, '2022-01-01'::TEXT AS ds"),
        ),
        version="a",
    )
    state_sync.push_snapshots([snapshot_a, snapshot_b])
    state_sync.add_interval(snapshot_a, "2020-01-01", "2020-01-10")
    state_sync.add_interval(snapshot_b, "2020-01-11", "2020-01-30")
    # Remove snapshot b in order to test the scenario where it is missing
    state_sync.delete_snapshots([snapshot_b.snapshot_id])

    snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
    assert len(snapshots) == 1
    assert snapshots[snapshot_a.snapshot_id].intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-31")),
    ]

    state_sync.remove_intervals(
        [(snapshot_a, snapshot_a.inclusive_exclusive("2020-01-15", "2020-01-17"))],
        remove_shared_versions=True,
    )

    snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
    assert len(snapshots) == 1
    assert snapshots[snapshot_a.snapshot_id].intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-15")),
        (to_timestamp("2020-01-18"), to_timestamp("2020-01-31")),
    ]


def test_refresh_snapshot_intervals(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot])
    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-01")
    assert not snapshot.intervals

    state_sync.refresh_snapshot_intervals([snapshot])
    assert snapshot.intervals == [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]


def test_get_snapshot_intervals(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable, get_snapshot_intervals
) -> None:
    state_sync.interval_state.SNAPSHOT_BATCH_SIZE = 1

    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot_a])
    state_sync.add_interval(snapshot_a, "2020-01-01", "2020-01-01")

    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 2, ds"),
        ),
        version="a",
    )
    state_sync.push_snapshots([snapshot_b])

    snapshot_c = make_snapshot(
        SqlModel(
            name="c",
            cron="@daily",
            query=parse_one("select 3, ds"),
        ),
        version="c",
    )
    state_sync.push_snapshots([snapshot_c])
    state_sync.add_interval(snapshot_c, "2020-01-03", "2020-01-03")

    a_intervals = get_snapshot_intervals(snapshot_a)
    c_intervals = get_snapshot_intervals(snapshot_c)
    assert a_intervals.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]
    assert c_intervals.intervals == [(to_timestamp("2020-01-03"), to_timestamp("2020-01-04"))]


def test_compact_intervals(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot])

    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-10")
    state_sync.add_interval(snapshot, "2020-01-11", "2020-01-15")
    state_sync.remove_intervals(
        [(snapshot, snapshot.inclusive_exclusive("2020-01-05", "2020-01-12"))]
    )
    state_sync.add_interval(snapshot, "2020-01-12", "2020-01-16")
    state_sync.remove_intervals(
        [(snapshot, snapshot.inclusive_exclusive("2020-01-14", "2020-01-16"))]
    )

    expected_intervals = [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        (to_timestamp("2020-01-12"), to_timestamp("2020-01-14")),
    ]

    assert get_snapshot_intervals(snapshot).intervals == expected_intervals

    state_sync.compact_intervals()
    assert get_snapshot_intervals(snapshot).intervals == expected_intervals

    # Make sure compaction is idempotent.
    state_sync.compact_intervals()
    assert get_snapshot_intervals(snapshot).intervals == expected_intervals


def test_compact_intervals_delete_batches(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    mocker: MockerFixture,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    delete_from_mock = mocker.patch.object(state_sync.engine_adapter, "delete_from")
    state_sync.interval_state.INTERVAL_BATCH_SIZE = 2

    state_sync.push_snapshots([snapshot])

    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-11")
    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-12")
    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-13")
    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-14")
    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-15")

    state_sync.compact_intervals()

    delete_from_mock.assert_has_calls(
        [call(state_sync.interval_state.intervals_table, mocker.ANY)] * 3
    )


def test_promote_snapshots(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b_old = make_snapshot(
        SqlModel(
            name="b",
            kind=FullKind(),
            query=parse_one("select 2 from a"),
        ),
        nodes={"a": snapshot_a.model},
    )
    snapshot_b_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            kind=FullKind(),
            query=parse_one("select * from a"),
        ),
        nodes={"a": snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_c = make_snapshot(
        SqlModel(
            name="c",
            query=parse_one("select 3, ds"),
        ),
    )
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)

    with pytest.raises(
        SQLMeshError,
        match=r"Missing snapshots.*",
    ):
        promote_snapshots(state_sync, [snapshot_a], "prod")

    state_sync.push_snapshots([snapshot_a, snapshot_b_old, snapshot_b, snapshot_c])

    promotion_result = promote_snapshots(state_sync, [snapshot_a, snapshot_b_old], "prod")

    assert set(promotion_result.added) == set([snapshot_a.table_info, snapshot_b_old.table_info])
    assert not promotion_result.removed
    assert not promotion_result.removed_environment_naming_info
    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_a, snapshot_b_old, snapshot_c],
        "prod",
    )
    assert set(promotion_result.added) == {
        snapshot_a.table_info,
        snapshot_b_old.table_info,
        snapshot_c.table_info,
    }
    assert not promotion_result.removed
    assert not promotion_result.removed_environment_naming_info

    prev_snapshot_b_old_updated_ts = snapshot_b_old.updated_ts
    prev_snapshot_c_updated_ts = snapshot_c.updated_ts

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_a, snapshot_b],
        "prod",
    )
    assert set(promotion_result.added) == {snapshot_a.table_info, snapshot_b.table_info}
    assert set(promotion_result.removed) == {snapshot_c.table_info}
    assert promotion_result.removed_environment_naming_info
    assert promotion_result.removed_environment_naming_info.suffix_target.is_schema
    assert (
        state_sync.get_snapshots([snapshot_c])[snapshot_c.snapshot_id].updated_ts
        > prev_snapshot_c_updated_ts
    )
    assert (
        state_sync.get_snapshots([snapshot_b_old])[snapshot_b_old.snapshot_id].updated_ts
        > prev_snapshot_b_old_updated_ts
    )

    snapshot_d = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 2, ds"),
        ),
    )
    snapshot_d.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_d])
    promotion_result = promote_snapshots(state_sync, [snapshot_d], "prod")
    assert set(promotion_result.added) == {snapshot_d.table_info}
    assert set(promotion_result.removed) == {snapshot_b.table_info}
    assert promotion_result.removed_environment_naming_info
    assert promotion_result.removed_environment_naming_info.suffix_target.is_schema


def test_promote_snapshots_suffix_change(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            kind=FullKind(),
            query=parse_one("select * from a"),
        ),
        nodes={"a": snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_a, snapshot_b])

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_a, snapshot_b],
        "prod",
        environment_suffix_target=EnvironmentSuffixTarget.TABLE,
    )

    assert set(promotion_result.added) == {snapshot_a.table_info, snapshot_b.table_info}
    assert not promotion_result.removed
    assert not promotion_result.removed_environment_naming_info

    snapshot_c = make_snapshot(
        SqlModel(
            name="c",
            query=parse_one("select 3, ds"),
        ),
    )
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_c])

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_b, snapshot_c],
        "prod",
        environment_suffix_target=EnvironmentSuffixTarget.SCHEMA,
    )

    # We still only add the snapshots that are included in the promotion
    assert set(promotion_result.added) == {snapshot_b.table_info, snapshot_c.table_info}
    # B does not get removed because the suffix target change doesn't affect it due to running in prod.
    assert set(promotion_result.removed) == {snapshot_a.table_info}
    # Make sure the removed suffix target is correctly seen as table
    assert promotion_result.removed_environment_naming_info is not None
    assert promotion_result.removed_environment_naming_info.suffix_target.is_table

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_b, snapshot_c],
        "dev",
        environment_suffix_target=EnvironmentSuffixTarget.SCHEMA,
    )

    # We still only add the snapshots that are included in the promotion
    assert set(promotion_result.added) == {snapshot_b.table_info, snapshot_c.table_info}
    assert len(promotion_result.removed) == 0
    assert promotion_result.removed_environment_naming_info is None

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_b, snapshot_c],
        "dev",
        environment_suffix_target=EnvironmentSuffixTarget.TABLE,
    )

    # All snapshots are promoted due to suffix target change
    assert set(promotion_result.added) == {
        snapshot_b.table_info,
        snapshot_c.table_info,
    }
    # All snapshots are removed due to suffix target change
    assert set(promotion_result.removed) == {
        snapshot_b.table_info,
        snapshot_c.table_info,
    }
    assert promotion_result.removed_environment_naming_info is not None
    assert promotion_result.removed_environment_naming_info.suffix_target.is_schema


def test_promote_snapshots_catalog_name_override_change(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot_a = make_snapshot(
        SqlModel(
            name="catalog1.schema.a",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(
            name="catalog1.schema.b",
            kind=FullKind(),
            query=parse_one("select * from a"),
        ),
        nodes={"a": snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_c = make_snapshot(
        SqlModel(
            name="catalog2.schema.c",
            kind=FullKind(),
            query=parse_one("select * from a"),
        ),
        nodes={"a": snapshot_a.model},
    )
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_a, snapshot_b, snapshot_c])

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_a, snapshot_b, snapshot_c],
        "prod",
        environment_catalog_mapping={},
    )

    assert set(promotion_result.added) == {
        snapshot_a.table_info,
        snapshot_b.table_info,
        snapshot_c.table_info,
    }
    assert not promotion_result.removed
    assert not promotion_result.removed_environment_naming_info

    snapshot_d = make_snapshot(
        SqlModel(
            name="catalog1.schema.d",
            query=parse_one("select 3, ds"),
        ),
    )
    snapshot_d.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_d])

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_b, snapshot_c, snapshot_d],
        "prod",
        environment_catalog_mapping={
            re.compile("^prod$"): "catalog1",
        },
    )

    # We still only add the snapshots that are included in the promotion which means removing A
    assert set(promotion_result.added) == {
        snapshot_b.table_info,
        snapshot_c.table_info,
        snapshot_d.table_info,
    }
    # C is removed because of the catalog change. The new one will be created in the new catalog.
    # B is not removed because it's catalog did not change and therefore removing would actually result
    # in dropping what we just added.
    # A is removed because it was explicitly removed from the promotion.
    assert set(promotion_result.removed) == {snapshot_a.table_info, snapshot_c.table_info}
    # Make sure the removed suffix target correctly has the old catalog name set
    assert promotion_result.removed_environment_naming_info
    assert promotion_result.removed_environment_naming_info.catalog_name_override is None

    promotion_result = promote_snapshots(
        state_sync,
        [snapshot_b, snapshot_c, snapshot_d],
        "prod",
        environment_catalog_mapping={
            re.compile("^prod$"): "catalog2",
        },
    )

    # All are added since their catalog was changed
    assert set(promotion_result.added) == {
        snapshot_b.table_info,
        snapshot_c.table_info,
        snapshot_d.table_info,
    }
    # All are removed since there were moved from their old catalog location
    # Note that C has a catalog set in the model definition of `catalog2` which is what we moved to so you might think
    # it shouldn't be removed, but its actual catalog was `catalog1` because of the previous override so therefore
    # it should be removed from `catalog1`.
    assert set(promotion_result.removed) == {
        snapshot_b.table_info,
        snapshot_c.table_info,
        snapshot_d.table_info,
    }
    # Make sure the removed suffix target correctly has the old catalog name set
    assert promotion_result.removed_environment_naming_info
    assert promotion_result.removed_environment_naming_info.catalog_name_override == "catalog1"


def test_promote_snapshots_parent_plan_id_mismatch(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])
    promote_snapshots(state_sync, [snapshot], "prod")

    new_environment = Environment(
        name="prod",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="new_plan_id",
        previous_plan_id="test_plan_id",
    )

    stale_new_environment = Environment(
        name="prod",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="stale_new_plan_id",
        previous_plan_id="test_plan_id",
    )

    state_sync.promote(new_environment)

    with pytest.raises(
        SQLMeshError,
        match=re.escape(
            "Another plan (new_plan_id) was applied to the target environment 'prod' while your current plan (stale_new_plan_id) was still in progress, interrupting it. Please re-apply your plan to resolve this error."
        ),
    ):
        state_sync.promote(stale_new_environment)


@pytest.mark.parametrize("environment_name", ["dev", "prod"])
def test_promote_environment_expired(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable, environment_name: str
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])
    promote_snapshots(state_sync, [snapshot], "dev")
    state_sync.finalize(state_sync.get_environment("dev"))
    state_sync.invalidate_environment("dev")

    new_environment = Environment(
        name=environment_name,
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="new_plan_id",
        previous_plan_id=None,  # No previous plan ID since it's technically a new environment
        expiration_ts=now_timestamp() + 3600,
    )
    assert new_environment.expiration_ts

    # This call shouldn't fail.
    promotion_result = state_sync.promote(new_environment)
    assert promotion_result.added == [snapshot.table_info]
    assert promotion_result.removed == []
    assert promotion_result.removed_environment_naming_info is None

    state_sync.finalize(new_environment)

    new_environment.previous_plan_id = new_environment.plan_id
    new_environment.plan_id = "another_plan_id"
    promotion_result = state_sync.promote(new_environment)

    #  Should be empty since the environment is no longer expired and nothing has changed
    assert promotion_result.removed == []
    assert promotion_result.removed_environment_naming_info is None
    if environment_name == "prod":
        assert promotion_result.added == []
    else:
        # We should always recreate views in dev environments
        assert promotion_result.added == [snapshot.table_info]


def test_promote_snapshots_no_gaps(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    model = SqlModel(
        name="a",
        query=parse_one("select 1, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2022-01-01",
    )

    snapshot = make_snapshot(model, version="a")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    state_sync.push_snapshots([snapshot])
    state_sync.add_interval(snapshot, "2022-01-01", "2022-01-02")
    promote_snapshots(state_sync, [snapshot], "prod", no_gaps=True)

    new_snapshot_same_version = make_snapshot(model, version="a")
    new_snapshot_same_version.change_category = SnapshotChangeCategory.INDIRECT_NON_BREAKING
    new_snapshot_same_version.fingerprint = snapshot.fingerprint.copy(
        update={"data_hash": "new_snapshot_same_version"}
    )
    state_sync.push_snapshots([new_snapshot_same_version])
    state_sync.add_interval(new_snapshot_same_version, "2022-01-03", "2022-01-03")
    promote_snapshots(state_sync, [new_snapshot_same_version], "prod", no_gaps=True)

    new_snapshot_missing_interval = make_snapshot(model, version="b")
    new_snapshot_missing_interval.change_category = SnapshotChangeCategory.BREAKING
    new_snapshot_missing_interval.fingerprint = snapshot.fingerprint.copy(
        update={"data_hash": "new_snapshot_missing_interval"}
    )
    state_sync.push_snapshots([new_snapshot_missing_interval])
    state_sync.add_interval(new_snapshot_missing_interval, "2022-01-01", "2022-01-02")
    with pytest.raises(
        SQLMeshError,
        match=r".*Detected missing intervals for model .*, interrupting your current plan. Please re-apply your plan to resolve this error.*",
    ):
        promote_snapshots(state_sync, [new_snapshot_missing_interval], "prod", no_gaps=True)

    new_snapshot_same_interval = make_snapshot(model, version="c")
    new_snapshot_same_interval.change_category = SnapshotChangeCategory.BREAKING
    new_snapshot_same_interval.fingerprint = snapshot.fingerprint.copy(
        update={"data_hash": "new_snapshot_same_interval"}
    )
    state_sync.push_snapshots([new_snapshot_same_interval])
    state_sync.add_interval(new_snapshot_same_interval, "2022-01-01", "2022-01-03")
    promote_snapshots(state_sync, [new_snapshot_same_interval], "prod", no_gaps=True)

    # We should skip the gaps check if the snapshot is not representative.
    promote_snapshots(
        state_sync,
        [new_snapshot_missing_interval],
        "prod",
        no_gaps=True,
        no_gaps_snapshot_names=set(),
    )


@time_machine.travel("2023-01-08 16:00:00 UTC", tick=False)
def test_promote_snapshots_no_gaps_lookback(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    model = SqlModel(
        name="a",
        cron="@hourly",
        query=parse_one("select 1, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds", lookback=1),
        start="2023-01-01",
    )

    snapshot = make_snapshot(model, version="a")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    state_sync.push_snapshots([snapshot])
    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-08 15:00:00")
    promote_snapshots(state_sync, [snapshot], "prod", no_gaps=True)

    assert now_timestamp() == to_timestamp("2023-01-08 16:00:00")

    new_snapshot_same_version = make_snapshot(model, version="b")
    new_snapshot_same_version.change_category = SnapshotChangeCategory.BREAKING
    new_snapshot_same_version.fingerprint = snapshot.fingerprint.copy(
        update={"data_hash": "new_snapshot_same_version"}
    )
    state_sync.push_snapshots([new_snapshot_same_version])
    state_sync.add_interval(new_snapshot_same_version, "2023-01-01", "2023-01-08 15:00:00")
    promote_snapshots(state_sync, [new_snapshot_same_version], "prod", no_gaps=True)


def test_finalize(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_a])
    promote_snapshots(state_sync, [snapshot_a], "prod")

    env = state_sync.get_environment("prod")
    assert env
    state_sync.finalize(env)

    env = state_sync.get_environment("prod")
    assert env
    assert env.finalized_ts is not None

    env.plan_id = "different_plan_id"
    with pytest.raises(
        SQLMeshError,
        match=re.escape(
            "Another plan (test_plan_id) was applied to the target environment 'prod' while your current plan (different_plan_id) was still in progress, interrupting it. Please re-apply your plan to resolve this error."
        ),
    ):
        state_sync.finalize(env)


def test_start_date_gap(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    model = SqlModel(
        name="a",
        query=parse_one("select 1, ds"),
        start="2022-01-01",
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        cron="@daily",
    )

    snapshot = make_snapshot(model, version="a")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    state_sync.push_snapshots([snapshot])
    state_sync.add_interval(snapshot, "2022-01-01", "2022-01-03")
    promote_snapshots(state_sync, [snapshot], "prod")

    model = SqlModel(
        name="a",
        query=parse_one("select 1, ds"),
        start="2022-01-02",
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        cron="@daily",
    )

    snapshot = make_snapshot(model, version="b")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    state_sync.push_snapshots([snapshot])
    state_sync.add_interval(snapshot, "2022-01-03", "2022-01-04")
    with pytest.raises(
        SQLMeshError,
        match=r".*Detected missing intervals for model .*, interrupting your current plan. Please re-apply your plan to resolve this error.*",
    ):
        promote_snapshots(state_sync, [snapshot], "prod", no_gaps=True)

    state_sync.add_interval(snapshot, "2022-01-02", "2022-01-03")
    promote_snapshots(state_sync, [snapshot], "prod", no_gaps=True)


def test_delete_expired_environments(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()

    env_a = Environment(
        name="test_environment_a",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts - 1000,
    )

    environment_statements = [
        EnvironmentStatements(
            before_all=["CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"],
            after_all=["CREATE OR REPLACE TABLE table_2 AS SELECT 'b'"],
            python_env={},
        )
    ]

    state_sync.promote(env_a, environment_statements=environment_statements)

    env_b = env_a.copy(update={"name": "test_environment_b", "expiration_ts": now_ts + 1000})
    state_sync.promote(env_b)

    env_a = Environment(**json.loads(env_a.json()))
    env_b = Environment(**json.loads(env_b.json()))

    assert state_sync.get_environment(env_a.name) == env_a
    assert state_sync.get_environment(env_b.name) == env_b

    assert not state_sync.get_environment_statements(env_b.name)
    assert state_sync.get_environment_statements(env_a.name) == environment_statements

    deleted_environments = state_sync.delete_expired_environments()
    assert deleted_environments == [env_a.summary]

    assert state_sync.get_environment(env_a.name) is None
    assert state_sync.get_environment(env_b.name) == env_b

    # Deleting the environments should remove the corresponding environment's statements
    assert state_sync.get_environment_statements(env_a.name) == []


def test_get_expired_environments_filtered_by_name(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()

    env_a = Environment(
        name="test_env_a",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts - 1000,
    )
    env_b = env_a.copy(update={"name": "test_env_b", "expiration_ts": now_ts - 500})
    env_c = env_a.copy(update={"name": "test_env_c", "expiration_ts": now_ts + 1000})

    state_sync.promote(env_a)
    state_sync.promote(env_b)
    state_sync.promote(env_c)

    # Both env_a and env_b are expired, but only env_a is returned when filtering by name
    expired_all = state_sync.get_expired_environments(current_ts=now_ts)
    assert {e.name for e in expired_all} == {"test_env_a", "test_env_b"}

    expired_filtered = state_sync.get_expired_environments(current_ts=now_ts, name="test_env_a")
    assert [e.name for e in expired_filtered] == ["test_env_a"]

    expired_non_expired = state_sync.get_expired_environments(current_ts=now_ts, name="test_env_c")
    assert expired_non_expired == []


def test_delete_expired_environments_filtered_by_name(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()

    env_a = Environment(
        name="test_env_a",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts - 1000,
    )
    env_b = env_a.copy(update={"name": "test_env_b", "expiration_ts": now_ts - 500})

    state_sync.promote(env_a)
    state_sync.promote(env_b)

    # Delete only env_a by name; env_b should survive
    deleted = state_sync.delete_expired_environments(name="test_env_a")
    assert [e.name for e in deleted] == ["test_env_a"]

    assert state_sync.get_environment("test_env_a") is None
    assert state_sync.get_environment("test_env_b") is not None


def test_get_expired_environments_nonexistent_name(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """get_expired_environments with a name that does not exist returns an empty list without error."""
    now_ts = now_timestamp()
    result = state_sync.get_expired_environments(current_ts=now_ts, name="nonexistent_env")
    assert result == []


def test_delete_expired_environments_non_expired_name_is_not_deleted(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """delete_expired_environments with a name whose expiration_ts is in the future does not delete it."""
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()

    env = Environment(
        name="non_expired_env",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts + 100_000,  # far future
    )
    state_sync.promote(env)

    deleted = state_sync.delete_expired_environments(current_ts=now_ts, name="non_expired_env")
    assert deleted == []
    assert state_sync.get_environment("non_expired_env") is not None


def test_invalidate_environment_sync_does_not_delete_sibling_expired_envs(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """invalidate_environment does not delete sibling environments that are already expired.

    When sync=True, _cleanup_environments is called with name=<target> so only the
    named environment is removed — other expired environments are left for the next
    janitor run.
    """
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()

    # env_sibling is already expired before we even start
    env_sibling = Environment(
        name="sibling_env",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts - 1000,
    )
    # env_target will be invalidated (expiration set to now) and then cleaned up
    env_target = Environment(
        name="target_env",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts + 100_000,
    )

    state_sync.promote(env_sibling)
    state_sync.promote(env_target)

    # Simulate the sync=True path: invalidate then clean up only target_env
    state_sync.invalidate_environment("target_env")
    state_sync.delete_expired_environments(name="target_env")

    # target_env is gone
    assert state_sync.get_environment("target_env") is None
    # sibling_env was already expired but was NOT touched by the targeted cleanup
    assert state_sync.get_environment("sibling_env") is not None


def test_delete_expired_snapshots(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    now_ts = now_timestamp()

    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.ttl = "in 10 seconds"
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version
    new_snapshot.updated_ts = now_ts - 11000

    all_snapshots = [snapshot, new_snapshot]
    state_sync.push_snapshots(all_snapshots)
    assert set(state_sync.get_snapshots(all_snapshots)) == {
        snapshot.snapshot_id,
        new_snapshot.snapshot_id,
    }

    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True),
        SnapshotTableCleanupTask(snapshot=new_snapshot.table_info, dev_table_only=False),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())

    assert not state_sync.get_snapshots(all_snapshots)


def test_get_expired_snapshot_batch(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    now_ts = now_timestamp()

    snapshots = []
    for idx in range(3):
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

    batch = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange.init_batch_range(batch_size=2),
    )
    assert batch is not None
    assert len(batch.expired_snapshot_ids) == 2
    assert len(batch.cleanup_tasks) == 2

    state_sync.delete_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=RowBoundary.lowest_boundary(),
            end=batch.batch_range.end,
        ),
    )

    next_batch = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch.batch_range.end,
            end=LimitBoundary(batch_size=2),
        ),
    )
    assert next_batch is not None
    assert len(next_batch.expired_snapshot_ids) == 1

    state_sync.delete_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=next_batch.batch_range.start,
            end=next_batch.batch_range.end,
        ),
    )

    assert (
        state_sync.get_expired_snapshots(
            batch_range=ExpiredBatchRange(
                start=next_batch.batch_range.end,
                end=LimitBoundary(batch_size=2),
            ),
        )
        is None
    )


def test_get_expired_snapshot_batch_same_timestamp(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """Test that pagination works correctly when multiple snapshots have the same updated_ts."""
    now_ts = now_timestamp()
    same_timestamp = now_ts - 20000

    snapshots = []
    for idx in range(5):
        snapshot = make_snapshot(
            SqlModel(
                name=f"model_{idx:02d}",  # Zero-padded to ensure deterministic name ordering
                query=parse_one("select 1 as a, ds"),
            ),
        )
        snapshot.ttl = "in 10 seconds"
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
        # All snapshots have the same updated_ts
        snapshot.updated_ts = same_timestamp
        snapshots.append(snapshot)

    state_sync.push_snapshots(snapshots)

    # Fetch first batch of 2
    batch1 = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange.init_batch_range(batch_size=2),
    )
    assert batch1 is not None
    assert len(batch1.expired_snapshot_ids) == 2
    assert sorted([x.name for x in batch1.expired_snapshot_ids]) == [
        '"model_00"',
        '"model_01"',
    ]

    # Fetch second batch of 2 using cursor from batch1
    batch2 = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch1.batch_range.end,
            end=LimitBoundary(batch_size=2),
        ),
    )
    assert batch2 is not None
    assert len(batch2.expired_snapshot_ids) == 2
    assert sorted([x.name for x in batch2.expired_snapshot_ids]) == [
        '"model_02"',
        '"model_03"',
    ]

    # Fetch third batch of 2 using cursor from batch2
    batch3 = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch2.batch_range.end,
            end=LimitBoundary(batch_size=2),
        ),
    )
    assert batch3 is not None
    assert sorted([x.name for x in batch3.expired_snapshot_ids]) == [
        '"model_04"',
    ]


def test_delete_expired_snapshots_batching_with_deletion(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """Test that delete_expired_snapshots properly deletes batches as it pages through them."""
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

    # Verify all 5 snapshots exist
    assert len(state_sync.get_snapshots(snapshots)) == 5

    # Get first batch of 2
    batch1 = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange.init_batch_range(batch_size=2),
    )
    assert batch1 is not None
    assert len(batch1.expired_snapshot_ids) == 2

    # Delete the first batch using batch_range
    state_sync.delete_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch1.batch_range.start,
            end=batch1.batch_range.end,
        ),
    )

    # Verify first 2 snapshots (model_0 and model_1, the oldest) are deleted and last 3 remain
    remaining = state_sync.get_snapshots(snapshots)
    assert len(remaining) == 3
    assert snapshots[0].snapshot_id in remaining  # model_0 (newest)
    assert snapshots[1].snapshot_id in remaining  # model_1
    assert snapshots[2].snapshot_id in remaining  # model_2
    assert snapshots[3].snapshot_id not in remaining  # model_3
    assert snapshots[4].snapshot_id not in remaining  # model_4 (oldest)

    # Get next batch of 2 (should start after batch1's boundary)
    batch2 = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch1.batch_range.end,
            end=LimitBoundary(batch_size=2),
        ),
    )
    assert batch2 is not None
    assert len(batch2.expired_snapshot_ids) == 2

    # Delete the second batch
    state_sync.delete_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch2.batch_range.start,
            end=batch2.batch_range.end,
        ),
    )

    # Verify only the last snapshot remains
    remaining = state_sync.get_snapshots(snapshots)
    assert len(remaining) == 1
    assert snapshots[0].snapshot_id in remaining  # model_0 (newest)
    assert snapshots[1].snapshot_id not in remaining  # model_1
    assert snapshots[2].snapshot_id not in remaining  # model_2
    assert snapshots[3].snapshot_id not in remaining  # model_3
    assert snapshots[4].snapshot_id not in remaining  # model_4 (oldest)

    # Get final batch
    batch3 = state_sync.get_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch2.batch_range.end,
            end=LimitBoundary(batch_size=2),
        ),
    )
    assert batch3 is not None
    assert len(batch3.expired_snapshot_ids) == 1

    # Delete the final batch
    state_sync.delete_expired_snapshots(
        batch_range=ExpiredBatchRange(
            start=batch3.batch_range.start,
            end=batch3.batch_range.end,
        ),
    )

    # Verify all snapshots are deleted
    assert len(state_sync.get_snapshots(snapshots)) == 0

    # Verify no more expired snapshots exist
    assert (
        state_sync.get_expired_snapshots(
            batch_range=ExpiredBatchRange(
                start=batch3.batch_range.end,
                end=LimitBoundary(batch_size=2),
            ),
        )
        is None
    )


def test_iterator_expired_snapshot_batch(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """Test the for_each_expired_snapshot_batch helper function."""
    from sqlmesh.core.state_sync.common import iter_expired_snapshot_batches

    now_ts = now_timestamp()

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

    # Track all batches processed
    batches_processed = []

    # Process with batch size of 2
    for batch in iter_expired_snapshot_batches(
        state_sync,
        current_ts=now_ts,
        ignore_ttl=False,
        batch_size=2,
    ):
        batches_processed.append(batch)

    # Should have processed 3 batches (2 + 2 + 1)
    assert len(batches_processed) == 3
    assert len(batches_processed[0].expired_snapshot_ids) == 2
    assert len(batches_processed[1].expired_snapshot_ids) == 2
    assert len(batches_processed[2].expired_snapshot_ids) == 1

    # Verify all snapshots were processed
    all_processed_ids = set()
    for batch in batches_processed:
        all_processed_ids.update(batch.expired_snapshot_ids)

    expected_ids = {s.snapshot_id for s in snapshots}
    assert all_processed_ids == expected_ids


@pytest.mark.parametrize(
    "start_boundary,end_boundary,expected_sql",
    [
        # Test with GT only (when end is LimitBoundary)
        (
            RowBoundary(updated_ts=0, name="", identifier=""),
            LimitBoundary(batch_size=100),
            "updated_ts > 0 OR (updated_ts = 0 AND name > '') OR (updated_ts = 0 AND name = '' AND identifier > '')",
        ),
        # Test with GT and LTE (when both are RowBoundary)
        (
            RowBoundary(updated_ts=1000, name="model_a", identifier="abc"),
            RowBoundary(updated_ts=2000, name="model_z", identifier="xyz"),
            "(updated_ts > 1000 OR (updated_ts = 1000 AND name > 'model_a') OR (updated_ts = 1000 AND name = 'model_a' AND identifier > 'abc')) AND (updated_ts < 2000 OR (updated_ts = 2000 AND name < 'model_z') OR (updated_ts = 2000 AND name = 'model_z' AND identifier <= 'xyz'))",
        ),
        # Test with zero timestamp
        (
            RowBoundary(updated_ts=0, name="", identifier=""),
            RowBoundary(updated_ts=1234567890, name="model_x", identifier="id_123"),
            "(updated_ts > 0 OR (updated_ts = 0 AND name > '') OR (updated_ts = 0 AND name = '' AND identifier > '')) AND (updated_ts < 1234567890 OR (updated_ts = 1234567890 AND name < 'model_x') OR (updated_ts = 1234567890 AND name = 'model_x' AND identifier <= 'id_123'))",
        ),
        # Test with same timestamp, different names
        (
            RowBoundary(updated_ts=5000, name="model_a", identifier="id_1"),
            RowBoundary(updated_ts=5000, name="model_b", identifier="id_2"),
            "(updated_ts > 5000 OR (updated_ts = 5000 AND name > 'model_a') OR (updated_ts = 5000 AND name = 'model_a' AND identifier > 'id_1')) AND (updated_ts < 5000 OR (updated_ts = 5000 AND name < 'model_b') OR (updated_ts = 5000 AND name = 'model_b' AND identifier <= 'id_2'))",
        ),
        # Test with same timestamp and name, different identifiers
        (
            RowBoundary(updated_ts=7000, name="model_x", identifier="id_a"),
            RowBoundary(updated_ts=7000, name="model_x", identifier="id_b"),
            "(updated_ts > 7000 OR (updated_ts = 7000 AND name > 'model_x') OR (updated_ts = 7000 AND name = 'model_x' AND identifier > 'id_a')) AND (updated_ts < 7000 OR (updated_ts = 7000 AND name < 'model_x') OR (updated_ts = 7000 AND name = 'model_x' AND identifier <= 'id_b'))",
        ),
        # Test all_batch_range use case
        (
            RowBoundary(updated_ts=0, name="", identifier=""),
            RowBoundary(updated_ts=253_402_300_799_999, name="", identifier=""),
            "(updated_ts > 0 OR (updated_ts = 0 AND name > '') OR (updated_ts = 0 AND name = '' AND identifier > '')) AND (updated_ts < 253402300799999 OR (updated_ts = 253402300799999 AND name < '') OR (updated_ts = 253402300799999 AND name = '' AND identifier <= ''))",
        ),
    ],
)
def test_expired_batch_range_where_filter(start_boundary, end_boundary, expected_sql):
    """Test ExpiredBatchRange.where_filter generates correct SQL for various boundary combinations."""
    batch_range = ExpiredBatchRange(start=start_boundary, end=end_boundary)
    result = batch_range.where_filter
    assert result.sql() == expected_sql


def test_expired_batch_range_where_filter_with_limit():
    """Test that where_filter correctly handles LimitBoundary (only start condition, no end condition)."""
    batch_range = ExpiredBatchRange(
        start=RowBoundary(updated_ts=1000, name="model_a", identifier="abc"),
        end=LimitBoundary(batch_size=50),
    )
    result = batch_range.where_filter
    # When end is LimitBoundary, should only have the start (GT) condition
    assert (
        result.sql()
        == "updated_ts > 1000 OR (updated_ts = 1000 AND name > 'model_a') OR (updated_ts = 1000 AND name = 'model_a' AND identifier > 'abc')"
    )


def test_delete_expired_snapshots_seed(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    now_ts = now_timestamp()

    snapshot = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="header\n1\n2"),
            column_hashes={"header": "hash"},
            depends_on=set(),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    all_snapshots = [snapshot]
    state_sync.push_snapshots(all_snapshots)
    assert set(state_sync.get_snapshots(all_snapshots)) == {snapshot.snapshot_id}

    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=False),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())

    assert not state_sync.get_snapshots(all_snapshots)


def test_delete_expired_snapshots_batching(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    state_sync.snapshot_state.SNAPSHOT_BATCH_SIZE = 1
    now_ts = now_timestamp()

    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot_a.ttl = "in 10 seconds"
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_a.updated_ts = now_ts - 15000

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select a, b, ds"),
        ),
    )
    snapshot_b.ttl = "in 10 seconds"
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_b.updated_ts = now_ts - 11000

    all_snapshots = [snapshot_a, snapshot_b]
    state_sync.push_snapshots(all_snapshots)
    assert set(state_sync.get_snapshots(all_snapshots)) == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot_a.table_info, dev_table_only=False),
        SnapshotTableCleanupTask(snapshot=snapshot_b.table_info, dev_table_only=False),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())

    assert not state_sync.get_snapshots(all_snapshots)


def test_delete_expired_snapshots_promoted(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable, mocker: MockerFixture
):
    now_ts = now_timestamp()

    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    state_sync.push_snapshots([snapshot])

    env = Environment(
        name="test_environment",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
    )
    state_sync.promote(env)

    all_snapshots = [snapshot]
    assert not _get_cleanup_tasks(state_sync)
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert set(state_sync.get_snapshots(all_snapshots)) == {snapshot.snapshot_id}

    env.snapshots_ = []
    state_sync.promote(env)

    now_timestamp_mock = mocker.patch("sqlmesh.core.state_sync.db.facade.now_timestamp")
    now_timestamp_mock.return_value = now_timestamp() + 11000

    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=False)
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert not state_sync.get_snapshots(all_snapshots)


def test_delete_expired_snapshots_previous_finalized_snapshots(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    """Test that expired snapshots are protected if they are part of previous finalized snapshots
    in a non-finalized environment."""
    now_ts = now_timestamp()

    # Create an old snapshot that will be expired
    old_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    old_snapshot.ttl = "in 10 seconds"
    old_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # Create a new snapshot
    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([old_snapshot, new_snapshot])

    # Promote the old snapshot to an environment and finalize it
    env = Environment(
        name="test_environment",
        snapshots=[old_snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
    )
    state_sync.promote(env)
    state_sync.finalize(env)

    # Verify old snapshot is not cleaned up because it's in a finalized environment
    assert not _get_cleanup_tasks(state_sync)

    # Now promote the new snapshot to the same environment (this simulates a new plan)
    # The environment will have previous_finalized_snapshots set to the old snapshot
    # and will not be finalized yet
    env = Environment(
        name="test_environment",
        snapshots=[new_snapshot.table_info],
        previous_finalized_snapshots=[old_snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="new_plan_id",
        previous_plan_id="test_plan_id",
    )
    state_sync.promote(env)

    # Manually update the snapshots updated_ts to simulate expiration
    state_sync.engine_adapter.execute(
        f"UPDATE sqlmesh._snapshots SET updated_ts = {now_ts - 15000} WHERE name = '{old_snapshot.name}' AND identifier = '{old_snapshot.identifier}'"
    )

    # The old snapshot should still not be cleaned up because it's part of
    # previous_finalized_snapshots in a non-finalized environment
    assert not _get_cleanup_tasks(state_sync)
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert state_sync.snapshots_exist([old_snapshot.snapshot_id]) == {old_snapshot.snapshot_id}

    # Once the environment is finalized, the expired snapshot should be removed successfully
    state_sync.finalize(env)
    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=old_snapshot.table_info, dev_table_only=False),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert not state_sync.snapshots_exist([old_snapshot.snapshot_id])


def test_delete_expired_snapshots_dev_table_cleanup_only(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    now_ts = now_timestamp()

    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.ttl = "in 10 seconds"
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version
    new_snapshot.updated_ts = now_ts - 5000

    all_snapshots = [snapshot, new_snapshot]
    state_sync.push_snapshots(all_snapshots)
    assert set(state_sync.get_snapshots(all_snapshots)) == {
        snapshot.snapshot_id,
        new_snapshot.snapshot_id,
    }

    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True)
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())

    assert set(state_sync.get_snapshots(all_snapshots)) == {new_snapshot.snapshot_id}


def test_delete_expired_snapshots_shared_dev_table(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    now_ts = now_timestamp()

    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.ttl = "in 10 seconds"
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version
    new_snapshot.dev_version_ = snapshot.dev_version
    new_snapshot.updated_ts = now_ts - 5000

    all_snapshots = [snapshot, new_snapshot]
    state_sync.push_snapshots(all_snapshots)
    assert set(state_sync.get_snapshots(all_snapshots)) == {
        snapshot.snapshot_id,
        new_snapshot.snapshot_id,
    }

    assert not _get_cleanup_tasks(state_sync)  # No dev table cleanup
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert set(state_sync.get_snapshots(all_snapshots)) == {new_snapshot.snapshot_id}


def test_delete_expired_snapshots_ignore_ttl(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        )
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
        version="2",
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    snapshot_c = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, c, ds"),
        ),
    )
    snapshot_c.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    state_sync.push_snapshots([snapshot_a, snapshot_b, snapshot_c])

    env = Environment(
        name="test_environment",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
    )
    state_sync.promote(env)

    # default TTL = 1 week, nothing to clean up yet if we take TTL into account
    assert not _get_cleanup_tasks(state_sync)
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert state_sync.snapshots_exist([snapshot_c.snapshot_id]) == {snapshot_c.snapshot_id}

    # If we ignore TTL, only snapshot_c should get cleaned up because snapshot_a and snapshot_b are part of an environment
    assert snapshot_a.table_info != snapshot_b.table_info != snapshot_c.table_info
    assert _get_cleanup_tasks(state_sync, ignore_ttl=True) == [
        SnapshotTableCleanupTask(snapshot=snapshot_c.table_info, dev_table_only=False)
    ]
    state_sync.delete_expired_snapshots(
        batch_range=ExpiredBatchRange.all_batch_range(), ignore_ttl=True
    )
    assert not state_sync.snapshots_exist([snapshot_c.snapshot_id])


def test_delete_expired_snapshots_cleanup_intervals(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
):
    now_ts = now_timestamp()

    # Expired snapshot
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    # Another expired snapshot with the same version
    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.ttl = "in 10 seconds"
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version
    new_snapshot.updated_ts = now_ts - 12000

    state_sync.push_snapshots([snapshot, new_snapshot])
    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-03")
    state_sync.add_interval(snapshot, "2023-01-04", "2023-01-05", is_dev=True)
    state_sync.add_interval(snapshot, "2023-01-06", "2023-01-07")
    state_sync.remove_intervals(
        [(snapshot, (to_timestamp("2023-01-06"), to_timestamp("2023-01-08")))]
    )

    state_sync.add_interval(new_snapshot, "2023-01-04", "2023-01-05")
    state_sync.add_interval(new_snapshot, "2023-01-06", "2023-01-07")
    state_sync.remove_intervals(
        [(new_snapshot, (to_timestamp("2023-01-06"), to_timestamp("2023-01-08")))]
    )

    # Check old snapshot's intervals
    stored_snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
    assert stored_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-06")),
    ]
    assert stored_snapshot.dev_intervals == [
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-06")),
    ]

    # Check new snapshot's intervals
    stored_new_snapshot = state_sync.get_snapshots([new_snapshot])[new_snapshot.snapshot_id]
    assert stored_new_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-06")),
    ]
    assert not stored_new_snapshot.dev_intervals

    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True),
        SnapshotTableCleanupTask(snapshot=new_snapshot.table_info, dev_table_only=False),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())

    assert not get_snapshot_intervals(snapshot)


def test_delete_expired_snapshots_cleanup_intervals_shared_version(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
):
    now_ts = now_timestamp()

    # Expired snapshot
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    # New non-expired snapshot with the same version
    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.ttl = "in 10 seconds"
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version
    new_snapshot.updated_ts = now_ts - 5000

    state_sync.push_snapshots([snapshot, new_snapshot])
    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-03")
    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-03", is_dev=True)
    state_sync.add_interval(new_snapshot, "2023-01-04", "2023-01-07")
    state_sync.remove_intervals(
        [(new_snapshot, (to_timestamp("2023-01-06"), to_timestamp("2023-01-08")))]
    )

    # Check new snapshot's intervals
    stored_new_snapshot = state_sync.get_snapshots([new_snapshot])[new_snapshot.snapshot_id]
    assert stored_new_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-06")),
    ]
    assert not stored_new_snapshot.dev_intervals

    # Check old snapshot's intervals
    stored_snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
    assert stored_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-06")),
    ]
    assert stored_snapshot.dev_intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-04")),
    ]

    # Check all intervals
    assert sorted(
        state_sync.interval_state.get_snapshot_intervals([snapshot, new_snapshot]),
        key=compare_snapshot_intervals,
    ) == sorted(
        [
            SnapshotIntervals(
                name='"a"',
                identifier=snapshot.identifier,
                version=snapshot.version,
                dev_version=snapshot.dev_version,
                intervals=[(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))],
                dev_intervals=[(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))],
            ),
            SnapshotIntervals(
                name='"a"',
                identifier=new_snapshot.identifier,
                version=snapshot.version,
                dev_version=new_snapshot.dev_version,
                intervals=[(to_timestamp("2023-01-04"), to_timestamp("2023-01-06"))],
            ),
        ],
        key=compare_snapshot_intervals,
    )

    # Delete the expired snapshot
    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert not state_sync.get_snapshots([snapshot])

    # Check new snapshot's intervals
    stored_new_snapshot = state_sync.get_snapshots([new_snapshot])[new_snapshot.snapshot_id]
    assert stored_new_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-06")),
    ]
    assert not stored_new_snapshot.dev_intervals

    # Check all intervals
    assert sorted(
        state_sync.interval_state.get_snapshot_intervals([snapshot, new_snapshot]),
        key=compare_snapshot_intervals,
    ) == sorted(
        [
            # The intervals of the old snapshot is preserved with the null identifier
            SnapshotIntervals(
                name='"a"',
                identifier=None,
                version=snapshot.version,
                dev_version=None,
                intervals=[(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))],
            ),
            # The intervals of the new snapshot has identifier
            SnapshotIntervals(
                name='"a"',
                identifier=new_snapshot.identifier,
                version=snapshot.version,
                dev_version=new_snapshot.dev_version,
                intervals=[(to_timestamp("2023-01-04"), to_timestamp("2023-01-06"))],
            ),
        ],
        key=compare_snapshot_intervals,
    )


def test_delete_expired_snapshots_cleanup_intervals_shared_dev_version(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    now_ts = now_timestamp()

    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.ttl = "in 10 seconds"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.updated_ts = now_ts - 15000

    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    new_snapshot.ttl = "in 10 seconds"
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version
    new_snapshot.dev_version_ = snapshot.dev_version
    new_snapshot.updated_ts = now_ts - 5000

    state_sync.push_snapshots([snapshot, new_snapshot])

    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-03")
    state_sync.add_interval(snapshot, "2023-01-04", "2023-01-07", is_dev=True)
    state_sync.add_interval(new_snapshot, "2023-01-08", "2023-01-10", is_dev=True)
    state_sync.remove_intervals(
        [(new_snapshot, (to_timestamp("2023-01-10"), to_timestamp("2023-01-11")))]
    )

    # Check new snapshot's intervals
    stored_new_snapshot = state_sync.get_snapshots([new_snapshot])[new_snapshot.snapshot_id]
    assert stored_new_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-04")),
    ]
    assert stored_new_snapshot.dev_intervals == [
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-11")),
    ]

    # Check old snapshot's intervals
    stored_snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
    assert stored_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-04")),
    ]
    assert stored_snapshot.dev_intervals == [
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-11")),
    ]

    # Check all intervals
    assert sorted(
        state_sync.interval_state.get_snapshot_intervals([snapshot, new_snapshot]),
        key=compare_snapshot_intervals,
    ) == sorted(
        [
            SnapshotIntervals(
                name='"a"',
                identifier=snapshot.identifier,
                version=snapshot.version,
                dev_version=snapshot.dev_version,
                intervals=[(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))],
                dev_intervals=[(to_timestamp("2023-01-04"), to_timestamp("2023-01-08"))],
            ),
            SnapshotIntervals(
                name='"a"',
                identifier=new_snapshot.identifier,
                version=snapshot.version,
                dev_version=new_snapshot.dev_version,
                dev_intervals=[(to_timestamp("2023-01-08"), to_timestamp("2023-01-11"))],
            ),
        ],
        key=compare_snapshot_intervals,
    )

    # Delete the expired snapshot
    assert not _get_cleanup_tasks(state_sync)
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())
    assert not state_sync.get_snapshots([snapshot])

    # Check new snapshot's intervals
    stored_new_snapshot = state_sync.get_snapshots([new_snapshot])[new_snapshot.snapshot_id]
    assert stored_new_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-04")),
    ]
    assert stored_new_snapshot.dev_intervals == [
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-11")),
    ]

    # Check all intervals
    assert sorted(
        state_sync.interval_state.get_snapshot_intervals([snapshot, new_snapshot]),
        key=compare_snapshot_intervals,
    ) == sorted(
        [
            SnapshotIntervals(
                name='"a"',
                identifier=None,
                version=snapshot.version,
                dev_version=None,
                intervals=[(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))],
            ),
            SnapshotIntervals(
                name='"a"',
                identifier=None,
                version=snapshot.version,
                dev_version=snapshot.dev_version,
                dev_intervals=[(to_timestamp("2023-01-04"), to_timestamp("2023-01-08"))],
            ),
            SnapshotIntervals(
                name='"a"',
                identifier=new_snapshot.identifier,
                version=snapshot.version,
                dev_version=new_snapshot.dev_version,
                dev_intervals=[(to_timestamp("2023-01-08"), to_timestamp("2023-01-11"))],
            ),
        ],
        key=compare_snapshot_intervals,
    )


def test_compact_intervals_after_cleanup(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    now_ts = now_timestamp()

    # Original expired snapshot
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot_a.ttl = "in 10 seconds"
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_a.updated_ts = now_ts - 15000

    # A forward-only change on top of the original snapshot. Also expired.
    # This snapshot reuses only prod table
    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, b, ds"),
        ),
    )
    snapshot_b.previous_versions = snapshot_a.all_versions
    snapshot_b.ttl = "in 10 seconds"
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_b.updated_ts = now_ts - 12000

    # An indirect non-breaking change on top of the forward-only change. Not expired.
    # This snapshot reuses both prod and dev tables
    snapshot_c = make_snapshot(snapshot_b.model.copy(update={"stamp": "1"}))
    snapshot_c.previous_versions = snapshot_b.all_versions
    snapshot_c.ttl = "in 10 seconds"
    snapshot_c.change_category = SnapshotChangeCategory.INDIRECT_NON_BREAKING
    snapshot_c.version = snapshot_b.version
    snapshot_c.dev_version_ = snapshot_b.dev_version
    snapshot_c.updated_ts = now_ts - 5000

    state_sync.push_snapshots([snapshot_a, snapshot_b, snapshot_c])

    state_sync.add_interval(snapshot_a, "2023-01-01", "2023-01-03")
    state_sync.add_interval(snapshot_a, "2023-01-01", "2023-01-03", is_dev=True)
    state_sync.add_interval(snapshot_b, "2023-01-04", "2023-01-06")
    state_sync.add_interval(snapshot_b, "2023-01-04", "2023-01-06", is_dev=True)
    state_sync.add_interval(snapshot_c, "2023-01-07", "2023-01-09")
    state_sync.add_interval(snapshot_c, "2023-01-07", "2023-01-09", is_dev=True)

    # Only the dev table of the original snapshot should be deleted
    assert _get_cleanup_tasks(state_sync) == [
        SnapshotTableCleanupTask(snapshot=snapshot_a.table_info, dev_table_only=True),
    ]
    state_sync.delete_expired_snapshots(batch_range=ExpiredBatchRange.all_batch_range())

    assert state_sync.engine_adapter.fetchone("SELECT COUNT(*) FROM sqlmesh._intervals")[0] == 5  # type: ignore

    expected_intervals = [
        # Combined intervals from the original and the forward-only expired snapshots
        SnapshotIntervals(
            name='"a"',
            identifier=None,
            version=snapshot_a.version,
            dev_version=None,
            intervals=[(to_timestamp("2023-01-01"), to_timestamp("2023-01-07"))],
        ),
        # Dev intervals from the forward-only expired snapshot
        SnapshotIntervals(
            name='"a"',
            identifier=None,
            version=snapshot_b.version,
            dev_version=snapshot_b.dev_version,
            dev_intervals=[(to_timestamp("2023-01-04"), to_timestamp("2023-01-07"))],
        ),
        # Intervals from the indirect non-breaking snapshot
        SnapshotIntervals(
            name='"a"',
            identifier=snapshot_c.identifier,
            version=snapshot_c.version,
            dev_version=snapshot_c.dev_version,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-10"))],
            dev_intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-10"))],
        ),
    ]

    assert (
        sorted(
            state_sync.interval_state.get_snapshot_intervals([snapshot_a, snapshot_b, snapshot_c]),
            key=lambda x: (x.identifier or "", x.dev_version or ""),
        )
        == expected_intervals
    )

    state_sync.compact_intervals()

    assert state_sync.engine_adapter.fetchone("SELECT COUNT(*) FROM sqlmesh._intervals")[0] == 4  # type: ignore
    assert (
        sorted(
            state_sync.interval_state.get_snapshot_intervals([snapshot_a, snapshot_b, snapshot_c]),
            key=lambda x: (x.identifier or "", x.dev_version or ""),
        )
        == expected_intervals
    )


def test_environment_start_as_timestamp(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()

    env = Environment(
        name="test_environment_a",
        snapshots=[snapshot.table_info],
        start_at=now_ts,
        end_at=None,
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=now_ts - 1000,
    )
    state_sync.promote(env)

    stored_env = state_sync.get_environment(env.name)
    assert stored_env
    assert stored_env.start_at == to_datetime(now_ts).replace(tzinfo=None).isoformat(sep=" ")


def test_unpause_snapshots(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot = make_snapshot(
        SqlModel(
            name="test_snapshot",
            query=parse_one("select 1, ds"),
            cron="@daily",
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.version = "a"

    assert not snapshot.unpaused_ts
    state_sync.push_snapshots([snapshot])

    unpaused_dt = "2022-01-01"
    state_sync.unpause_snapshots([snapshot], unpaused_dt)

    actual_snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
    assert actual_snapshot.unpaused_ts
    assert actual_snapshot.unpaused_ts == to_timestamp(unpaused_dt)

    new_snapshot = make_snapshot(
        SqlModel(name="test_snapshot", query=parse_one("select 2, ds"), cron="@daily")
    )
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = "a"

    assert not new_snapshot.unpaused_ts
    state_sync.push_snapshots([new_snapshot])
    state_sync.unpause_snapshots([new_snapshot], unpaused_dt)

    actual_snapshots = state_sync.get_snapshots([snapshot, new_snapshot])
    assert not actual_snapshots[snapshot.snapshot_id].unpaused_ts
    assert actual_snapshots[new_snapshot.snapshot_id].unpaused_ts == to_timestamp(unpaused_dt)

    assert actual_snapshots[snapshot.snapshot_id].unrestorable
    assert not actual_snapshots[new_snapshot.snapshot_id].unrestorable


def test_unrestorable_snapshot(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot = make_snapshot(
        SqlModel(
            name="test_snapshot",
            query=parse_one("select 1, ds"),
            cron="@daily",
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.version = "a"

    assert not snapshot.unpaused_ts
    state_sync.push_snapshots([snapshot])

    unpaused_dt = "2022-01-01"
    state_sync.unpause_snapshots([snapshot], unpaused_dt)

    actual_snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
    assert actual_snapshot.unpaused_ts
    assert actual_snapshot.unpaused_ts == to_timestamp(unpaused_dt)

    new_indirect_non_breaking_snapshot = make_snapshot(
        SqlModel(name="test_snapshot", query=parse_one("select 2, ds"), cron="@daily")
    )
    new_indirect_non_breaking_snapshot.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING)
    new_indirect_non_breaking_snapshot.version = "a"

    assert not new_indirect_non_breaking_snapshot.unpaused_ts
    state_sync.push_snapshots([new_indirect_non_breaking_snapshot])
    state_sync.unpause_snapshots([new_indirect_non_breaking_snapshot], unpaused_dt)

    actual_snapshots = state_sync.get_snapshots([snapshot, new_indirect_non_breaking_snapshot])
    assert not actual_snapshots[snapshot.snapshot_id].unpaused_ts
    assert actual_snapshots[
        new_indirect_non_breaking_snapshot.snapshot_id
    ].unpaused_ts == to_timestamp(unpaused_dt)

    assert not actual_snapshots[snapshot.snapshot_id].unrestorable
    assert not actual_snapshots[new_indirect_non_breaking_snapshot.snapshot_id].unrestorable

    new_forward_only_snapshot = make_snapshot(
        SqlModel(name="test_snapshot", query=parse_one("select 3, ds"), cron="@daily")
    )
    new_forward_only_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_forward_only_snapshot.version = "a"

    assert not new_forward_only_snapshot.unpaused_ts
    state_sync.push_snapshots([new_forward_only_snapshot])
    state_sync.unpause_snapshots([new_forward_only_snapshot], unpaused_dt)

    actual_snapshots = state_sync.get_snapshots(
        [snapshot, new_indirect_non_breaking_snapshot, new_forward_only_snapshot]
    )
    assert not actual_snapshots[snapshot.snapshot_id].unpaused_ts
    assert not actual_snapshots[new_indirect_non_breaking_snapshot.snapshot_id].unpaused_ts
    assert actual_snapshots[new_forward_only_snapshot.snapshot_id].unpaused_ts == to_timestamp(
        unpaused_dt
    )

    assert actual_snapshots[snapshot.snapshot_id].unrestorable
    assert actual_snapshots[new_indirect_non_breaking_snapshot.snapshot_id].unrestorable
    assert not actual_snapshots[new_forward_only_snapshot.snapshot_id].unrestorable


def test_unrestorable_snapshot_target_not_forward_only(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot = make_snapshot(
        SqlModel(
            name="test_snapshot",
            query=parse_one("select 1, ds"),
            cron="@daily",
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.version = "a"

    assert not snapshot.unpaused_ts
    state_sync.push_snapshots([snapshot])

    unpaused_dt = "2022-01-01"
    state_sync.unpause_snapshots([snapshot], unpaused_dt)

    actual_snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
    assert actual_snapshot.unpaused_ts
    assert actual_snapshot.unpaused_ts == to_timestamp(unpaused_dt)

    updated_snapshot = make_snapshot(
        SqlModel(name="test_snapshot", query=parse_one("select 2, ds"), cron="@daily")
    )
    updated_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=False)
    updated_snapshot.version = "a"

    assert not updated_snapshot.unpaused_ts
    state_sync.push_snapshots([updated_snapshot])
    state_sync.unpause_snapshots([updated_snapshot], unpaused_dt)

    actual_snapshots = state_sync.get_snapshots([snapshot, updated_snapshot])
    assert not actual_snapshots[snapshot.snapshot_id].unpaused_ts
    assert actual_snapshots[updated_snapshot.snapshot_id].unpaused_ts == to_timestamp(unpaused_dt)

    assert actual_snapshots[snapshot.snapshot_id].unrestorable
    assert not actual_snapshots[updated_snapshot.snapshot_id].unrestorable


def test_version_schema(state_sync: EngineAdapterStateSync, tmp_path) -> None:
    from sqlmesh import __version__ as SQLMESH_VERSION

    # fresh install should not raise
    assert state_sync.get_versions() == Versions(
        schema_version=SCHEMA_VERSION,
        sqlglot_version=SQLGLOT_VERSION,
        sqlmesh_version=SQLMESH_VERSION,
    )

    # Start with a clean slate.
    state_sync = EngineAdapterStateSync(
        create_engine_adapter(duckdb.connect, "duckdb"),
        schema=c.SQLMESH,
        cache_dir=tmp_path / c.CACHE,
    )

    with pytest.raises(
        SQLMeshError,
        match=rf"SQLMesh \(local\) is using version '{re.escape(SQLMESH_VERSION)}' which is ahead of '0.0.0' \(remote\). Please run a migration \('sqlmesh migrate' command\).",
    ):
        state_sync.get_versions()

    state_sync.migrate()

    # migration version is behind, always raise
    state_sync.version_state.update_versions(schema_version=SCHEMA_VERSION + 1)
    error = (
        rf"SQLMesh \(local\) is using version '{SCHEMA_VERSION}' which is behind '{SCHEMA_VERSION + 1}' \(remote\). "
        rf"""Please upgrade SQLMesh \('pip install --upgrade "sqlmesh=={re.escape(SQLMESH_VERSION)}"' command\)."""
    )

    with pytest.raises(SQLMeshError, match=error):
        state_sync.get_versions()

    # should no longer raise
    state_sync.get_versions(validate=False)

    # migration version is ahead, only raise when validate is true
    state_sync.version_state.update_versions(schema_version=SCHEMA_VERSION - 1)
    with pytest.raises(
        SQLMeshError,
        match=rf"SQLMesh \(local\) is using version '{SCHEMA_VERSION}' which is ahead of '{SCHEMA_VERSION - 1}'",
    ):
        state_sync.get_versions()
    state_sync.get_versions(validate=False)


def test_version_sqlmesh(state_sync: EngineAdapterStateSync) -> None:
    from sqlmesh import __version__ as SQLMESH_VERSION
    from sqlmesh import __version_tuple__ as SQLMESH_VERSION_TUPLE

    # patch version sqlmesh doesn't matter
    major, minor, patch, *_ = SQLMESH_VERSION_TUPLE
    new_patch = (
        f"dev{int(patch[3:]) + 1}"  # type: ignore
        if isinstance(patch, str) and patch.startswith("dev")
        else f"{int(patch) + 1}"
    )
    sqlmesh_version_patch_bump = f"{major}.{minor}.{new_patch}"
    state_sync.version_state.update_versions(sqlmesh_version=sqlmesh_version_patch_bump)
    state_sync.get_versions(validate=False)

    # sqlmesh version is behind
    sqlmesh_version_minor_bump = f"{major}.{int(minor) + 1}.{patch}"
    error = (
        rf"SQLMesh \(local\) is using version '{re.escape(SQLMESH_VERSION)}' which is behind '{sqlmesh_version_minor_bump}' \(remote\). "
        rf"""Please upgrade SQLMesh \('pip install --upgrade "sqlmesh=={sqlmesh_version_minor_bump}"' command\)."""
    )
    state_sync.version_state.update_versions(sqlmesh_version=sqlmesh_version_minor_bump)
    with pytest.raises(SQLMeshError, match=error):
        state_sync.get_versions()
    state_sync.get_versions(validate=False)

    # sqlmesh version is ahead
    sqlmesh_version_minor_decrease = f"{major}.{int(minor) - 1}.{patch}"
    error = rf"SQLMesh \(local\) is using version '{re.escape(SQLMESH_VERSION)}' which is ahead of '{sqlmesh_version_minor_decrease}'"
    state_sync.version_state.update_versions(sqlmesh_version=sqlmesh_version_minor_decrease)
    with pytest.raises(SQLMeshError, match=error):
        state_sync.get_versions()
    state_sync.get_versions(validate=False)


def test_version_sqlglot(state_sync: EngineAdapterStateSync) -> None:
    # patch version sqlglot doesn't matter
    major, minor, patch, *_ = SQLGLOT_VERSION.split(".")
    sqlglot_version = f"{major}.{minor}.{int(patch) + 1}"
    state_sync.version_state.update_versions(sqlglot_version=sqlglot_version)
    state_sync.get_versions(validate=False)

    # sqlglot version is behind
    sqlglot_version = f"{major}.{int(minor) + 1}.{patch}"
    error = (
        rf"SQLGlot \(local\) is using version '{SQLGLOT_VERSION}' which is behind '{sqlglot_version}' \(remote\). "
        rf"""Please upgrade SQLGlot \('pip install --upgrade "sqlglot=={sqlglot_version}"' command\)."""
    )
    state_sync.version_state.update_versions(sqlglot_version=sqlglot_version)
    with pytest.raises(SQLMeshError, match=error):
        state_sync.get_versions()
    state_sync.get_versions(validate=False)

    # sqlglot version is ahead
    sqlglot_version = f"{major}.{int(minor) - 1}.{patch}"
    error = rf"SQLGlot \(local\) is using version '{SQLGLOT_VERSION}' which is ahead of '{sqlglot_version}'"
    state_sync.version_state.update_versions(sqlglot_version=sqlglot_version)
    with pytest.raises(SQLMeshError, match=error):
        state_sync.get_versions()
    state_sync.get_versions(validate=False)


def test_empty_versions() -> None:
    for empty_versions in (
        Versions(),
        Versions(schema_version=None, sqlglot_version=None, sqlmesh_version=None),
    ):
        assert empty_versions.schema_version == 0
        assert empty_versions.sqlglot_version == "0.0.0"
        assert empty_versions.sqlmesh_version == "0.0.0"


def test_migrate(state_sync: EngineAdapterStateSync, mocker: MockerFixture, tmp_path) -> None:
    from sqlmesh import __version__ as SQLMESH_VERSION

    migrate_rows_mock = mocker.patch(
        "sqlmesh.core.state_sync.db.migrator.StateMigrator._migrate_rows"
    )
    backup_state_mock = mocker.patch(
        "sqlmesh.core.state_sync.db.migrator.StateMigrator._backup_state"
    )
    state_sync.migrate()
    migrate_rows_mock.assert_not_called()
    backup_state_mock.assert_not_called()

    # Start with a clean slate.
    state_sync = EngineAdapterStateSync(
        create_engine_adapter(duckdb.connect, "duckdb"),
        schema=c.SQLMESH,
        cache_dir=tmp_path / c.CACHE,
    )

    state_sync.migrate()
    migrate_rows_mock.assert_called_once()
    backup_state_mock.assert_called_once()
    assert state_sync.get_versions() == Versions(
        schema_version=SCHEMA_VERSION,
        sqlglot_version=SQLGLOT_VERSION,
        sqlmesh_version=SQLMESH_VERSION,
    )

    assert (
        state_sync.engine_adapter.fetchone(
            "SELECT COUNT(*) FROM sqlmesh._snapshots WHERE ttl_ms IS NULL"
        )[0]  # type: ignore
        == 0
    )


def test_rollback(state_sync: EngineAdapterStateSync, mocker: MockerFixture) -> None:
    with pytest.raises(
        SQLMeshError,
        match="There are no prior migrations to roll back to.",
    ):
        state_sync.rollback()

    restore_table_spy = mocker.spy(state_sync.migrator, "_restore_table")
    state_sync.migrator._backup_state()

    state_sync.rollback()
    calls = {(a.sql(), b.sql()) for (a, b), _ in restore_table_spy.call_args_list}
    assert (
        f"{state_sync.schema}._snapshots",
        f"{state_sync.schema}._snapshots_backup",
    ) in calls
    assert (
        f"{state_sync.schema}._environments",
        f"{state_sync.schema}._environments_backup",
    ) in calls
    assert (
        f"{state_sync.schema}._versions",
        f"{state_sync.schema}._versions_backup",
    ) in calls
    assert not state_sync.engine_adapter.table_exists(f"{state_sync.schema}._snapshots_backup")
    assert not state_sync.engine_adapter.table_exists(f"{state_sync.schema}._environments_backup")
    assert not state_sync.engine_adapter.table_exists(f"{state_sync.schema}._versions_backup")


def test_first_migration_failure(duck_conn, mocker: MockerFixture, tmp_path) -> None:
    state_sync = EngineAdapterStateSync(
        create_engine_adapter(lambda: duck_conn, "duckdb"),
        schema=c.SQLMESH,
        cache_dir=tmp_path / c.CACHE,
    )
    mocker.patch.object(state_sync.migrator, "_migrate_rows", side_effect=Exception("mocked error"))
    with pytest.raises(
        SQLMeshError,
        match="SQLMesh migration failed.",
    ):
        state_sync.migrate()
    assert not state_sync.engine_adapter.table_exists(state_sync.snapshot_state.snapshots_table)
    assert not state_sync.engine_adapter.table_exists(
        state_sync.environment_state.environments_table
    )
    assert not state_sync.engine_adapter.table_exists(state_sync.version_state.versions_table)
    assert not state_sync.engine_adapter.table_exists(state_sync.interval_state.intervals_table)


def test_migrate_rows(state_sync: EngineAdapterStateSync, mocker: MockerFixture) -> None:
    state_sync.engine_adapter.replace_query(
        "sqlmesh._versions",
        pd.read_json("tests/fixtures/migrations/versions.json"),
        target_columns_to_types={
            "schema_version": exp.DataType.build("int"),
            "sqlglot_version": exp.DataType.build("text"),
            "sqlmesh_version": exp.DataType.build("text"),
        },
    )

    state_sync.engine_adapter.replace_query(
        "sqlmesh._snapshots",
        pd.read_json("tests/fixtures/migrations/snapshots.json"),
        target_columns_to_types={
            "name": exp.DataType.build("text"),
            "identifier": exp.DataType.build("text"),
            "version": exp.DataType.build("text"),
            "snapshot": exp.DataType.build("text"),
            "kind_name": exp.DataType.build("text"),
            "updated_ts": exp.DataType.build("bigint"),
            "unpaused_ts": exp.DataType.build("bigint"),
            "ttl_ms": exp.DataType.build("bigint"),
            "unrestorable": exp.DataType.build("boolean"),
        },
    )

    state_sync.engine_adapter.replace_query(
        "sqlmesh._environments",
        pd.read_json("tests/fixtures/migrations/environments.json"),
        target_columns_to_types={
            "name": exp.DataType.build("text"),
            "snapshots": exp.DataType.build("text"),
            "start_at": exp.DataType.build("text"),
            "end_at": exp.DataType.build("text"),
            "plan_id": exp.DataType.build("text"),
            "previous_plan_id": exp.DataType.build("text"),
            "expiration_ts": exp.DataType.build("bigint"),
            "finalized_ts": exp.DataType.build("bigint"),
            "promoted_snapshot_ids": exp.DataType.build("text"),
            "suffix_target": exp.DataType.build("text"),
            "catalog_name_override": exp.DataType.build("text"),
            "previous_finalized_snapshots": exp.DataType.build("text"),
            "normalize_name": exp.DataType.build("boolean"),
            "requirements": exp.DataType.build("text"),
        },
    )

    state_sync.engine_adapter.replace_query(
        "sqlmesh._intervals",
        pd.read_json("tests/fixtures/migrations/intervals.json"),
        target_columns_to_types={
            "id": exp.DataType.build("text"),
            "created_ts": exp.DataType.build("bigint"),
            "name": exp.DataType.build("text"),
            "identifier": exp.DataType.build("text"),
            "version": exp.DataType.build("text"),
            "start_ts": exp.DataType.build("bigint"),
            "end_ts": exp.DataType.build("bigint"),
            "is_dev": exp.DataType.build("boolean"),
            "is_removed": exp.DataType.build("boolean"),
            "is_compacted": exp.DataType.build("boolean"),
        },
    )

    old_snapshots = state_sync.engine_adapter.fetchdf("select * from sqlmesh._snapshots")
    old_environments = state_sync.engine_adapter.fetchdf("select * from sqlmesh._environments")

    state_sync.migrate(skip_backup=True)

    new_snapshots = state_sync.engine_adapter.fetchdf("select * from sqlmesh._snapshots")
    new_environments = state_sync.engine_adapter.fetchdf("select * from sqlmesh._environments")

    assert len(old_snapshots) == 24
    assert len(new_snapshots) == 36
    assert len(old_environments) == len(new_environments)

    start = "2023-01-01"
    end = "2023-01-07"

    assert not missing_intervals(
        state_sync.get_snapshots(
            t.cast(Environment, state_sync.get_environment("staging")).snapshots
        ).values(),
        start=start,
        end=end,
    )

    dev_snapshots = state_sync.get_snapshots(
        t.cast(Environment, state_sync.get_environment("dev")).snapshots
    ).values()

    assert all(s.migrated for s in dev_snapshots)
    assert all(s.change_category is not None for s in dev_snapshots)

    assert not missing_intervals(dev_snapshots, start=start, end=end)

    assert not missing_intervals(dev_snapshots, start="2023-01-08", end="2023-01-10") == 8

    all_snapshot_ids = [
        SnapshotId(name=name, identifier=identifier)
        for name, identifier in state_sync.engine_adapter.fetchall(
            "SELECT name, identifier FROM sqlmesh._snapshots"
        )
    ]
    for s in state_sync.get_snapshots(all_snapshot_ids).values():
        if not s.is_symbolic:
            assert s.intervals

    customer_revenue_by_day = new_snapshots.loc[
        new_snapshots["name"] == '"sushi"."customer_revenue_by_day"'
    ].iloc[0]
    assert json.loads(customer_revenue_by_day["snapshot"])["node"]["query"].startswith(
        "JINJA_QUERY_BEGIN"
    )


def test_backup_state(state_sync: EngineAdapterStateSync, mocker: MockerFixture) -> None:
    state_sync.engine_adapter.replace_query(
        "sqlmesh._snapshots",
        pd.read_json("tests/fixtures/migrations/snapshots.json"),
        target_columns_to_types={
            "name": exp.DataType.build("text"),
            "identifier": exp.DataType.build("text"),
            "version": exp.DataType.build("text"),
            "snapshot": exp.DataType.build("text"),
        },
    )

    state_sync.migrator._backup_state()
    pd.testing.assert_frame_equal(
        state_sync.engine_adapter.fetchdf("select * from sqlmesh._snapshots"),
        state_sync.engine_adapter.fetchdf("select * from sqlmesh._snapshots_backup"),
    )


def test_restore_snapshots_table(state_sync: EngineAdapterStateSync) -> None:
    snapshot_columns_to_types = {
        "name": exp.DataType.build("text"),
        "identifier": exp.DataType.build("text"),
        "version": exp.DataType.build("text"),
        "snapshot": exp.DataType.build("text"),
    }
    state_sync.engine_adapter.replace_query(
        "sqlmesh._snapshots",
        pd.read_json("tests/fixtures/migrations/snapshots.json"),
        target_columns_to_types=snapshot_columns_to_types,
    )

    old_snapshots = state_sync.engine_adapter.fetchdf("select * from sqlmesh._snapshots")
    old_snapshots_count = state_sync.engine_adapter.fetchone(
        "select count(*) from sqlmesh._snapshots"
    )
    assert old_snapshots_count == (24,)
    state_sync.migrator._backup_state()

    state_sync.engine_adapter.delete_from("sqlmesh._snapshots", "TRUE")
    snapshots_count = state_sync.engine_adapter.fetchone("select count(*) from sqlmesh._snapshots")
    assert snapshots_count == (0,)
    state_sync.migrator._restore_table(
        table_name="sqlmesh._snapshots",
        backup_table_name="sqlmesh._snapshots_backup",
    )

    new_snapshots = state_sync.engine_adapter.fetchdf("select * from sqlmesh._snapshots")
    pd.testing.assert_frame_equal(
        old_snapshots,
        new_snapshots,
    )


def test_seed_hydration(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
):
    snapshot = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="header\n1\n2"),
            column_hashes={"header": "hash"},
            depends_on=set(),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    assert snapshot.model.is_hydrated
    assert snapshot.model.seed.content == "header\n1\n2"

    state_sync.snapshot_state.clear_cache()
    stored_snapshot = state_sync.get_snapshots([snapshot.snapshot_id])[snapshot.snapshot_id]
    assert isinstance(stored_snapshot.model, SeedModel)
    assert not stored_snapshot.model.is_hydrated
    assert stored_snapshot.model.seed.content == ""


def test_nodes_exist(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
        )
    )

    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    assert not state_sync.nodes_exist([snapshot.name])

    state_sync.push_snapshots([snapshot])

    assert state_sync.nodes_exist([snapshot.name]) == {snapshot.name}


def test_invalidate_environment(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    original_expiration_ts = now_timestamp() + 100000

    env = Environment(
        name="test_environment",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=original_expiration_ts,
    )
    environment_statements = [
        EnvironmentStatements(
            before_all=["CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"],
            after_all=["CREATE OR REPLACE TABLE table_2 AS SELECT 'b'"],
            python_env={},
        )
    ]

    state_sync.promote(env, environment_statements=environment_statements)

    assert state_sync.get_environment_statements(env.name) == environment_statements

    assert not state_sync.delete_expired_environments()
    state_sync.invalidate_environment("test_environment")

    stored_env = state_sync.get_environment("test_environment")
    assert stored_env
    assert stored_env.expiration_ts and stored_env.expiration_ts < original_expiration_ts

    deleted_environments = state_sync.delete_expired_environments()
    assert len(deleted_environments) == 1
    assert deleted_environments[0].name == "test_environment"
    assert state_sync.get_environment_statements(env.name) == []

    with pytest.raises(SQLMeshError, match="Cannot invalidate the production environment."):
        state_sync.invalidate_environment("prod")


def test_promote_environment_without_statements(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    original_expiration_ts = now_timestamp() + 100000

    env = Environment(
        name="test_environment",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=original_expiration_ts,
    )
    environment_statements = [
        EnvironmentStatements(
            before_all=["CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"],
            after_all=["CREATE OR REPLACE TABLE table_2 AS SELECT 'b'"],
            python_env={},
        )
    ]

    state_sync.promote(env, environment_statements=environment_statements)

    # Verify the environment statements table is populated with the statements
    assert state_sync.get_environment_statements(env.name) == environment_statements

    # Scenario where the statements have been removed from the project and then
    # If we promote the environment it doesn't contain before_all, after_all statements
    state_sync.promote(env, environment_statements=[])

    # This should trigger an internal update to the environment statements' table to be removed
    assert state_sync.get_environment_statements(env.name) == []


def test_cache(state_sync, make_snapshot, mocker):
    cache = CachingStateSync(state_sync, ttl=10)

    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 'a', 'ds'"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    now_timestamp = mocker.patch("sqlmesh.core.state_sync.cache.now_timestamp")
    now_timestamp.return_value = to_timestamp("2023-01-01 00:00:00")

    # prime the cache with a cached missing snapshot
    assert not cache.get_snapshots([snapshot.snapshot_id])

    # item is cached and shouldn't hit state sync
    with patch.object(state_sync, "get_snapshots") as mock:
        assert not cache.get_snapshots([snapshot.snapshot_id])
        mock.assert_not_called()

    # prime the cache with a real snapshot
    cache.push_snapshots([snapshot])
    assert cache.get_snapshots([snapshot.snapshot_id]) == {snapshot.snapshot_id: snapshot}

    # cache hit
    with patch.object(state_sync, "get_snapshots") as mock:
        assert cache.get_snapshots([snapshot.snapshot_id]) == {snapshot.snapshot_id: snapshot}
        mock.assert_not_called()

    # clear the cache by adding intervals
    cache.add_interval(snapshot, "2020-01-01", "2020-01-01")
    with patch.object(state_sync, "get_snapshots") as mock:
        assert not cache.get_snapshots([snapshot.snapshot_id])
        mock.assert_called()

    # clear the cache by removing intervals
    cache.remove_intervals([(snapshot, snapshot.inclusive_exclusive("2020-01-01", "2020-01-01"))])

    # prime the cache
    assert cache.get_snapshots([snapshot.snapshot_id]) == {snapshot.snapshot_id: snapshot}

    # cache hit half way
    now_timestamp.return_value = to_timestamp("2023-01-01 00:00:05")
    with patch.object(state_sync, "get_snapshots") as mock:
        assert cache.get_snapshots([snapshot.snapshot_id])
        mock.assert_not_called()

    # no cache hit
    now_timestamp.return_value = to_timestamp("2023-01-01 00:00:11")
    with patch.object(state_sync, "get_snapshots") as mock:
        assert not cache.get_snapshots([snapshot.snapshot_id])
        mock.assert_called()


def test_max_interval_end_per_model(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            cron="@daily",
            query=parse_one("select 2, ds"),
        ),
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_a, snapshot_b])

    state_sync.add_interval(snapshot_a, "2023-01-01", "2023-01-01")
    state_sync.add_interval(snapshot_a, "2023-01-02", "2023-01-02")
    state_sync.add_interval(snapshot_a, "2023-01-03", "2023-01-03")
    state_sync.add_interval(snapshot_b, "2023-01-01", "2023-01-01")
    state_sync.add_interval(snapshot_b, "2023-01-02", "2023-01-02")

    environment_name = "test_max_interval_end_for_environment"

    assert state_sync.max_interval_end_per_model(environment_name) == {}
    assert state_sync.max_interval_end_per_model(environment_name, {snapshot_a.name}) == {}

    state_sync.promote(
        Environment(
            name=environment_name,
            snapshots=[snapshot_a.table_info, snapshot_b.table_info],
            start_at="2023-01-01",
            end_at="2023-01-03",
            plan_id="test_plan_id",
            previous_finalized_snapshots=[snapshot_b.table_info],
        )
    )

    assert state_sync.max_interval_end_per_model(environment_name, {snapshot_a.name}) == {
        snapshot_a.name: to_timestamp("2023-01-04")
    }

    assert state_sync.max_interval_end_per_model(environment_name, {snapshot_b.name}) == {
        snapshot_b.name: to_timestamp("2023-01-03")
    }

    assert state_sync.max_interval_end_per_model(
        environment_name, {snapshot_a.name, snapshot_b.name}
    ) == {
        snapshot_a.name: to_timestamp("2023-01-04"),
        snapshot_b.name: to_timestamp("2023-01-03"),
    }

    assert state_sync.max_interval_end_per_model(environment_name) == {
        snapshot_a.name: to_timestamp("2023-01-04"),
        snapshot_b.name: to_timestamp("2023-01-03"),
    }

    assert state_sync.max_interval_end_per_model(environment_name, {"missing"}) == {}
    assert state_sync.max_interval_end_per_model(environment_name, set()) == {}


def test_max_interval_end_per_model_with_pending_restatements(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    state_sync.add_interval(snapshot, "2023-01-01", "2023-01-01")
    state_sync.add_interval(snapshot, "2023-01-02", "2023-01-02")
    state_sync.add_interval(snapshot, "2023-01-03", "2023-01-03")
    # Add a pending restatement interval
    state_sync.add_snapshots_intervals(
        [
            SnapshotIntervals(
                name=snapshot.name,
                identifier=snapshot.identifier,
                version=snapshot.version,
                dev_version=snapshot.dev_version,
                intervals=[],
                dev_intervals=[],
                pending_restatement_intervals=[
                    (to_timestamp("2023-01-04"), to_timestamp("2023-01-05"))
                ],
            )
        ]
    )

    snapshot = state_sync.get_snapshots([snapshot.snapshot_id])[snapshot.snapshot_id]
    assert snapshot.intervals == [(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))]
    assert snapshot.pending_restatement_intervals == [
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05"))
    ]

    environment_name = "test_max_interval_end_for_environment"

    state_sync.promote(
        Environment(
            name=environment_name,
            snapshots=[snapshot.table_info],
            start_at="2023-01-01",
            end_at="2023-01-03",
            plan_id="test_plan_id",
            previous_finalized_snapshots=[],
        )
    )

    assert state_sync.max_interval_end_per_model(environment_name) == {
        snapshot.name: to_timestamp("2023-01-04")
    }


def test_max_interval_end_per_model_ensure_finalized_snapshots(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable
) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            cron="@daily",
            query=parse_one("select 2, ds"),
        ),
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot_a, snapshot_b])

    state_sync.add_interval(snapshot_a, "2023-01-01", "2023-01-01")
    state_sync.add_interval(snapshot_a, "2023-01-02", "2023-01-02")
    state_sync.add_interval(snapshot_a, "2023-01-03", "2023-01-03")
    state_sync.add_interval(snapshot_b, "2023-01-01", "2023-01-01")
    state_sync.add_interval(snapshot_b, "2023-01-02", "2023-01-02")

    environment_name = "test_max_interval_end_for_environment"

    assert state_sync.max_interval_end_per_model(environment_name) == {}
    assert state_sync.max_interval_end_per_model(environment_name, {snapshot_a.name}) == {}

    state_sync.promote(
        Environment(
            name=environment_name,
            snapshots=[snapshot_a.table_info, snapshot_b.table_info],
            start_at="2023-01-01",
            end_at="2023-01-03",
            plan_id="test_plan_id",
            previous_finalized_snapshots=[snapshot_b.table_info],
        )
    )

    assert (
        state_sync.max_interval_end_per_model(
            environment_name, {snapshot_a.name}, ensure_finalized_snapshots=True
        )
        == {}
    )

    assert state_sync.max_interval_end_per_model(
        environment_name, {snapshot_b.name}, ensure_finalized_snapshots=True
    ) == {snapshot_b.name: to_timestamp("2023-01-03")}

    assert state_sync.max_interval_end_per_model(
        environment_name, {snapshot_a.name, snapshot_b.name}, ensure_finalized_snapshots=True
    ) == {snapshot_b.name: to_timestamp("2023-01-03")}

    assert state_sync.max_interval_end_per_model(
        environment_name, ensure_finalized_snapshots=True
    ) == {snapshot_b.name: to_timestamp("2023-01-03")}

    assert (
        state_sync.max_interval_end_per_model(
            environment_name, {"missing"}, ensure_finalized_snapshots=True
        )
        == {}
    )
    assert state_sync.max_interval_end_per_model(environment_name, set()) == {}


def test_get_snapshots(mocker):
    mock = mocker.MagicMock()
    cache = CachingStateSync(mock)
    cache.get_snapshots([])
    mock.get_snapshots.assert_not_called()


def test_snapshot_batching(state_sync, mocker, make_snapshot):
    mock = mocker.Mock()

    state_sync.snapshot_state.SNAPSHOT_BATCH_SIZE = 2
    state_sync.snapshot_state.engine_adapter = mock

    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1")), "1")
    snapshot_b = make_snapshot(SqlModel(name="a", query=parse_one("select 2")), "2")
    snapshot_c = make_snapshot(SqlModel(name="a", query=parse_one("select 3")), "3")

    state_sync.delete_snapshots(
        (
            snapshot_a,
            snapshot_b,
            snapshot_c,
        )
    )
    calls = mock.delete_from.call_args_list
    identifiers = sorted([snapshot_a.identifier, snapshot_b.identifier, snapshot_c.identifier])
    assert mock.delete_from.call_args_list == [
        call(
            exp.to_table("sqlmesh._snapshots"),
            where=parse_one(
                f"(name, identifier) in (('\"a\"', '{identifiers[0]}'), ('\"a\"', '{identifiers[1]}'))"
            ),
        ),
        call(
            exp.to_table("sqlmesh._snapshots"),
            where=parse_one(f"(name, identifier) in (('\"a\"', '{identifiers[2]}'))"),
        ),
    ]

    mock.fetchall.side_effect = [
        [
            [
                make_snapshot(
                    SqlModel(name="a", query=parse_one("select 1")),
                ).json(),
                "a",
                "1",
                "1",
                1,
                1,
                False,
                False,
                None,
            ],
            [
                make_snapshot(
                    SqlModel(name="a", query=parse_one("select 2")),
                ).json(),
                "a",
                "2",
                "2",
                1,
                1,
                False,
                False,
                None,
            ],
        ],
        [
            [
                make_snapshot(
                    SqlModel(name="a", query=parse_one("select 3")),
                ).json(),
                "a",
                "3",
                "3",
                1,
                1,
                False,
                False,
                None,
            ],
        ],
    ]

    snapshots = state_sync.snapshot_state.get_snapshots(
        (
            SnapshotId(name="a", identifier="1"),
            SnapshotId(name="a", identifier="2"),
            SnapshotId(name="a", identifier="3"),
        ),
    )
    assert len(snapshots) == 3
    calls = mock.fetchall.call_args_list
    assert len(calls) == 2


def test_seed_model_metadata_update(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
):
    model = SeedModel(
        name="a",
        kind=SeedKind(path="./path/to/seed"),
        seed=Seed(content="header\n1\n2"),
        column_hashes={"header": "hash"},
        depends_on=set(),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    model = model.copy(update={"owner": "jen"})
    new_snapshot = make_snapshot(model)
    new_snapshot.previous_versions = snapshot.all_versions
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    assert snapshot.fingerprint != new_snapshot.fingerprint
    assert snapshot.version == new_snapshot.version

    state_sync.push_snapshots([new_snapshot])
    assert len(state_sync.get_snapshots([new_snapshot, snapshot])) == 2


def test_snapshot_cache(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable, mocker: MockerFixture
):
    cache_mock = mocker.Mock()
    state_sync.snapshot_state._snapshot_cache = cache_mock

    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1")))
    cache_mock.get_or_load.return_value = ({snapshot.snapshot_id: snapshot}, {snapshot.snapshot_id})

    state_sync.snapshot_state.push_snapshots([snapshot])

    assert state_sync.get_snapshots([snapshot.snapshot_id]) == {snapshot.snapshot_id: snapshot}
    cache_mock.get_or_load.assert_called_once_with({snapshot.snapshot_id}, mocker.ANY)

    # Update the snapshot in the state and make sure this update is reflected on the cached instance.
    assert snapshot.unpaused_ts is None
    assert not snapshot.unrestorable
    state_sync.snapshot_state._update_snapshots(
        [snapshot.snapshot_id], unpaused_ts=1, unrestorable=True
    )
    new_snapshot = state_sync.get_snapshots([snapshot.snapshot_id])[snapshot.snapshot_id]
    assert new_snapshot.unpaused_ts == 1
    assert new_snapshot.unrestorable

    # If the record was deleted from the state, the cached version should not be returned.
    state_sync.delete_snapshots([snapshot.snapshot_id])
    assert state_sync.get_snapshots([snapshot.snapshot_id]) == {}


def test_update_auto_restatements(state_sync: EngineAdapterStateSync, make_snapshot: t.Callable):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1")), version="1")
    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2")), version="2")
    snapshot_c = make_snapshot(SqlModel(name="c", query=parse_one("select 3")), version="3")

    state_sync.snapshot_state.push_snapshots([snapshot_a, snapshot_b, snapshot_c])

    next_auto_restatement_ts: t.Dict[SnapshotNameVersion, t.Optional[int]] = {
        snapshot_a.name_version: 1,
        snapshot_b.name_version: 2,
        snapshot_c.name_version: 3,
    }
    state_sync.update_auto_restatements(next_auto_restatement_ts)

    snapshots = state_sync.get_snapshots(
        [snapshot_a.snapshot_id, snapshot_b.snapshot_id, snapshot_c.snapshot_id]
    )
    assert snapshots[snapshot_a.snapshot_id].next_auto_restatement_ts == 1
    assert snapshots[snapshot_b.snapshot_id].next_auto_restatement_ts == 2
    assert snapshots[snapshot_c.snapshot_id].next_auto_restatement_ts == 3

    next_auto_restatement_ts = {
        snapshot_a.name_version: 4,
        snapshot_b.name_version: 5,
        snapshot_c.name_version: None,
    }
    state_sync.update_auto_restatements(next_auto_restatement_ts)

    snapshots = state_sync.get_snapshots(
        [snapshot_a.snapshot_id, snapshot_b.snapshot_id, snapshot_c.snapshot_id]
    )
    assert snapshots[snapshot_a.snapshot_id].next_auto_restatement_ts == 4
    assert snapshots[snapshot_b.snapshot_id].next_auto_restatement_ts == 5
    assert snapshots[snapshot_c.snapshot_id].next_auto_restatement_ts is None


@time_machine.travel("2020-01-05 00:00:00 UTC")
def test_compact_intervals_pending_restatement(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot])

    state_sync.add_interval(snapshot, "2020-01-01", "2020-01-01")
    state_sync.add_interval(snapshot, "2020-01-02", "2020-01-02")
    state_sync.add_interval(snapshot, "2020-01-03", "2020-01-03")
    state_sync.add_interval(snapshot, "2020-01-04", "2020-01-04")

    pending_restatement_intervals = [
        (to_timestamp("2020-01-03"), to_timestamp("2020-01-05")),
    ]
    state_sync.add_snapshots_intervals(
        [
            SnapshotIntervals(
                name=snapshot.name,
                identifier=snapshot.identifier,
                version=snapshot.version,
                dev_version=snapshot.dev_version,
                intervals=[],
                dev_intervals=[],
                pending_restatement_intervals=pending_restatement_intervals,
            )
        ]
    )

    with time_machine.travel("2020-01-05 01:00:00 UTC"):
        # Backfill one of the pending restatement intervals.
        state_sync.add_interval(snapshot, "2020-01-03", "2020-01-03")
        snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
        assert snapshot.intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        ]
        assert snapshot.pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-05")),
        ]

        state_sync.compact_intervals()
        snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
        assert snapshot.intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        ]
        assert snapshot.pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-05")),
        ]

        # Make sure compaction is idempotent.
        state_sync.compact_intervals()
        snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
        assert snapshot.intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        ]
        assert snapshot.pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-05")),
        ]

    with time_machine.travel("2020-01-05 02:00:00 UTC"):
        # Backfill the remaining pending restatement interval.
        state_sync.add_interval(snapshot, "2020-01-04", "2020-01-04")
        snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
        assert snapshot.intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        ]
        assert snapshot.pending_restatement_intervals == []

        state_sync.compact_intervals()
        snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
        assert snapshot.intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        ]
        assert snapshot.pending_restatement_intervals == []

        # Make sure compaction is idempotent.
        state_sync.compact_intervals()
        snapshot = state_sync.get_snapshots([snapshot])[snapshot.snapshot_id]
        assert snapshot.intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
        ]
        assert snapshot.pending_restatement_intervals == []


@time_machine.travel("2020-01-05 00:00:00 UTC")
def test_compact_intervals_pending_restatement_shared_version(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
) -> None:
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 1, ds"),
        ),
        version="a",
    )
    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            cron="@daily",
            query=parse_one("select 2, ds"),
        ),
        version="a",
    )

    state_sync.push_snapshots([snapshot_a, snapshot_b])

    state_sync.add_interval(snapshot_a, "2020-01-01", "2020-01-01")
    state_sync.add_interval(snapshot_a, "2020-01-02", "2020-01-02")
    state_sync.add_interval(snapshot_a, "2020-01-03", "2020-01-03")
    state_sync.add_interval(snapshot_a, "2020-01-04", "2020-01-04")
    state_sync.add_interval(snapshot_a, "2020-01-05", "2020-01-05")
    state_sync.add_snapshots_intervals(
        [
            SnapshotIntervals(
                name=snapshot_a.name,
                identifier=snapshot_a.identifier,
                version=snapshot_a.version,
                dev_version=snapshot_a.dev_version,
                intervals=[],
                dev_intervals=[],
                pending_restatement_intervals=[
                    (to_timestamp("2020-01-03"), to_timestamp("2020-01-06")),
                ],
            )
        ]
    )

    expected_intervals = [
        SnapshotIntervals(
            name=snapshot_b.name,
            identifier=None,
            version=snapshot_b.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
            ],
        ),
        SnapshotIntervals(
            name=snapshot_a.name,
            identifier=snapshot_a.identifier,
            version=snapshot_a.version,
            dev_version=snapshot_a.dev_version,
            intervals=[
                (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
            ],
            dev_intervals=[],
            pending_restatement_intervals=[],
        ),
        SnapshotIntervals(
            name=snapshot_b.name,
            identifier=snapshot_b.identifier,
            version=snapshot_b.version,
            dev_version=snapshot_b.dev_version,
            intervals=[
                (to_timestamp("2020-01-03"), to_timestamp("2020-01-04")),
            ],
            dev_intervals=[],
            pending_restatement_intervals=[],
        ),
    ]
    expected_intervals = sorted(expected_intervals, key=lambda x: (x.name, x.identifier or ""))

    with time_machine.travel("2020-01-05 01:00:00 UTC"):
        # Add a new interval for the new snapshot
        state_sync.add_interval(snapshot_b, "2020-01-03", "2020-01-03")
        assert (
            sorted(
                state_sync.interval_state.get_snapshot_intervals([snapshot_a, snapshot_b]),
                key=lambda x: (x.name, x.identifier or ""),
            )
            == expected_intervals
        )
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
        ]

        state_sync.compact_intervals()
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]

        state_sync.compact_intervals()
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]

    expected_intervals = [
        SnapshotIntervals(
            name=snapshot_a.name,
            identifier=None,
            version=snapshot_a.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
            ],
        ),
        SnapshotIntervals(
            name=snapshot_a.name,
            identifier=snapshot_a.identifier,
            version=snapshot_a.version,
            dev_version=snapshot_a.dev_version,
            intervals=[
                (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
            ],
            dev_intervals=[],
            pending_restatement_intervals=[],
        ),
        SnapshotIntervals(
            name=snapshot_b.name,
            identifier=snapshot_b.identifier,
            version=snapshot_b.version,
            dev_version=snapshot_b.dev_version,
            intervals=[
                (to_timestamp("2020-01-03"), to_timestamp("2020-01-04")),
            ],
            dev_intervals=[],
            pending_restatement_intervals=[],
        ),
    ]
    expected_intervals = sorted(expected_intervals, key=lambda x: (x.name, x.identifier or ""))

    with time_machine.travel("2020-01-05 02:00:00 UTC"):
        # Add a new interval for the previous snapshot
        state_sync.add_interval(snapshot_a, "2020-01-04", "2020-01-04")
        assert (
            sorted(
                state_sync.interval_state.get_snapshot_intervals([snapshot_a, snapshot_b]),
                key=lambda x: (x.name, x.identifier or ""),
            )
            == expected_intervals
        )
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        ]

        state_sync.compact_intervals()
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == [
            (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]

    expected_intervals = [
        SnapshotIntervals(
            name=snapshot_a.name,
            identifier=snapshot_a.identifier,
            version=snapshot_a.version,
            dev_version=snapshot_a.dev_version,
            intervals=[
                (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
            ],
            dev_intervals=[],
            pending_restatement_intervals=[],
        ),
        SnapshotIntervals(
            name=snapshot_b.name,
            identifier=snapshot_b.identifier,
            version=snapshot_b.version,
            dev_version=snapshot_b.dev_version,
            intervals=[
                (to_timestamp("2020-01-03"), to_timestamp("2020-01-04")),
                (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
            ],
            dev_intervals=[],
            pending_restatement_intervals=[],
        ),
    ]
    expected_intervals = sorted(expected_intervals, key=lambda x: (x.name, x.identifier or ""))

    with time_machine.travel("2020-01-05 03:00:00 UTC"):
        state_sync.add_interval(snapshot_b, "2020-01-05", "2020-01-05")
        assert (
            sorted(
                state_sync.interval_state.get_snapshot_intervals([snapshot_a, snapshot_b]),
                key=lambda x: (x.name, x.identifier or ""),
            )
            == expected_intervals
        )
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == []
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == []

        state_sync.compact_intervals()
        snapshots = state_sync.get_snapshots([snapshot_a, snapshot_b])
        assert snapshots[snapshot_a.snapshot_id].pending_restatement_intervals == []
        assert snapshots[snapshot_a.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]
        assert snapshots[snapshot_b.snapshot_id].pending_restatement_intervals == []
        assert snapshots[snapshot_b.snapshot_id].intervals == [
            (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
        ]


def test_get_environments_summary(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    now_ts = now_timestamp()
    env_a_ttl = now_ts - 1000

    env_a = Environment(
        name="test_environment_a",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
        expiration_ts=env_a_ttl,
    )
    state_sync.promote(env_a)

    env_b_ttl = now_ts + 1000
    env_b = env_a.copy(update={"name": "test_environment_b", "expiration_ts": env_b_ttl})
    state_sync.promote(env_b)

    prod = Environment(
        name="prod",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
    )
    state_sync.promote(prod)

    actual = set(state_sync.get_environments_summary())
    expected = {prod.summary, env_a.summary, env_b.summary}
    assert actual == expected


def test_get_environments_summary_only_prod(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
) -> None:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select a, ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    state_sync.push_snapshots([snapshot])

    prod = Environment(
        name="prod",
        snapshots=[snapshot.table_info],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
        previous_plan_id="test_plan_id",
    )
    state_sync.promote(prod)
    actual = state_sync.get_environments_summary()
    expected = [prod.summary]
    assert actual == expected


def test_get_environments_summary_no_env(state_sync: EngineAdapterStateSync) -> None:
    assert state_sync.get_environments_summary() == []


@time_machine.travel("2020-01-05 00:00:00 UTC")
def test_compact_intervals_pending_restatement_many_snapshots_same_version(
    state_sync: EngineAdapterStateSync,
    make_snapshot: t.Callable,
    get_snapshot_intervals: t.Callable,
) -> None:
    snapshots = [
        make_snapshot(
            SqlModel(
                name="a",
                cron="@daily",
                query=parse_one(f"select {i}, ds"),
            ),
            version="a",
        )
        for i in range(100)
    ]

    state_sync.push_snapshots(snapshots)

    for snapshot in snapshots:
        state_sync.add_interval(snapshot, "2020-01-01", "2020-01-01")
        state_sync.add_interval(snapshot, "2020-01-02", "2020-01-02")
        state_sync.add_interval(snapshot, "2020-01-03", "2020-01-03")
        state_sync.add_interval(snapshot, "2020-01-04", "2020-01-04")

    pending_restatement_intervals = [
        (to_timestamp("2020-01-03"), to_timestamp("2020-01-05")),
    ]
    state_sync.add_snapshots_intervals(
        [
            SnapshotIntervals(
                name=snapshots[0].name,
                identifier=snapshots[0].identifier,
                version=snapshots[0].version,
                dev_version=snapshots[0].dev_version,
                intervals=[],
                dev_intervals=[],
                pending_restatement_intervals=pending_restatement_intervals,
            )
        ]
    )

    # Because of the number of snapshots requiring compaction, some compacted records will have different creation
    # timestamps.
    state_sync.compact_intervals()

    assert state_sync.get_snapshots([snapshots[0].snapshot_id])[
        snapshots[0].snapshot_id
    ].pending_restatement_intervals == [
        (to_timestamp("2020-01-03"), to_timestamp("2020-01-05")),
    ]


def test_update_environment_statements(state_sync: EngineAdapterStateSync):
    assert state_sync.get_environment_statements(environment="dev") == []

    environment = Environment(
        name="dev",
        snapshots=[],
        start_at="2022-01-01",
        end_at="2022-01-01",
        plan_id="test_plan_id",
    )
    environment_statements = [
        EnvironmentStatements(
            before_all=["CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"],
            after_all=["CREATE OR REPLACE TABLE table_2 AS SELECT 'b'"],
            python_env={},
        )
    ]

    state_sync.environment_state.update_environment(environment=environment)
    state_sync.environment_state.update_environment_statements(
        environment.name, environment.plan_id, environment_statements
    )

    environment_statements_dev = state_sync.get_environment_statements(environment="dev")
    assert environment_statements_dev[0].before_all == [
        "CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"
    ]
    assert environment_statements_dev[0].after_all == [
        "CREATE OR REPLACE TABLE table_2 AS SELECT 'b'"
    ]

    environment_statements = [
        EnvironmentStatements(
            before_all=["CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"],
            after_all=[
                "@grant_schema_usage()",
                "@grant_select_privileges()",
            ],
            python_env={},
        )
    ]

    state_sync.environment_state.update_environment(environment=environment)
    state_sync.environment_state.update_environment_statements(
        environment.name, environment.plan_id, environment_statements
    )

    environment_statements_dev = state_sync.get_environment_statements(environment="dev")
    assert environment_statements_dev[0].before_all == [
        "CREATE OR REPLACE TABLE table_1 AS SELECT 'a'"
    ]
    assert environment_statements_dev[0].after_all == [
        "@grant_schema_usage()",
        "@grant_select_privileges()",
    ]


def test_get_snapshots_by_names(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable[..., Snapshot]
):
    assert state_sync.get_snapshots_by_names(snapshot_names=[]) == set()

    snap_a_v1, snap_a_v2 = (
        make_snapshot(
            SqlModel(
                name="a",
                query=parse_one(f"select {i}, ds"),
            ),
            version="a",
        )
        for i in range(2)
    )

    snap_b = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one(f"select 'b' as b, ds"),
        ),
        version="b",
    )

    state_sync.push_snapshots([snap_a_v1, snap_a_v2, snap_b])

    assert {s.snapshot_id for s in state_sync.get_snapshots_by_names(snapshot_names=['"a"'])} == {
        snap_a_v1.snapshot_id,
        snap_a_v2.snapshot_id,
    }
    assert {
        s.snapshot_id for s in state_sync.get_snapshots_by_names(snapshot_names=['"a"', '"b"'])
    } == {
        snap_a_v1.snapshot_id,
        snap_a_v2.snapshot_id,
        snap_b.snapshot_id,
    }


def test_get_snapshots_by_names_include_expired(
    state_sync: EngineAdapterStateSync, make_snapshot: t.Callable[..., Snapshot]
):
    now_ts = now_timestamp()

    normal_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one(f"select 1, ds"),
        ),
        version="a",
    )

    expired_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one(f"select 2, ds"),
        ),
        version="a",
        ttl="in 10 seconds",
    )
    expired_a.updated_ts = now_ts - (
        1000 * 15
    )  # last updated 15 seconds ago, expired 10 seconds from last updated = expired 5 seconds ago

    state_sync.push_snapshots([normal_a, expired_a])

    assert {
        s.snapshot_id
        for s in state_sync.get_snapshots_by_names(snapshot_names=['"a"'], current_ts=now_ts)
    } == {normal_a.snapshot_id}
    assert {
        s.snapshot_id
        for s in state_sync.get_snapshots_by_names(snapshot_names=['"a"'], exclude_expired=False)
    } == {
        normal_a.snapshot_id,
        expired_a.snapshot_id,
    }

    # wind back time to 10 seconds ago (before the expired snapshot is expired - it expired 5 seconds ago) to test it stil shows in a normal query
    assert {
        s.snapshot_id
        for s in state_sync.get_snapshots_by_names(
            snapshot_names=['"a"'], current_ts=(now_ts - (10 * 1000))
        )
    } == {normal_a.snapshot_id, expired_a.snapshot_id}


def test_state_version_is_too_old(
    state_sync: EngineAdapterStateSync, mocker: MockerFixture
) -> None:
    state_sync.engine_adapter.replace_query(
        "sqlmesh._versions",
        pd.DataFrame(
            [{"schema_version": 59, "sqlmesh_version": "0.133.0", "sqlglot_version": "25.31.4"}]
        ),
        target_columns_to_types={
            "schema_version": exp.DataType.build("int"),
            "sqlglot_version": exp.DataType.build("text"),
            "sqlmesh_version": exp.DataType.build("text"),
        },
    )

    with pytest.raises(
        StateMigrationError,
        match="The current state belongs to an old version of SQLMesh that is no longer supported. Please upgrade to 0.134.0 first before upgrading to.*",
    ):
        state_sync.migrate(skip_backup=True)
