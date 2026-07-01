import pickle
import json
import typing as t
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import time_machine
from _pytest.monkeypatch import MonkeyPatch
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, to_column

from sqlmesh.core import constants as c
from sqlmesh.core.audit import StandaloneAudit
from sqlmesh.core.config import (
    AutoCategorizationMode,
    CategorizerConfig,
    EnvironmentSuffixTarget,
)
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import parse, parse_one
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.macros import SQL
from sqlmesh.core.model import (
    FullKind,
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    IncrementalUnmanagedKind,
    Model,
    Seed,
    SeedKind,
    SeedModel,
    SqlModel,
    create_seed_model,
    load_sql_based_model,
    CustomKind,
)
from sqlmesh.core.model.kind import TimeColumn, ModelKindName
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.signal import signal
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    QualifiedViewName,
    Snapshot,
    SnapshotId,
    SnapshotIdAndVersion,
    SnapshotChangeCategory,
    SnapshotFingerprint,
    SnapshotIntervals,
    SnapshotTableInfo,
    earliest_start_date,
    fingerprint_from_node,
    has_paused_forward_only,
    missing_intervals,
)
from sqlmesh.core.snapshot.cache import SnapshotCache
from sqlmesh.core.snapshot.categorizer import categorize_change
from sqlmesh.core.snapshot.definition import (
    apply_auto_restatements,
    display_name,
    get_next_model_interval_start,
    check_ready_intervals,
    _contiguous_intervals,
    table_name,
    TableNamingConvention,
)
from sqlmesh.core.config.common import VirtualEnvironmentMode
from sqlmesh.utils import AttributeDict
from sqlmesh.utils.date import DatetimeRanges, to_date, to_datetime, to_timestamp
from sqlmesh.utils.errors import SQLMeshError, SignalEvalError
from sqlmesh.utils.jinja import JinjaMacroRegistry, MacroInfo
from sqlmesh.utils.hashing import md5
from sqlmesh.core.console import get_console


@pytest.fixture
def parent_model():
    return SqlModel(
        name="parent.tbl",
        kind=dict(time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE),
        dialect="spark",
        query="SELECT 1, ds",
    )


@pytest.fixture
def model():
    return SqlModel(
        name="name",
        kind=dict(time_column="ds", batch_size=30, name=ModelKindName.INCREMENTAL_BY_TIME_RANGE),
        owner="owner",
        dialect="spark",
        cron="1 0 * * *",
        start="2020-01-01",
        query="SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl",
    )


@pytest.fixture
def snapshot(
    model: Model,
    parent_model: Model,
    monkeypatch: MonkeyPatch,
    mocker: MockerFixture,
    make_snapshot,
):
    mock = mocker.Mock()
    mock.return_value = to_datetime("2022-09-23T00:12:53+00:00")
    monkeypatch.setattr("sqlmesh.utils.date.now", mock)
    snapshot = make_snapshot(
        model,
        nodes={parent_model.fqn: parent_model, model.fqn: model},
    )
    snapshot.version = snapshot.fingerprint.to_version()
    return snapshot


def test_json(snapshot: Snapshot):
    assert json.loads(snapshot.json()) == {
        "created_ts": 1663891973000,
        "ttl": "in 1 week",
        "fingerprint": snapshot.fingerprint.dict(),
        "intervals": [],
        "dev_intervals": [],
        "dev_table_suffix": "dev",
        "pending_restatement_intervals": [],
        "node": {
            "audits": [],
            "audit_definitions": {},
            "clustered_by": [],
            "cron": "1 0 * * *",
            "kind": {
                "name": "INCREMENTAL_BY_TIME_RANGE",
                "time_column": {"column": "`ds`"},
                "batch_size": 30,
                "forward_only": False,
                "on_destructive_change": "ERROR",
                "on_additive_change": "ALLOW",
                "partition_by_time_column": True,
                "disable_restatement": False,
                "dialect": "spark",
            },
            "mapping_schema": {},
            "start": "2020-01-01",
            "dialect": "spark",
            "name": "name",
            "partitioned_by": [],
            "project": "",
            "python_env": {},
            "owner": "owner",
            "query": {
                "sql": "SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl",
            },
            "jinja_macros": {
                "create_builtins_module": "sqlmesh.utils.jinja",
                "global_objs": {},
                "packages": {},
                "root_macros": {},
                "top_level_packages": [],
            },
            "source_type": "sql",
            "tags": [],
            "grains": [],
            "references": [],
            "allow_partials": False,
            "signals": [],
            "enabled": True,
            "extract_dependencies_from_query": True,
            "virtual_environment_mode": "full",
            "grants_target_layer": "virtual",
        },
        "name": '"name"',
        "parents": [{"name": '"parent"."tbl"', "identifier": snapshot.parents[0].identifier}],
        "previous_versions": [],
        "table_naming_convention": "schema_and_table",
        "updated_ts": 1663891973000,
        "version": snapshot.fingerprint.to_version(),
        "migrated": False,
        "unrestorable": False,
        "forward_only": False,
    }


def test_json_with_grants(make_snapshot: t.Callable):
    from sqlmesh.core.model.meta import GrantsTargetLayer

    model = SqlModel(
        name="name",
        kind=dict(time_column="ds", batch_size=30, name=ModelKindName.INCREMENTAL_BY_TIME_RANGE),
        owner="owner",
        dialect="spark",
        cron="1 0 * * *",
        start="2020-01-01",
        query=parse_one("SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl"),
        grants={"SELECT": ["role1", "role2"], "INSERT": ["role3"]},
        grants_target_layer=GrantsTargetLayer.VIRTUAL,
    )
    snapshot = make_snapshot(model)

    json_str = snapshot.json()
    json_data = json.loads(json_str)
    assert (
        json_data["node"]["grants"]
        == "('SELECT' = ARRAY('role1', 'role2'), 'INSERT' = ARRAY('role3'))"
    )
    assert json_data["node"]["grants_target_layer"] == "virtual"

    reparsed_snapshot = Snapshot.model_validate_json(json_str)
    assert isinstance(reparsed_snapshot.node, SqlModel)
    assert reparsed_snapshot.node.grants == {"SELECT": ["role1", "role2"], "INSERT": ["role3"]}
    assert reparsed_snapshot.node.grants_target_layer == GrantsTargetLayer.VIRTUAL


def test_json_custom_materialization(make_snapshot: t.Callable):
    model = SqlModel(
        name="name",
        kind=dict(name=ModelKindName.CUSTOM, materialization="non_existent_should_still_work"),
        owner="owner",
        dialect="spark",
        cron="1 0 * * *",
        start="2020-01-01",
        query="SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl",
    )

    snapshot = make_snapshot(
        model,
        nodes={model.fqn: model},
    )
    snapshot.version = snapshot.fingerprint.to_version()

    # this should not throw an error even though the 'non_existent_should_still_work' custom materialization doesnt exist
    # this is so we can always deserialize a snapshot based on a custom materialization without the custom materialization class being loaded
    new_snapshot = Snapshot.model_validate_json(snapshot.json())
    assert new_snapshot == snapshot
    assert isinstance(new_snapshot.model.kind, CustomKind)
    assert new_snapshot.model.kind.materialization == "non_existent_should_still_work"
    assert new_snapshot.model.kind.materialization_properties == {}

    # this, however, should throw an error
    with pytest.raises(SQLMeshError, match=r"Materialization strategy.*was not found"):
        new_snapshot.model.validate_definition()


def test_add_interval(snapshot: Snapshot, make_snapshot):
    with pytest.raises(ValueError):
        snapshot.add_interval("2020-01-02", "2020-01-01")

    snapshot.add_interval("2020-01-01", "2020-01-01")
    assert snapshot.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    snapshot.add_interval("2020-01-02", "2020-01-02")
    assert snapshot.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-03"))]

    snapshot.add_interval("2020-01-04", "2020-01-05")
    assert snapshot.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
    ]
    snapshot.add_interval("2019-12-31", "2019-12-31")
    assert snapshot.intervals == [
        (to_timestamp("2019-12-31"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-04"), to_timestamp("2020-01-06")),
    ]
    snapshot.add_interval("2020-01-03", "2020-01-03")
    assert snapshot.intervals == [
        (to_timestamp("2019-12-31"), to_timestamp("2020-01-06")),
    ]
    snapshot.add_interval("2019-12-25", "2019-12-26")
    assert snapshot.intervals == [
        (to_timestamp("2019-12-25"), to_timestamp("2019-12-27")),
        (to_timestamp("2019-12-31"), to_timestamp("2020-01-06")),
    ]
    snapshot.add_interval("2019-01-01", "2020-01-30")
    assert snapshot.intervals == [
        (to_timestamp("2019-01-01"), to_timestamp("2020-01-31")),
    ]

    snapshot.add_interval("2020-01-01", "2020-01-31 00:00:00")
    assert snapshot.intervals == [
        (to_timestamp("2019-01-01"), to_timestamp("2020-01-31")),
    ]
    snapshot.add_interval("2019-01-01 00:00:00", "2020-01-31 00:00:01")
    assert snapshot.intervals == [
        (to_timestamp("2019-01-01"), to_timestamp("2020-01-31")),
    ]
    snapshot.add_interval("2018-12-31 23:59:59", "2020-01-31 12:00:01")
    assert snapshot.intervals == [
        (to_timestamp("2019-01-01"), to_timestamp("2020-01-31")),
    ]

    new_snapshot = make_snapshot(snapshot.model)
    new_snapshot.add_interval("2020-01-29", "2020-02-01")
    new_snapshot.add_interval("2020-02-05", "2020-02-10")
    new_snapshot.merge_intervals(snapshot)
    assert new_snapshot.intervals == [
        (to_timestamp("2019-01-01"), to_timestamp("2020-02-02")),
        (to_timestamp("2020-02-05"), to_timestamp("2020-02-11")),
    ]


def test_add_interval_dev(snapshot: Snapshot, make_snapshot):
    snapshot.version = "existing_version"
    snapshot.dev_version_ = "existing_dev_version"
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True

    snapshot.add_interval("2020-01-01", "2020-01-01")
    assert snapshot.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    snapshot.add_interval("2020-01-02", "2020-01-02", is_dev=True)
    assert snapshot.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]
    assert snapshot.dev_intervals == [(to_timestamp("2020-01-02"), to_timestamp("2020-01-03"))]

    new_snapshot = make_snapshot(snapshot.model)
    new_snapshot.merge_intervals(snapshot)
    assert new_snapshot.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]
    assert new_snapshot.dev_intervals == []

    new_snapshot = make_snapshot(snapshot.model)
    new_snapshot.dev_version_ = snapshot.dev_version
    new_snapshot.merge_intervals(snapshot)
    assert new_snapshot.intervals == [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]
    assert new_snapshot.dev_intervals == [(to_timestamp("2020-01-02"), to_timestamp("2020-01-03"))]


def test_add_interval_partial(snapshot: Snapshot, make_snapshot):
    snapshot.add_interval("2023-01-01 00:00:00", "2023-01-01 23:59:59")
    assert snapshot.intervals == []

    snapshot.add_interval("2023-01-01 00:00:00", "2023-01-01 14:00:00")
    assert snapshot.intervals == []

    snapshot.add_interval("2023-01-01 15:00:00", "2023-01-03 00:00:00")
    assert snapshot.intervals == [
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
    ]

    monthly_snapshot = make_snapshot(
        SqlModel(name="test_model", cron="0 7 1 * *", query=parse_one("SELECT 1"))
    )
    monthly_snapshot.add_interval("2023-01-01 15:00:00", "2023-01-01 16:00:00")
    assert monthly_snapshot.intervals == []

    monthly_snapshot.add_interval("2023-01-01 15:00:00", "2023-03-01 16:00:00")
    assert monthly_snapshot.intervals == [
        (to_timestamp("2023-02-01"), to_timestamp("2023-03-01")),
    ]


@time_machine.travel("2023-01-01 01:00:00 UTC")
def test_get_next_model_interval_start(make_snapshot):
    hourly_snapshot = make_snapshot(
        SqlModel(
            name="late",
            kind=FullKind(),
            query=parse_one("SELECT 1, ds FROM name"),
            cron="@hourly",
            interval_unit=IntervalUnit.HALF_HOUR,
        )
    )

    daily_snapshot = make_snapshot(
        SqlModel(
            name="early", kind=FullKind(), query=parse_one("SELECT 1, ds FROM name"), cron="@daily"
        )
    )

    seed_snapshot = make_snapshot(
        SeedModel(
            name="seed",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="content"),
            column_hashes={"col": "hash"},
            depends_on=set(),
            interval_unit=IntervalUnit.FIVE_MINUTE,
        )
    )

    audit_snapshot = make_snapshot(
        StandaloneAudit(
            name="test_standalone_audit",
            query=parse_one("SELECT 1"),
            interval_unit=IntervalUnit.FIVE_MINUTE,
        )
    )

    assert get_next_model_interval_start(
        [daily_snapshot, hourly_snapshot, seed_snapshot, audit_snapshot]
    ) == to_datetime("2023-01-01 02:00:00 UTC")


def test_missing_intervals(snapshot: Snapshot):
    snapshot.add_interval("2020-01-01", "2020-01-01")
    snapshot.add_interval("2020-01-03", "2020-01-05")
    assert snapshot.missing_intervals("2020-01-01", "2020-01-01") == []
    assert snapshot.missing_intervals("2020-01-02", "2020-01-02") == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03"))
    ]
    assert snapshot.missing_intervals("2020-01-02", "2020-01-03") == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03"))
    ]
    assert snapshot.missing_intervals("2020-01-01", "2020-01-03") == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03"))
    ]
    assert snapshot.missing_intervals("2020-01-03", "2020-01-03") == []
    assert snapshot.missing_intervals("2020-01-03", "2020-01-05") == []
    assert snapshot.missing_intervals("2020-01-03", "2020-01-06") == [
        (to_timestamp("2020-01-06"), to_timestamp("2020-01-07"))
    ]
    assert snapshot.missing_intervals("2020-01-03", "2020-01-07") == [
        (to_timestamp("2020-01-06"), to_timestamp("2020-01-07")),
        (to_timestamp("2020-01-07"), to_timestamp("2020-01-08")),
    ]
    assert snapshot.missing_intervals("2020-01-03 00:00:01", "2020-01-05 00:00:02") == []
    assert snapshot.missing_intervals("2020-01-03 00:00:01", "2020-01-07 00:00:02") == [
        (to_timestamp("2020-01-06"), to_timestamp("2020-01-07")),
        (to_timestamp("2020-01-07"), to_timestamp("2020-01-08")),
    ]


def test_missing_intervals_partial(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds")),
            owner="owner",
            cron="@daily",
            query=parse_one("SELECT 1, ds FROM name"),
            allow_partials=True,
        )
    )

    start = "2023-01-01"
    end_ts = to_timestamp(start) + 1000
    assert snapshot.missing_intervals(start, end_ts) == [
        (to_timestamp(start), end_ts),
    ]
    assert snapshot.missing_intervals(start, end_ts, execution_time=end_ts) == []
    assert snapshot.missing_intervals(start, end_ts, execution_time=end_ts, ignore_cron=True) == [
        (to_timestamp(start), end_ts)
    ]
    assert snapshot.missing_intervals(start, end_ts, execution_time="2023-01-02") == [
        (to_timestamp(start), end_ts)
    ]
    assert snapshot.missing_intervals(start, start) == [
        (to_timestamp(start), to_timestamp("2023-01-02")),
    ]
    assert snapshot.missing_intervals(start, start, execution_time=start, ignore_cron=True) == []
    assert snapshot.missing_intervals(start, start, execution_time=end_ts, end_bounded=True) == []

    assert snapshot.missing_intervals(start, to_timestamp("2023-01-02 12:00:00")) == [
        (to_timestamp(start), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-02 12:00:00")),
    ]


def test_missing_intervals_end_bounded_with_lookback(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds"), lookback=1),
            owner="owner",
            cron="@daily",
            query=parse_one("SELECT 1, ds FROM name"),
        )
    )

    start = "2023-01-01"
    end = "2023-01-02"

    snapshot.intervals = [(to_timestamp(start), to_timestamp(end))]

    execution_ts = to_timestamp("2023-01-03")
    assert snapshot.missing_intervals(start, start, execution_time=execution_ts) == []
    assert snapshot.missing_intervals(start, end, execution_time=execution_ts) == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
    ]


def test_missing_intervals_end_bounded_with_ignore_cron(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds")),
            owner="owner",
            cron="1 0 * * *",
            query=parse_one("SELECT 1, ds FROM name"),
        )
    )

    start = "2023-01-01"
    end = "2023-01-03"

    snapshot.intervals = [(to_timestamp(start), to_timestamp("2023-01-02"))]

    execution_ts = to_timestamp(end)
    assert snapshot.missing_intervals(start, end, execution_time=execution_ts) == []
    assert snapshot.missing_intervals(
        start, end, execution_time=execution_ts, ignore_cron=True
    ) == [
        (to_timestamp("2023-01-02"), to_timestamp(end)),
    ]
    assert (
        snapshot.missing_intervals(
            start, to_datetime(end), execution_time=execution_ts, end_bounded=True
        )
        == []
    )
    assert snapshot.missing_intervals(
        start, to_datetime(end), execution_time=execution_ts, ignore_cron=True, end_bounded=True
    ) == [
        (to_timestamp("2023-01-02"), to_timestamp(end)),
    ]


def test_missing_intervals_past_end_date_with_lookback(make_snapshot):
    snapshot: Snapshot = make_snapshot(  # type: ignore
        SqlModel(
            name="test_model",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds"), lookback=2),
            owner="owner",
            cron="@daily",
            query=parse_one("SELECT 1, ds FROM name"),
            start="2023-01-01",
            end="2023-01-05",  # inclusive, equivalent to to_timestamp('2023-01-05 23:59:59.999999')
        )
    )

    start_time = to_timestamp("2023-01-01")
    end_time = to_timestamp(
        "2023-01-06"
    )  # exclusive because to_timestamp() returns a timestamp and not a date
    assert snapshot.inclusive_exclusive(snapshot.node.start, snapshot.node.end) == (
        start_time,
        end_time,
    )

    # baseline - all intervals missing
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
    ]

    # fully backfill model - no intervals missing
    snapshot.add_interval(start_time, end_time)

    # even though lookback=2, because every interval has been filled,
    # there should be no missing intervals
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == []

    # however, when running for a new interval, this triggers lookback
    # in this case, we remove the most recent interval (the one for 2023-01-05) to simulate it being new
    # since lookback=2 days, this triggers missing intervals for 2023-01-03, 2023-01-04, 2023-01-05
    snapshot.remove_interval(interval=(to_timestamp("2023-01-05"), to_timestamp("2023-01-06")))
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == [
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
    ]

    # put the interval we just removed back to make the model fully backfilled again
    snapshot.add_interval(to_timestamp("2023-01-05"), to_timestamp("2023-01-06"))
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == []

    # running on the end date + 1 day (2023-01-07)
    # 2023-01-06 "would" run and since lookback=2 this pulls in 2023-01-04 and 2023-01-05 as well
    # however, only 2023-01-04 and 2023-01-05 are within the model end date
    end_time = to_timestamp("2023-01-07")
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == [
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
    ]

    # running on the end date + 2 days (2023-01-08)
    # 2023-01-07 "would" run and since lookback=2 this pulls in 2023-01-06 and 2023-01-05 as well
    # however, only 2023-01-05 is within the model end date
    end_time = to_timestamp("2023-01-08")
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == [
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06"))
    ]

    # running on the end date + 3 days (2023-01-09)
    # no missing intervals because subtracting 2 days for lookback exceeds the models end date
    end_time = to_timestamp("2023-01-09")
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == []

    # running way in the future, no missing intervals because subtracting 2 days for lookback still exceeds the models end date
    end_time = to_timestamp("2024-01-01")
    assert snapshot.missing_intervals(start_time, end_time, execution_time=end_time) == []


def test_missing_intervals_start_override_per_model(make_snapshot: t.Callable[..., Snapshot]):
    snapshot = make_snapshot(
        load_sql_based_model(
            parse("""
            MODEL (
                name a,
                kind FULL,
                start '2023-02-01',
                cron '@daily'
            );
            SELECT 1;
            """)
        ),
        version="a",
    )

    # base case - no override
    assert list(
        missing_intervals(execution_time="2023-02-08 00:05:07", snapshots=[snapshot]).values()
    )[0] == [
        (to_timestamp("2023-02-01"), to_timestamp("2023-02-02")),
        (to_timestamp("2023-02-02"), to_timestamp("2023-02-03")),
        (to_timestamp("2023-02-03"), to_timestamp("2023-02-04")),
        (to_timestamp("2023-02-04"), to_timestamp("2023-02-05")),
        (to_timestamp("2023-02-05"), to_timestamp("2023-02-06")),
        (to_timestamp("2023-02-06"), to_timestamp("2023-02-07")),
        (to_timestamp("2023-02-07"), to_timestamp("2023-02-08")),
    ]

    # with override, should use the overridden start date when calculating missing intervals
    assert list(
        missing_intervals(
            start="1 day ago",
            execution_time="2023-02-08 00:05:07",
            snapshots=[snapshot],
            start_override_per_model={snapshot.name: to_datetime("2023-02-05 00:00:00")},
        ).values()
    )[0] == [
        (to_timestamp("2023-02-05"), to_timestamp("2023-02-06")),
        (to_timestamp("2023-02-06"), to_timestamp("2023-02-07")),
        (to_timestamp("2023-02-07"), to_timestamp("2023-02-08")),
    ]


def test_incremental_time_self_reference(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds"), batch_size=1),
            owner="owner",
            dialect="",
            cron="@daily",
            start="1 week ago",
            query=parse_one("SELECT id, @end_ds as ds FROM name"),
        )
    )
    snapshot.add_interval(to_date("1 week ago"), to_date("1 day ago"))
    assert snapshot.missing_intervals(to_date("1 week ago"), to_date("1 day ago")) == []
    # Remove should take away not only 3 days ago but also everything after since this model
    # depends on past
    interval = snapshot.get_removal_interval(to_date("3 days ago"), to_date("3 days ago"))
    snapshot.remove_interval(interval)
    assert snapshot.missing_intervals(to_date("1 week ago"), to_date("1 day ago")) == [
        (to_timestamp(to_date("3 days ago")), to_timestamp(to_date("2 days ago"))),
        (to_timestamp(to_date("2 days ago")), to_timestamp(to_date("1 days ago"))),
        (to_timestamp(to_date("1 days ago")), to_timestamp(to_date("today"))),
    ]


def test_lookback(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds", lookback=2),
            cron="0 5 * * *",
            start="2023-01-01",
            query=parse_one("SELECT ds FROM parent.tbl"),
        )
    )

    assert snapshot.missing_intervals("2023-01-01", "2023-01-01") == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    ]

    snapshot.add_interval("2023-01-01", "2023-01-04")
    assert snapshot.missing_intervals("2023-01-01", "2023-01-04") == []
    assert snapshot.missing_intervals("2023-01-01", "2023-01-05") == [
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
    ]

    snapshot.add_interval("2023-01-06", "2023-01-07")
    assert snapshot.missing_intervals("2023-01-03", "2023-01-03") == []
    assert snapshot.missing_intervals("2023-01-05", "2023-01-05") == [
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
    ]
    assert snapshot.missing_intervals("2023-01-06", "2023-01-07") == []
    assert snapshot.missing_intervals("2023-01-05", "2023-01-08") == [
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
        (to_timestamp("2023-01-08"), to_timestamp("2023-01-09")),
    ]

    snapshot.add_interval("2023-01-28", "2023-01-29")
    assert snapshot.missing_intervals("2023-01-27", "2023-01-27", "2023-01-30 05:00:00") == [
        (to_timestamp("2023-01-27"), to_timestamp("2023-01-28")),
    ]
    assert snapshot.missing_intervals("2023-01-28", "2023-01-29", "2023-01-31 05:00:00") == []
    assert snapshot.missing_intervals("2023-01-28", "2023-01-30", "2023-01-31 05:00:00") == [
        (to_timestamp("2023-01-28"), to_timestamp("2023-01-29")),
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
        (to_timestamp("2023-01-30"), to_timestamp("2023-01-31")),
    ]

    assert snapshot.missing_intervals("2023-01-28", "2023-01-30", "2023-01-31 04:00:00") == []


def test_lookback_custom_materialization(make_snapshot):
    from sqlmesh import CustomMaterialization

    class MyTestStrategy(CustomMaterialization):
        pass

    expressions = parse(
        """
        MODEL (
            name name,
            kind CUSTOM (
                materialization 'MyTestStrategy',
                lookback 2
            ),
            start '2023-01-01',
            cron '0 5 * * *',
        );

        SELECT ds FROM parent.tbl
        """
    )

    snapshot = make_snapshot(load_sql_based_model(expressions))

    assert snapshot.missing_intervals("2023-01-01", "2023-01-01") == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    ]

    snapshot.add_interval("2023-01-01", "2023-01-04")
    assert snapshot.missing_intervals("2023-01-01", "2023-01-04") == []
    assert snapshot.missing_intervals("2023-01-01", "2023-01-05") == [
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
    ]


def test_seed_intervals(make_snapshot):
    snapshot_a = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="content"),
            column_hashes={"col": "hash"},
            depends_on=set(),
        )
    )

    assert snapshot_a.missing_intervals("2020-01-01", "2020-01-01") == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))
    ]
    snapshot_a.add_interval("2020-01-01", "2020-01-01")
    assert snapshot_a.missing_intervals("2020-01-02", "2020-01-02") == []


def test_missing_interval_smaller_than_interval_unit(make_snapshot):
    snapshot_daily = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds", batch_size=30),
            owner="owner",
            dialect="spark",
            cron="@daily",
            start="2020-01-01",
            query=parse_one("SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl"),
        )
    )

    assert snapshot_daily.missing_intervals("2020-01-01 00:00:05", "2020-01-01 23:59:59") == []
    assert snapshot_daily.missing_intervals("2020-01-01 00:00:00", "2020-01-02 00:00:00") == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))
    ]

    snapshot_hourly = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds", batch_size=30),
            owner="owner",
            dialect="spark",
            cron="@hourly",
            start="2020-01-01",
            query=parse_one("SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl"),
        )
    )

    assert snapshot_hourly.missing_intervals("2020-01-01 00:00:00", "2020-01-01 00:59:00") == []
    assert snapshot_hourly.missing_intervals("2020-01-01 00:00:00", "2020-01-01 01:00:00") == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-01 01:00:00"))
    ]

    snapshot_end_categorical = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds", batch_size=30),
            owner="owner",
            dialect="spark",
            cron="@daily",
            start="2020-01-01",
            query=parse_one("SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl"),
        )
    )

    assert snapshot_end_categorical.missing_intervals("2020-01-01 00:00:00", "2020-01-01") == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))
    ]

    snapshot_partial = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds", batch_size=30),
            owner="owner",
            dialect="spark",
            cron="@daily",
            start="2020-01-01",
            query=parse_one("SELECT @EACH([1, 2], x -> x), ds FROM parent.tbl"),
            allow_partials=True,
        )
    )

    assert snapshot_partial.missing_intervals("2020-01-01 00:00:05", "2020-01-01 23:59:59") == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-01 23:59:59"))
    ]
    assert snapshot_partial.missing_intervals("2020-01-01 00:00:00", "2020-01-02 00:00:00") == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))
    ]


def test_remove_intervals(snapshot):
    snapshot.add_interval("2020-01-01", "2020-01-01")
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-01", "2020-01-01"))
    assert snapshot.intervals == []

    snapshot.add_interval("2020-01-01", "2020-01-01")
    snapshot.add_interval("2020-01-03", "2020-01-03")
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-01", "2020-01-01"))
    assert snapshot.intervals == [(to_timestamp("2020-01-03"), to_timestamp("2020-01-04"))]

    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-01", "2020-01-05"))
    assert snapshot.intervals == []

    snapshot.add_interval("2020-01-01", "2020-01-05")
    snapshot.add_interval("2020-01-07", "2020-01-10")
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-03", "2020-01-04"))
    assert snapshot.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        (to_timestamp("2020-01-07"), to_timestamp("2020-01-11")),
    ]
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-01", "2020-01-01"))
    assert snapshot.intervals == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        (to_timestamp("2020-01-07"), to_timestamp("2020-01-11")),
    ]
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-10", "2020-01-10"))
    assert snapshot.intervals == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        (to_timestamp("2020-01-07"), to_timestamp("2020-01-10")),
    ]
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-07", "2020-01-07"))
    assert snapshot.intervals == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
        (to_timestamp("2020-01-08"), to_timestamp("2020-01-10")),
    ]
    snapshot.remove_interval(snapshot.get_removal_interval("2020-01-06", "2020-01-21"))
    assert snapshot.intervals == [
        (to_timestamp("2020-01-02"), to_timestamp("2020-01-03")),
        (to_timestamp("2020-01-05"), to_timestamp("2020-01-06")),
    ]
    snapshot.remove_interval(snapshot.get_removal_interval("2019-01-01", "2022-01-01"))
    assert snapshot.intervals == []


def test_get_removal_intervals_full_history_restatement_model(make_snapshot):
    execution_time = to_timestamp("2024-01-02")
    model = SqlModel(
        name="name",
        kind=IncrementalUnmanagedKind(),
        query=parse_one("SELECT 1"),
    )
    snapshot = make_snapshot(model)
    snapshot.add_interval("2020-01-01", "2024-01-01")

    interval = snapshot.get_removal_interval(
        "2023-01-01",
        "2023-01-01",
        execution_time=execution_time,
        is_preview=True,
    )
    assert interval == (to_timestamp("2023-01-01"), execution_time)

    interval = snapshot.get_removal_interval(
        "2023-01-01", "2023-01-01", execution_time=execution_time
    )
    assert interval == (to_timestamp("2020-01-01"), execution_time)
    snapshot.remove_interval(interval)

    assert not snapshot.intervals
    interval = snapshot.get_removal_interval(
        "2023-01-01", "2023-01-01", execution_time=execution_time
    )
    assert interval == (to_timestamp("2023-01-01"), execution_time)


def test_get_removal_intervals_warns_when_requested_range_automatically_widened(
    make_snapshot: t.Callable[..., Snapshot], mocker: MockerFixture
):
    mock_logger = mocker.patch.object(get_console(), "log_warning")

    # INCREMENTAL_BY_UNIQUE_KEY should warn
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByUniqueKeyKind(unique_key=[exp.to_column("id")]),
            query=parse_one("select id from src"),
        )
    )

    assert not snapshot.intervals
    assert snapshot.full_history_restatement_only

    snapshot.add_interval("2020-01-01", "2020-01-10")

    # should warn if requested intervals are a subset of actual intervals and thus are automatically expanded
    snapshot.get_removal_interval("2020-01-05", "2020-01-06")

    msg = mock_logger.call_args[0][0]
    assert "does not support partial restatement" in msg
    assert "Expanding the requested restatement intervals" in msg

    # should not warn if requested intervals are equal to actual intervals
    mock_logger.reset_mock()

    snapshot.get_removal_interval("2020-01-01", "2020-01-10")
    mock_logger.assert_not_called()

    # should not warn if requested intervals are a superset of actual intervals
    mock_logger.reset_mock()

    snapshot.get_removal_interval("2019-12-30", "2020-01-15")
    mock_logger.assert_not_called()

    # should not warn on models that support partial restatement, such as INCREMENTAL_BY_TIME_RANGE
    mock_logger.reset_mock()
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds")),
            query=parse_one("select ds from src"),
        )
    )

    assert not snapshot.intervals
    assert not snapshot.full_history_restatement_only

    snapshot.add_interval("2020-01-01", "2020-01-10")

    snapshot.get_removal_interval("2020-01-05", "2020-01-06")
    mock_logger.assert_not_called()


each_macro = lambda: "test"  # noqa: E731


def test_fingerprint(model: Model, parent_model: Model):
    original_model = deepcopy(model)
    fingerprint = fingerprint_from_node(model, nodes={})

    original_fingerprint = SnapshotFingerprint(
        data_hash="2406542604",
        metadata_hash="1056339358",
    )

    assert fingerprint == original_fingerprint
    with_parent_fingerprint = fingerprint_from_node(model, nodes={'"parent"."tbl"': parent_model})
    assert with_parent_fingerprint != fingerprint
    assert int(with_parent_fingerprint.parent_data_hash) > 0
    assert int(with_parent_fingerprint.parent_metadata_hash) > 0

    assert (
        fingerprint_from_node(
            model,
            nodes={
                '"parent"."tbl"': SqlModel(**{**model.dict(), "query": parse_one("select 2, ds")})
            },
        )
        != with_parent_fingerprint
    )
    model = SqlModel(**{**model.dict(), "query": parse_one("select 1, ds")})
    new_fingerprint = fingerprint_from_node(model, nodes={})
    assert new_fingerprint != fingerprint
    assert new_fingerprint.data_hash != fingerprint.data_hash
    assert new_fingerprint.metadata_hash != fingerprint.metadata_hash

    model = SqlModel(**{**model.dict(), "query": parse_one("select 1, ds -- annotation")})
    fingerprint = fingerprint_from_node(model, nodes={})
    assert new_fingerprint != fingerprint
    assert new_fingerprint.data_hash != fingerprint.data_hash
    assert new_fingerprint.metadata_hash != fingerprint.metadata_hash

    model = SqlModel(
        **{**original_model.dict(), "pre_statements": [parse_one("CREATE TABLE test")]}
    )
    fingerprint = fingerprint_from_node(model, nodes={})
    assert new_fingerprint != fingerprint
    assert new_fingerprint.data_hash != fingerprint.data_hash
    assert new_fingerprint.metadata_hash != fingerprint.metadata_hash
    assert fingerprint.metadata_hash != original_fingerprint.metadata_hash

    model = SqlModel(**{**original_model.dict(), "post_statements": [parse_one("DROP TABLE test")]})
    fingerprint = fingerprint_from_node(model, nodes={})
    assert new_fingerprint != fingerprint
    assert new_fingerprint.data_hash != fingerprint.data_hash
    assert new_fingerprint.metadata_hash != fingerprint.metadata_hash
    assert fingerprint.metadata_hash != original_fingerprint.metadata_hash


def test_fingerprint_seed_model():
    expressions = parse(
        """
        MODEL (
            name db.seed,
            kind SEED (
              path '../seeds/waiter_names.csv'
            )
        );
    """
    )

    expected_fingerprint = SnapshotFingerprint(
        data_hash="2112858704",
        metadata_hash="2674364560",
    )

    model = load_sql_based_model(expressions, path=Path("./examples/sushi/models/test_model.sql"))
    actual_fingerprint = fingerprint_from_node(model, nodes={})
    assert actual_fingerprint == expected_fingerprint

    # Make sure that the fingerprint doesn't change when the model is dehydrated.
    dehydrated_model = model.to_dehydrated()
    assert fingerprint_from_node(dehydrated_model, nodes={}) == expected_fingerprint

    updated_model = model.copy(
        update={
            "columns_to_types_": {
                "id": exp.DataType.build("int"),
                "name": exp.DataType.build("text"),
            }
        }
    )
    updated_actual_fingerprint = fingerprint_from_node(updated_model, nodes={})
    assert updated_actual_fingerprint.data_hash != expected_fingerprint.data_hash
    assert updated_actual_fingerprint.metadata_hash == expected_fingerprint.metadata_hash


def test_fingerprint_jinja_macros(model: Model):
    model = SqlModel(
        **{
            **model.dict(),
            "jinja_macros": JinjaMacroRegistry(
                root_macros={
                    "test_macro": MacroInfo(
                        definition="{% macro test_macro() %}a{% endmacro %}", depends_on=[]
                    )
                }
            ),
        }
    )
    original_fingerprint = SnapshotFingerprint(
        data_hash="93332825",
        metadata_hash="1056339358",
    )

    fingerprint = fingerprint_from_node(model, nodes={})
    assert fingerprint == original_fingerprint
    model = model.copy()

    model.jinja_macros.root_macros["test_macro"] = MacroInfo(
        definition="{% macro test_macro() %}b{% endmacro %}", depends_on=[]
    )
    updated_fingerprint = fingerprint_from_node(model, nodes={})
    assert updated_fingerprint.data_hash != original_fingerprint.data_hash
    assert updated_fingerprint.metadata_hash == original_fingerprint.metadata_hash


@pytest.mark.parametrize("global_obj_key", ["refs", "sources", "vars"])
def test_fingerprint_jinja_macros_global_objs(model: Model, global_obj_key: str):
    model = SqlModel(
        **{
            **model.dict(),
            "jinja_macros": JinjaMacroRegistry(),
        }
    )
    fingerprint = fingerprint_from_node(model, nodes={})
    model = model.copy()
    model.jinja_macros.global_objs[global_obj_key] = AttributeDict(
        {"test": AttributeDict({"test": "test"})}
    )
    updated_fingerprint = fingerprint_from_node(model, nodes={})
    assert updated_fingerprint.data_hash != fingerprint.data_hash
    assert updated_fingerprint.metadata_hash == fingerprint.metadata_hash


def test_fingerprint_builtin_audits(model: Model, parent_model: Model):
    fingerprint = fingerprint_from_node(model, nodes={})

    model = SqlModel.parse_obj(
        {**model.dict(), "audits": [("unique_values", {"columns": exp.convert([to_column("a")])})]}
    )
    new_fingerprint = fingerprint_from_node(model, nodes={})
    assert new_fingerprint != fingerprint


def test_fingerprint_standalone_audits(parent_model: Model):
    audit = StandaloneAudit(
        name="test_standalone_audit",
        query=parse_one(f"SELECT cola FROM {parent_model.name} WHERE cola IS NULL"),
    )
    fingerprint = fingerprint_from_node(audit, nodes={parent_model.name: parent_model})

    new_audit = StandaloneAudit(
        name="test_standalone_audit",
        query=parse_one(f"SELECT colb FROM {parent_model.name} WHERE colb IS NULL"),
    )
    new_fingerprint = fingerprint_from_node(new_audit, nodes={parent_model.name: parent_model})

    assert new_fingerprint != fingerprint
    assert new_fingerprint.data_hash == fingerprint.data_hash
    assert new_fingerprint.metadata_hash != fingerprint.metadata_hash


def test_fingerprint_virtual_properties(model: Model, parent_model: Model):
    original_model = deepcopy(model)
    fingerprint = fingerprint_from_node(model, nodes={})

    updated_model = SqlModel(
        **original_model.dict(),
        virtual_properties=parse_one("(labels = [('test-virtual-label', 'label-virtual-value')],)"),
    )
    assert "labels" in updated_model.virtual_properties
    updated_fingerprint = fingerprint_from_node(updated_model, nodes={})

    assert updated_fingerprint != fingerprint
    assert updated_fingerprint.metadata_hash != fingerprint.metadata_hash
    assert updated_fingerprint.data_hash == fingerprint.data_hash


def test_fingerprint_grants(model: Model, parent_model: Model):
    from sqlmesh.core.model.meta import GrantsTargetLayer

    original_model = deepcopy(model)
    fingerprint = fingerprint_from_node(model, nodes={})

    updated_model = SqlModel(
        **original_model.dict(),
        grants={"SELECT": ["role1", "role2"]},
    )
    updated_fingerprint = fingerprint_from_node(updated_model, nodes={})

    assert updated_fingerprint != fingerprint
    assert updated_fingerprint.metadata_hash != fingerprint.metadata_hash
    assert updated_fingerprint.data_hash == fingerprint.data_hash

    different_grants_model = SqlModel(
        **original_model.dict(),
        grants={"SELECT": ["role3"], "INSERT": ["role4"]},
    )
    different_grants_fingerprint = fingerprint_from_node(different_grants_model, nodes={})

    assert different_grants_fingerprint.metadata_hash != updated_fingerprint.metadata_hash
    assert different_grants_fingerprint.metadata_hash != fingerprint.metadata_hash

    target_layer_model = SqlModel(
        **{**original_model.dict(), "grants_target_layer": GrantsTargetLayer.PHYSICAL},
        grants={"SELECT": ["role1", "role2"]},
    )
    target_layer_fingerprint = fingerprint_from_node(target_layer_model, nodes={})

    assert target_layer_fingerprint.metadata_hash != updated_fingerprint.metadata_hash


def test_tableinfo_equality():
    snapshot_a = SnapshotTableInfo(
        name="test_schema.a",
        fingerprint=SnapshotFingerprint(data_hash="1", metadata_hash="1"),
        version="test_version",
        physical_schema="test_physical_schema",
        parents=[],
        dev_table_suffix="dev",
    )

    snapshot_b = SnapshotTableInfo(
        name="test_schema.b",
        fingerprint=SnapshotFingerprint(data_hash="1", metadata_hash="1"),
        version="test_version",
        physical_schema="test_physical_schema",
        parents=[],
        dev_table_suffix="dev",
    )

    snapshot_c = SnapshotTableInfo(
        name="test_schema.c",
        fingerprint=SnapshotFingerprint(data_hash="1", metadata_hash="1"),
        version="test_version",
        physical_schema="test_physical_schema",
        parents=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        dev_table_suffix="dev",
    )

    # parents in different order than snapshot_c
    snapshot_c2 = SnapshotTableInfo(
        name="test_schema.c",
        fingerprint=SnapshotFingerprint(data_hash="1", metadata_hash="1"),
        version="test_version",
        physical_schema="test_physical_schema",
        parents=[snapshot_b.snapshot_id, snapshot_a.snapshot_id],
        dev_table_suffix="dev",
    )

    assert snapshot_c is not snapshot_c2
    assert snapshot_c == snapshot_c2


def test_stamp(model: Model):
    original_fingerprint = fingerprint_from_node(model, nodes={})

    stamped_model = SqlModel(**{**model.dict(), "stamp": "test_stamp"})
    stamped_fingerprint = fingerprint_from_node(stamped_model, nodes={})

    assert original_fingerprint != stamped_fingerprint


def test_snapshot_table_name(snapshot: Snapshot, make_snapshot: t.Callable):
    # Mimic a direct breaking change.
    snapshot.fingerprint = SnapshotFingerprint(
        data_hash="1", metadata_hash="1", parent_data_hash="1"
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.table_naming_convention == TableNamingConvention.SCHEMA_AND_TABLE
    assert snapshot.data_version.table_naming_convention == TableNamingConvention.SCHEMA_AND_TABLE

    snapshot.previous_versions = ()
    assert snapshot.table_name(is_deployable=True) == "sqlmesh__default.name__3078928823"
    assert snapshot.table_name(is_deployable=False) == "sqlmesh__default.name__3078928823__dev"

    assert snapshot.dev_version == snapshot.fingerprint.to_version()

    # Mimic an indirect non-breaking change.
    previous_data_version = snapshot.data_version
    assert previous_data_version.physical_schema == "sqlmesh__default"
    snapshot.fingerprint = SnapshotFingerprint(
        data_hash="1", metadata_hash="1", parent_data_hash="2"
    )
    snapshot.previous_versions = (previous_data_version,)
    snapshot.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING)
    assert snapshot.table_name(is_deployable=True) == "sqlmesh__default.name__3078928823"
    # Indirect non-breaking snapshots reuse the dev table as well.
    assert snapshot.table_name(is_deployable=False) == "sqlmesh__default.name__3078928823__dev"
    assert snapshot.dev_version != snapshot.fingerprint.to_version()
    assert snapshot.dev_version == previous_data_version.dev_version

    # Mimic a direct forward-only change.
    snapshot.fingerprint = SnapshotFingerprint(
        data_hash="2", metadata_hash="1", parent_data_hash="1"
    )
    snapshot.previous_versions = (previous_data_version,)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    assert snapshot.table_name(is_deployable=True) == "sqlmesh__default.name__3078928823"
    assert snapshot.table_name(is_deployable=False) == "sqlmesh__default.name__3049392110__dev"

    fully_qualified_snapshot = make_snapshot(
        SqlModel(name='"my-catalog".db.table', query=parse_one("select 1, ds"))
    )
    fully_qualified_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert (
        fully_qualified_snapshot.table_name()
        == f'"my-catalog".sqlmesh__db.db__table__{fully_qualified_snapshot.version}'
    )
    non_fully_qualified_snapshot = make_snapshot(
        SqlModel(
            name="db.table", query=parse_one("select 1, ds"), default_catalog='"other-catalog"'
        )
    )
    non_fully_qualified_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert (
        non_fully_qualified_snapshot.table_name(is_deployable=True)
        == f'"other-catalog".sqlmesh__db.db__table__{non_fully_qualified_snapshot.version}'
    )


def test_table_name_naming_convention_table_only(make_snapshot: t.Callable[..., Snapshot]):
    # 3-part naming
    snapshot = make_snapshot(
        SqlModel(name='"foo"."bar"."baz"', query=parse_one("select 1")),
        table_naming_convention=TableNamingConvention.TABLE_ONLY,
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.table_naming_convention == TableNamingConvention.TABLE_ONLY
    assert snapshot.data_version.table_naming_convention == TableNamingConvention.TABLE_ONLY

    assert snapshot.table_name(is_deployable=True) == f"foo.sqlmesh__bar.baz__{snapshot.version}"
    assert (
        snapshot.table_name(is_deployable=False) == f"foo.sqlmesh__bar.baz__{snapshot.version}__dev"
    )

    # 2-part naming
    snapshot = make_snapshot(
        SqlModel(name='"foo"."bar"', query=parse_one("select 1")),
        table_naming_convention=TableNamingConvention.TABLE_ONLY,
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snapshot.table_name(is_deployable=True) == f"sqlmesh__foo.bar__{snapshot.version}"
    assert snapshot.table_name(is_deployable=False) == f"sqlmesh__foo.bar__{snapshot.version}__dev"


def test_table_name_naming_convention_hash_md5(make_snapshot: t.Callable[..., Snapshot]):
    # 3-part naming
    snapshot = make_snapshot(
        SqlModel(name='"foo"."bar"."baz"', query=parse_one("select 1")),
        table_naming_convention=TableNamingConvention.HASH_MD5,
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.table_naming_convention == TableNamingConvention.HASH_MD5
    assert snapshot.data_version.table_naming_convention == TableNamingConvention.HASH_MD5

    hash = md5(f"foo.sqlmesh__bar.bar__baz__{snapshot.version}")
    assert snapshot.table_name(is_deployable=True) == f"foo.sqlmesh__bar.sqlmesh_md5__{hash}"
    hash_dev = md5(f"foo.sqlmesh__bar.bar__baz__{snapshot.version}__dev")
    assert (
        snapshot.table_name(is_deployable=False) == f"foo.sqlmesh__bar.sqlmesh_md5__{hash_dev}__dev"
    )

    # 2-part naming
    snapshot = make_snapshot(
        SqlModel(name='"foo"."bar"', query=parse_one("select 1")),
        table_naming_convention=TableNamingConvention.HASH_MD5,
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    hash = md5(f"sqlmesh__foo.foo__bar__{snapshot.version}")
    assert snapshot.table_name(is_deployable=True) == f"sqlmesh__foo.sqlmesh_md5__{hash}"

    hash_dev = md5(f"sqlmesh__foo.foo__bar__{snapshot.version}__dev")
    assert snapshot.table_name(is_deployable=False) == f"sqlmesh__foo.sqlmesh_md5__{hash_dev}__dev"


def test_table_naming_convention_passed_around_correctly(make_snapshot: t.Callable[..., Snapshot]):
    snapshot = make_snapshot(
        SqlModel(name='"foo"."bar"."baz"', query=parse_one("select 1")),
        table_naming_convention=TableNamingConvention.HASH_MD5,
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snapshot.table_naming_convention == TableNamingConvention.HASH_MD5
    assert snapshot.data_version.table_naming_convention == TableNamingConvention.HASH_MD5
    assert snapshot.table_info.table_naming_convention == TableNamingConvention.HASH_MD5
    assert (
        snapshot.table_info.data_version.table_naming_convention == TableNamingConvention.HASH_MD5
    )
    assert snapshot.table_info.table_info.table_naming_convention == TableNamingConvention.HASH_MD5
    assert (
        snapshot.table_info.table_info.data_version.table_naming_convention
        == TableNamingConvention.HASH_MD5
    )


def test_table_name_view(make_snapshot: t.Callable):
    # Mimic a direct breaking change.
    snapshot = make_snapshot(SqlModel(name="name", query=parse_one("select 1"), kind="VIEW"))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.previous_versions = ()
    assert snapshot.table_name(is_deployable=True) == f"sqlmesh__default.name__{snapshot.version}"
    assert (
        snapshot.table_name(is_deployable=False)
        == f"sqlmesh__default.name__{snapshot.dev_version}__dev"
    )

    assert snapshot.dev_version == snapshot.fingerprint.to_version()

    # Mimic an indirect non-breaking change.
    new_snapshot = make_snapshot(SqlModel(name="name", query=parse_one("select 2"), kind="VIEW"))
    previous_data_version = snapshot.data_version
    new_snapshot.previous_versions = (previous_data_version,)
    new_snapshot.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING)
    assert (
        new_snapshot.table_name(is_deployable=True) == f"sqlmesh__default.name__{snapshot.version}"
    )
    # Indirect non-breaking view snapshots should not reuse the dev table.
    assert (
        new_snapshot.table_name(is_deployable=False)
        == f"sqlmesh__default.name__{new_snapshot.dev_version}__dev"
    )
    assert new_snapshot.dev_version == new_snapshot.fingerprint.to_version()
    assert new_snapshot.version == snapshot.version
    assert new_snapshot.dev_version != snapshot.dev_version


def test_table_naming_convention_change_reuse_previous_version(make_snapshot):
    # Ensure that snapshots that trigger "reuse previous version" inherit the naming convention of the previous snapshot
    original_snapshot: Snapshot = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds")),
        table_naming_convention=TableNamingConvention.SCHEMA_AND_TABLE,
    )
    original_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    assert original_snapshot.table_naming_convention == TableNamingConvention.SCHEMA_AND_TABLE
    assert original_snapshot.table_name() == f"sqlmesh__default.a__{original_snapshot.version}"

    changed_snapshot: Snapshot = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, 'forward_only' as a, ds")),
        table_naming_convention=TableNamingConvention.HASH_MD5,
    )
    changed_snapshot.previous_versions = original_snapshot.all_versions

    assert changed_snapshot.previous_version == original_snapshot.data_version

    changed_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    # inherited from previous version even though changed_snapshot was created with TableNamingConvention.HASH_MD5
    assert changed_snapshot.table_naming_convention == TableNamingConvention.SCHEMA_AND_TABLE
    assert (
        changed_snapshot.previous_version.table_naming_convention
        == TableNamingConvention.SCHEMA_AND_TABLE
    )
    assert changed_snapshot.table_name() == f"sqlmesh__default.a__{changed_snapshot.version}"


def test_categorize_change_sql(make_snapshot):
    old_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))

    config = CategorizerConfig(sql=AutoCategorizationMode.SEMI)

    # A projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, 2, ds"))),
            old=old_snapshot,
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # A complex projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a", query=parse_one("select 1, fun(another_fun(a + 1) * 2)::INT, ds")
                )
            ),
            old=old_snapshot,
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # Multiple projections have been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, 2, a, b, ds"))),
            old=old_snapshot,
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # No change.
    with pytest.raises(SQLMeshError):
        categorize_change(old_snapshot, old_snapshot, config=config)

    # A projection has been removed.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select ds"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A projection has been replaced.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 2, ds"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A projection has been moved.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select ds, 1"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A WHERE clause has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds WHERE a = 2"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A FROM clause has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds FROM test_table"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # DISTINCT has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select DISTINCT 1, ds"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # An EXPLODE projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds, explode(a)"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # An EXPLODE_OUTER projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("select 1, ds, explode_outer(a)"))
            ),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A POSEXPLODE projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds, posexplode(a)"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A POSEXPLODE_OUTER projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("select 1, ds, posexplode_outer(a)"))
            ),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # An UNNEST projection has been added.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds, unnest(a)"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A metadata change occurred
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds"), owner="foo")),
            old=old_snapshot,
            config=config,
        )
        is SnapshotChangeCategory.NON_BREAKING
    )

    # A UDTF subquery has been added.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("select 1, ds, (select x from unnest(a) x)"))
            ),
            old=old_snapshot,
            config=config,
        )
        is SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("select 1, ds, (select x from posexplode(a) x)"))
            ),
            old=old_snapshot,
            config=config,
        )
        is SnapshotChangeCategory.NON_BREAKING
    )


def test_categorize_change_sql_redundant_cast(make_snapshot):
    # Adding a column with a redundant CAST above an existing same-type cast makes SQLGlot's tree
    # diff emit spurious Move/Update edits (interchangeable DataType leaves get cross-matched),
    # even though the change is purely an added projection. These must remain NON_BREAKING.
    config = CategorizerConfig(sql=AutoCategorizationMode.SEMI)

    old_snapshot = make_snapshot(
        SqlModel(name="a", query=parse_one("SELECT a::DATE, s::TEXT FROM t"))
    )

    # A same-type cast column has been inserted mid-list, above the existing one.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE, x::TEXT, s::TEXT FROM t"))
            ),
            old=old_snapshot,
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # Multiple same-type cast columns have been inserted mid-list.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a", query=parse_one("SELECT a::DATE, x::TEXT, s::TEXT, y::INT FROM t")
                )
            ),
            old=old_snapshot,
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # The type of an existing projection has changed (in addition to a new column): undetermined.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::INT, x::TEXT, s::TEXT FROM t"))
            ),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # Existing projections have been reordered: undetermined.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT s::TEXT, a::DATE FROM t"))
            ),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # An existing projection has been removed: undetermined.
    assert (
        categorize_change(
            new=make_snapshot(SqlModel(name="a", query=parse_one("SELECT s::TEXT FROM t"))),
            old=old_snapshot,
            config=config,
        )
        is None
    )

    # A WHERE clause changed alongside the added cast column: undetermined.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a",
                    query=parse_one("SELECT a::DATE, x::TEXT, s::TEXT FROM t WHERE a = 2"),
                )
            ),
            old=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE, s::TEXT FROM t WHERE a = 1"))
            ),
            config=config,
        )
        is None
    )

    # Mid-list insert with ORDER BY ordinal shifts the referenced projection: undetermined.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a",
                    query=parse_one("SELECT a::DATE, x::TEXT, s::TEXT FROM t ORDER BY 2"),
                )
            ),
            old=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE, s::TEXT FROM t ORDER BY 2"))
            ),
            config=config,
        )
        is None
    )

    # Append at end with ORDER BY ordinal leaves existing ordinal bindings unchanged.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a",
                    query=parse_one("SELECT a::DATE, s::TEXT, x::TEXT FROM t ORDER BY 2"),
                )
            ),
            old=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE, s::TEXT FROM t ORDER BY 2"))
            ),
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # Mid-list insert with GROUP BY ordinal shifts the referenced projection: undetermined.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a",
                    query=parse_one("SELECT a::DATE, x::TEXT, s::TEXT FROM t GROUP BY 2"),
                )
            ),
            old=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE, s::TEXT FROM t GROUP BY 2"))
            ),
            config=config,
        )
        is None
    )

    # Aliased UDTF projection via fallback path: undetermined.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a",
                    query=parse_one(
                        "SELECT a::DATE AS a, x::TEXT AS x, EXPLODE(y) AS y, s::TEXT AS s FROM t"
                    ),
                )
            ),
            old=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE AS a, s::TEXT AS s FROM t"))
            ),
            config=config,
        )
        is None
    )

    # UDTF inside aliased scalar subquery remains non-breaking.
    assert (
        categorize_change(
            new=make_snapshot(
                SqlModel(
                    name="a",
                    query=parse_one(
                        "SELECT a::DATE AS a, (SELECT x FROM unnest(b) x) AS sub, s::TEXT AS s FROM t"
                    ),
                )
            ),
            old=make_snapshot(
                SqlModel(name="a", query=parse_one("SELECT a::DATE AS a, s::TEXT AS s FROM t"))
            ),
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )


def test_categorize_change_seed(make_snapshot, tmp_path):
    config = CategorizerConfig(seed=AutoCategorizationMode.SEMI)
    model_name = "test_db.test_seed_model"
    seed_path = tmp_path / "seed.csv"
    model_kind = SeedKind(path=str(seed_path.absolute()))

    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_c
1,text_a,1.0
2,text_b,2.0"""
        )

    original_snapshot = make_snapshot(create_seed_model(model_name, model_kind))

    # New column.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_d,col_b,col_c
1,,text_a,1.0
2,test,text_b,2.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        == SnapshotChangeCategory.NON_BREAKING
    )

    # Column removed.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_c
1,1.0
2,2.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )

    # Column renamed.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_d
1,text_a,1.0
2,text_b,2.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )

    # New row.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_c
1,text_a,1.0
2,text_b,2.0
3,text_c,3.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )

    # Deleted row.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_c
1,text_a,1.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )

    # Numeric column changed.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_c
1,text_a,1.0
2,text_b,3.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )

    # Text column changed.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_c
1,text_a,1.0
2,text_c,2.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )

    # Column type changed.
    with open(seed_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
col_a,col_b,col_c
1,text_a,1.0
2.0,text_b,2.0"""
        )

    assert (
        categorize_change(
            new=make_snapshot(create_seed_model(model_name, model_kind)),
            old=original_snapshot,
            config=config,
        )
        is None
    )


def test_effective_from(snapshot: Snapshot):
    previous_snapshot = deepcopy(snapshot)
    previous_snapshot.add_interval("2023-01-01", "2023-01-05")
    previous_snapshot.add_interval("2023-01-07", "2023-01-09")

    new_snapshot_same_fingerprint = deepcopy(snapshot)
    new_snapshot_same_fingerprint.effective_from = "2023-01-04"
    new_snapshot_same_fingerprint.merge_intervals(previous_snapshot)

    assert new_snapshot_same_fingerprint.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-10")),
    ]

    new_snapshot_different_fingerprint = deepcopy(snapshot)
    new_snapshot_different_fingerprint.fingerprint = SnapshotFingerprint(
        data_hash="1", metadata_hash="1", parent_data_hash="1"
    )
    new_snapshot_different_fingerprint.effective_from = "2023-01-04"
    new_snapshot_different_fingerprint.merge_intervals(previous_snapshot)

    assert new_snapshot_different_fingerprint.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-04")),
    ]


def test_physical_schema(snapshot: Snapshot):
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snapshot.physical_schema == "sqlmesh__default"
    assert snapshot.data_version.physical_schema == "sqlmesh__default"
    assert snapshot.table_info.physical_schema == "sqlmesh__default"

    snapshot.physical_schema_ = "custom_schema"
    assert snapshot.physical_schema == "custom_schema"
    assert snapshot.data_version.physical_schema == "custom_schema"
    assert snapshot.table_info.physical_schema == "custom_schema"

    # Make sure the previous physical schema is preserved for forward-only snapshots.
    new_snapshot = deepcopy(snapshot)
    new_snapshot.previous_versions = (snapshot.data_version,)
    new_snapshot.physical_schema_ = None
    new_snapshot.version = None
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    assert new_snapshot.physical_schema == "custom_schema"
    assert new_snapshot.data_version.physical_schema == "custom_schema"
    assert new_snapshot.table_info.physical_schema == "custom_schema"


def test_has_paused_forward_only(snapshot: Snapshot):
    assert not has_paused_forward_only([snapshot], [snapshot])

    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    assert has_paused_forward_only([snapshot], [snapshot])

    snapshot.unpaused_ts = to_timestamp("2023-01-01")
    assert not has_paused_forward_only([snapshot], [snapshot])


def test_inclusive_exclusive_monthly(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds"), batch_size=1),
            owner="owner",
            dialect="",
            cron="@monthly",
            start="1 week ago",
            query=parse_one("SELECT id, @end_ds as ds FROM name"),
        )
    )

    assert snapshot.inclusive_exclusive("2023-01-01", "2023-07-01") == (
        to_timestamp("2023-01-01"),
        to_timestamp("2023-08-01"),
    )

    assert snapshot.inclusive_exclusive("2023-01-01", "2023-07-06") == (
        to_timestamp("2023-01-01"),
        to_timestamp("2023-08-01"),
    )

    assert snapshot.inclusive_exclusive("2023-01-01", "2023-07-31") == (
        to_timestamp("2023-01-01"),
        to_timestamp("2023-08-01"),
    )


def test_inclusive_exclusive_hourly(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds"), batch_size=1),
            owner="owner",
            dialect="",
            cron="@hourly",
            start="2023-01-29",
            query=parse_one("SELECT id, @end_ds as ds FROM name"),
        )
    )

    target_date = "2023-01-29"
    target_dt = to_datetime(target_date)

    assert snapshot.missing_intervals(target_date, target_date) == [
        (
            to_timestamp(target_dt + timedelta(hours=h)),
            to_timestamp(target_dt + timedelta(hours=h + 1)),
        )
        for h in range(24)
    ]


def test_model_custom_cron(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            cron="0 5 * * *",
            start="2023-01-01",
            query=parse_one("SELECT ds FROM parent.tbl"),
        )
    )

    # Run at 5;00AM
    assert snapshot.missing_intervals(
        "2023-01-29", "2023-01-29", execution_time="2023-01-30 05:00:00"
    ) == [
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
    ]
    assert snapshot.missing_intervals(
        to_timestamp("2023-01-29"), to_timestamp("2023-01-30"), execution_time="2023-01-30 05:00:00"
    ) == [
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
    ]

    # Run at 5;01AM
    assert snapshot.missing_intervals(
        "2023-01-29", "2023-01-29", execution_time="2023-01-30 05:01:00"
    ) == [
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
    ]
    assert snapshot.missing_intervals(
        to_timestamp("2023-01-29"), to_timestamp("2023-01-30"), execution_time="2023-01-30 05:01:00"
    ) == [
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
    ]

    # Run at 4:59AM
    assert (
        snapshot.missing_intervals("2023-01-29", "2023-01-29", execution_time="2023-01-30 04:59:00")
        == []
    )
    assert (
        snapshot.missing_intervals(
            to_timestamp("2023-01-29"),
            to_timestamp("2023-01-30"),
            execution_time="2023-01-30 04:59:00",
        )
        == []
    )

    # Run at 4:59AM and ignore cron
    assert snapshot.missing_intervals(
        "2023-01-29", "2023-01-29", execution_time="2023-01-30 04:59:00", ignore_cron=True
    ) == [
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
    ]
    assert snapshot.missing_intervals(
        to_timestamp("2023-01-29"),
        to_timestamp("2023-01-30"),
        execution_time="2023-01-30 04:59:00",
        ignore_cron=True,
    ) == [
        (to_timestamp("2023-01-29"), to_timestamp("2023-01-30")),
    ]


def test_model_custom_interval_unit(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            cron="0 5 * * *",
            interval_unit="hour",
            start="2023-01-01",
            query=parse_one("SELECT ds FROM parent.tbl"),
        )
    )

    assert len(snapshot.missing_intervals("2023-01-29", "2023-01-29")) == 24


def test_is_valid_start(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test",
            query="SELECT 1 FROM test",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.depends_on_self
    assert snapshot.is_valid_start("2023-01-01", "2023-01-01")
    assert snapshot.is_valid_start("2023-01-01", "2023-01-02")
    assert snapshot.is_valid_start("2023-01-02", "2023-01-01 10:00:00")
    assert not snapshot.is_valid_start("2023-01-02", "2023-01-01")
    snapshot.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
    assert snapshot.is_valid_start("2023-01-01", "2023-01-01")
    assert snapshot.is_valid_start("2023-01-02", "2023-01-01")
    assert not snapshot.is_valid_start("2023-01-03", "2023-01-01")
    snapshot.intervals = []
    assert snapshot.is_valid_start("2023-01-01", "2023-01-01")
    assert snapshot.is_valid_start("2023-01-01", "2023-01-02")
    assert not snapshot.is_valid_start("2023-01-02", "2023-01-01")


@pytest.mark.parametrize(
    "qualified_view_name, environment_naming_info, expected",
    (
        (
            QualifiedViewName(catalog="a-b", schema_name="c", table="d"),
            EnvironmentNamingInfo(),
            '"a-b".c.d',
        ),
        (
            QualifiedViewName(catalog="a-b", schema_name="c-d", table="e"),
            EnvironmentNamingInfo(name="dev"),
            '"a-b"."c-d__dev".e',
        ),
        (
            QualifiedViewName(catalog="a-b", schema_name="c-d", table="e-f"),
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.TABLE),
            '"a-b"."c-d"."e-f__dev"',
        ),
        (
            QualifiedViewName(catalog="a-b", schema_name="c-d", table="e-f"),
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.SCHEMA),
            '"a-b"."c-d__dev"."e-f"',
        ),
        (
            QualifiedViewName(catalog="a-b", schema_name="c-d", table="e-f"),
            EnvironmentNamingInfo(name="dev", catalog_name_override="g-h"),
            '"g-h"."c-d__dev"."e-f"',
        ),
        (
            QualifiedViewName(schema_name="c-d", table="e-f"),
            EnvironmentNamingInfo(name="dev", catalog_name_override="g-h"),
            '"g-h"."c-d__dev"."e-f"',
        ),
        (
            QualifiedViewName(table="e-f"),
            EnvironmentNamingInfo(name="dev", catalog_name_override="g-h"),
            '"g-h".default__dev."e-f"',
        ),
        (
            QualifiedViewName(table="e-f"),
            EnvironmentNamingInfo(name="dev"),
            'default__dev."e-f"',
        ),
        # EnvironmentSuffixTarget.CATALOG
        (
            QualifiedViewName(
                catalog="default-foo", schema_name="sqlmesh_example", table="full_model"
            ),
            EnvironmentNamingInfo(
                name="dev",
                suffix_target=EnvironmentSuffixTarget.CATALOG,
            ),
            '"default-foo__dev".sqlmesh_example.full_model',
        ),
        (
            QualifiedViewName(catalog="default", schema_name="sqlmesh_example", table="full_model"),
            EnvironmentNamingInfo(
                name=c.PROD,
                catalog_name_override=None,
                suffix_target=EnvironmentSuffixTarget.CATALOG,
            ),
            "default.sqlmesh_example.full_model",
        ),
    ),
)
def test_qualified_view_name(qualified_view_name, environment_naming_info, expected):
    assert qualified_view_name.for_environment(environment_naming_info) == expected


def test_qualified_view_name_with_dialect():
    qualified_view_name = QualifiedViewName(catalog="catalog", schema_name="db", table="table")
    environment_naming_info = EnvironmentNamingInfo(name="dev", catalog_name_override="override")
    assert (
        qualified_view_name.for_environment(environment_naming_info, dialect="snowflake")
        == "OVERRIDE.db__DEV.table"
    )


def test_multi_interval_merge(make_snapshot):
    a = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            cron="@daily",
            interval_unit="five_minute",
            start="2023-01-01",
            query=parse_one("SELECT ds FROM parent.tbl"),
        )
    )

    a.add_interval("2023-01-01 00:05:00", "2023-01-01 00:51:00")
    a_start, a_end = a.intervals[0]

    assert a_start == to_timestamp("2023-01-01 00:05:00")
    assert a_end == to_timestamp("2023-01-01 00:50:00")

    b = make_snapshot(
        SqlModel(
            name="name",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            cron="@daily",
            interval_unit="quarter_hour",
            start="2023-01-01",
            query=parse_one("SELECT ds FROM parent.tbl"),
        )
    )

    b.merge_intervals(a)
    b_start, b_end = b.intervals[0]

    assert b_start == to_timestamp("2023-01-01 00:15:00")
    assert b_end == to_timestamp("2023-01-01 00:45:00")


def test_earliest_start_date(sushi_context: Context):
    model_name = "sushi.waiter_names"
    fqn_name = '"memory"."sushi"."waiter_names"'
    assert sushi_context.get_snapshot(model_name, raise_if_missing=True).node.start is None

    cache: t.Dict[str, datetime] = {}
    earliest_start_date(sushi_context.snapshots.values(), cache)
    assert cache[fqn_name] == to_datetime("yesterday")


def test_deployability_index(make_snapshot):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("SELECT 1")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_b.parents = (snapshot_a.snapshot_id,)

    snapshot_c = make_snapshot(SqlModel(name="c", query=parse_one("SELECT 1")))
    snapshot_c.categorize_as(SnapshotChangeCategory.INDIRECT_BREAKING)
    snapshot_c.parents = (snapshot_b.snapshot_id,)

    snapshot_d = make_snapshot(SqlModel(name="d", query=parse_one("SELECT 1")))
    snapshot_d.categorize_as(SnapshotChangeCategory.INDIRECT_BREAKING)
    snapshot_d.parents = (snapshot_b.snapshot_id, snapshot_a.snapshot_id)

    snapshot_e = make_snapshot(SqlModel(name="e", query=parse_one("SELECT 1")))
    snapshot_e.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    snapshot_f = make_snapshot(SqlModel(name="f", query=parse_one("SELECT 1")))
    snapshot_f.categorize_as(SnapshotChangeCategory.INDIRECT_BREAKING)
    snapshot_f.parents = (snapshot_e.snapshot_id, snapshot_a.snapshot_id)

    snapshot_g = make_snapshot(SqlModel(name="g", query=parse_one("SELECT 1")))
    snapshot_g.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
    snapshot_g.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING)
    snapshot_g.parents = (snapshot_e.snapshot_id,)

    snapshots = {
        s.snapshot_id: s
        for s in [
            snapshot_a,
            snapshot_b,
            snapshot_c,
            snapshot_d,
            snapshot_e,
            snapshot_f,
            snapshot_g,
        ]
    }

    deployability_index = DeployabilityIndex.create(snapshots)

    assert deployability_index.is_deployable(snapshot_a)
    assert deployability_index.is_deployable(snapshot_e)
    assert deployability_index.is_deployable(snapshot_f)
    assert not deployability_index.is_deployable(snapshot_g)
    assert not deployability_index.is_deployable(snapshot_b)
    assert not deployability_index.is_deployable(snapshot_c)
    assert not deployability_index.is_deployable(snapshot_d)

    assert deployability_index.is_representative(snapshot_a)
    assert deployability_index.is_representative(snapshot_e)
    assert deployability_index.is_representative(snapshot_f)
    assert deployability_index.is_representative(snapshot_g)
    assert not deployability_index.is_representative(snapshot_b)
    assert not deployability_index.is_representative(snapshot_c)
    assert not deployability_index.is_representative(snapshot_d)

    all_deployable_index = deployability_index.all_deployable()
    assert all(all_deployable_index.is_deployable(s) for s in snapshots.values())
    assert all(all_deployable_index.is_representative(s) for s in snapshots.values())

    none_deployable_index = deployability_index.none_deployable()
    assert all(not none_deployable_index.is_deployable(s) for s in snapshots.values())
    assert all(not none_deployable_index.is_representative(s) for s in snapshots.values())


def test_deployability_index_unpaused_forward_only(make_snapshot):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("SELECT 1")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_a.unpaused_ts = 1

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_b.parents = (snapshot_a.snapshot_id,)

    deplyability_index = DeployabilityIndex.create(
        {s.snapshot_id: s for s in [snapshot_a, snapshot_b]}
    )

    assert not deplyability_index.is_deployable(snapshot_a)
    assert deplyability_index.is_deployable(snapshot_b)

    assert deplyability_index.is_representative(snapshot_a)
    assert deplyability_index.is_representative(snapshot_b)


def test_deployability_index_unpaused_auto_restatement(make_snapshot):
    model_a = SqlModel(
        name="a",
        query=parse_one("SELECT 1, ds"),
        kind=IncrementalByTimeRangeKind(
            time_column="ds", forward_only=True, auto_restatement_cron="@weekly"
        ),
    )
    snapshot_a = make_snapshot(model_a)
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_a.unpaused_ts = 1

    # Snapshot B is a child of a model with auto restatement and is not paused,
    # so it is not deployable but is representative
    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_b.parents = (snapshot_a.snapshot_id,)
    snapshot_b.unpaused_ts = 1

    # Snapshot C is paused and hence is neither deployable nor representative
    snapshot_c = make_snapshot(SqlModel(name="c", query=parse_one("SELECT 1")))
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_c.parents = (snapshot_b.snapshot_id,)

    deplyability_index = DeployabilityIndex.create(
        {s.snapshot_id: s for s in [snapshot_a, snapshot_b, snapshot_c]}
    )

    assert not deplyability_index.is_deployable(snapshot_a)
    assert not deplyability_index.is_deployable(snapshot_b)
    assert not deplyability_index.is_deployable(snapshot_c)

    assert deplyability_index.is_representative(snapshot_a)
    assert deplyability_index.is_representative(snapshot_b)
    assert not deplyability_index.is_representative(snapshot_c)


def test_deployability_index_uncategorized_forward_only_model(make_snapshot):
    model_a = SqlModel(
        name="a",
        query=parse_one("SELECT 1, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
    )

    snapshot_a_old = make_snapshot(model_a)
    snapshot_a_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a = make_snapshot(model_a)
    snapshot_a.previous_versions = snapshot_a_old.all_versions

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.parents = (snapshot_a.snapshot_id,)

    deployability_index = DeployabilityIndex.create(
        {s.snapshot_id: s for s in [snapshot_a, snapshot_b]}
    )

    assert not deployability_index.is_deployable(snapshot_a)
    assert not deployability_index.is_deployable(snapshot_b)

    assert not deployability_index.is_representative(snapshot_a)
    assert not deployability_index.is_representative(snapshot_b)


def test_deployability_index_auto_restatement_model(make_snapshot):
    model_a = SqlModel(
        name="a",
        query=parse_one("SELECT 1, ds"),
        kind=IncrementalByTimeRangeKind(
            time_column="ds", forward_only=False, auto_restatement_cron="@weekly"
        ),
    )

    snapshot_a_old = make_snapshot(model_a)
    snapshot_a_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a = make_snapshot(model_a)
    snapshot_a.previous_versions = snapshot_a_old.all_versions

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.parents = (snapshot_a.snapshot_id,)

    deployability_index = DeployabilityIndex.create(
        {s.snapshot_id: s for s in [snapshot_a, snapshot_b]}
    )

    assert not deployability_index.is_deployable(snapshot_a)
    assert not deployability_index.is_deployable(snapshot_b)

    assert not deployability_index.is_representative(snapshot_a)
    assert not deployability_index.is_representative(snapshot_b)


def test_deployability_index_categorized_forward_only_model(make_snapshot):
    model_a = SqlModel(
        name="a",
        query=parse_one("SELECT 1, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
    )

    snapshot_a_old = make_snapshot(model_a)
    snapshot_a_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a = make_snapshot(model_a)
    snapshot_a.previous_versions = snapshot_a_old.all_versions
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.parents = (snapshot_a.snapshot_id,)
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    deployability_index = DeployabilityIndex.create(
        {s.snapshot_id: s for s in [snapshot_a, snapshot_b]}
    )

    assert not deployability_index.is_deployable(snapshot_a)
    assert not deployability_index.is_deployable(snapshot_b)

    assert not deployability_index.is_representative(snapshot_a)
    assert not deployability_index.is_representative(snapshot_b)


def test_deployability_index_missing_parent(make_snapshot):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("SELECT 1")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("SELECT 1")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_b.parents = (snapshot_a.snapshot_id,)

    deplyability_index = DeployabilityIndex.create({snapshot_b.snapshot_id: snapshot_b})

    assert not deplyability_index.is_deployable(snapshot_b)
    assert not deplyability_index.is_deployable(snapshot_a)


@pytest.mark.parametrize(
    "call_kwargs, expected",
    [
        ########################################
        # TableNamingConvention.SCHEMA_AND_TABLE
        (
            dict(physical_schema="sqlmesh__foo", name="bar", version="1234"),
            "sqlmesh__foo.bar__1234",
        ),
        (
            dict(physical_schema="sqlmesh__foo", name="foo.bar", version="1234"),
            "sqlmesh__foo.foo__bar__1234",
        ),
        (
            dict(physical_schema="sqlmesh__foo", name="bar", version="1234", catalog="foo"),
            "foo.sqlmesh__foo.bar__1234",
        ),
        (
            dict(physical_schema="sqlmesh__foo", name="bar.baz", version="1234", catalog="foo"),
            "foo.sqlmesh__foo.bar__baz__1234",
        ),
        (
            dict(physical_schema="sqlmesh__foo", name="bar.baz", version="1234", suffix="dev"),
            "sqlmesh__foo.bar__baz__1234__dev",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar.baz",
                version="1234",
                catalog="foo",
                suffix="dev",
            ),
            "foo.sqlmesh__foo.bar__baz__1234__dev",
        ),
        ##################################
        # TableNamingConvention.TABLE_ONLY
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar",
                version="1234",
                naming_convention=TableNamingConvention.TABLE_ONLY,
            ),
            "sqlmesh__foo.bar__1234",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="foo.bar",
                version="1234",
                naming_convention=TableNamingConvention.TABLE_ONLY,
            ),
            "sqlmesh__foo.bar__1234",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar",
                version="1234",
                catalog="foo",
                naming_convention=TableNamingConvention.TABLE_ONLY,
            ),
            "foo.sqlmesh__foo.bar__1234",
        ),
        (
            dict(
                physical_schema="sqlmesh__bar",
                name="bar.baz",
                version="1234",
                catalog="foo",
                naming_convention=TableNamingConvention.TABLE_ONLY,
            ),
            "foo.sqlmesh__bar.baz__1234",
        ),
        (
            dict(
                physical_schema="sqlmesh__bar",
                name="bar.baz",
                version="1234",
                suffix="dev",
                naming_convention=TableNamingConvention.TABLE_ONLY,
            ),
            "sqlmesh__bar.baz__1234__dev",
        ),
        (
            dict(
                physical_schema="sqlmesh__bar",
                name="bar.baz",
                version="1234",
                catalog="foo",
                suffix="dev",
                naming_convention=TableNamingConvention.TABLE_ONLY,
            ),
            "foo.sqlmesh__bar.baz__1234__dev",
        ),
        #################################
        # TableNamingConvention.HASH_MD5
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar",
                version="1234",
                naming_convention=TableNamingConvention.HASH_MD5,
            ),
            f"sqlmesh__foo.sqlmesh_md5__{md5('sqlmesh__foo.bar__1234')}",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="foo.bar",
                version="1234",
                naming_convention=TableNamingConvention.HASH_MD5,
            ),
            f"sqlmesh__foo.sqlmesh_md5__{md5('sqlmesh__foo.foo__bar__1234')}",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar",
                version="1234",
                catalog="foo",
                naming_convention=TableNamingConvention.HASH_MD5,
            ),
            f"foo.sqlmesh__foo.sqlmesh_md5__{md5('foo.sqlmesh__foo.bar__1234')}",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar.baz",
                version="1234",
                catalog="foo",
                naming_convention=TableNamingConvention.HASH_MD5,
            ),
            f"foo.sqlmesh__foo.sqlmesh_md5__{md5('foo.sqlmesh__foo.bar__baz__1234')}",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar.baz",
                version="1234",
                suffix="dev",
                naming_convention=TableNamingConvention.HASH_MD5,
            ),
            f"sqlmesh__foo.sqlmesh_md5__{md5('sqlmesh__foo.bar__baz__1234__dev')}__dev",
        ),
        (
            dict(
                physical_schema="sqlmesh__foo",
                name="bar.baz",
                version="1234",
                catalog="foo",
                suffix="dev",
                naming_convention=TableNamingConvention.HASH_MD5,
            ),
            f"foo.sqlmesh__foo.sqlmesh_md5__{md5('foo.sqlmesh__foo.bar__baz__1234__dev')}__dev",
        ),
    ],
)
def test_table_name(call_kwargs: t.Dict[str, t.Any], expected: str):
    """
    physical_schema: str
    name: str
    version: str
    catalog: t.Optional[str]
    suffix: t.Optional[str]
    naming_convention: t.Optional[TableNamingConvention]
    """
    assert table_name(**call_kwargs) == expected


@pytest.mark.parametrize(
    "model_name, environment_naming_info, default_catalog, dialect, expected",
    (
        (
            "test_db.test_model",
            EnvironmentNamingInfo(),
            None,
            "duckdb",
            "test_db.test_model",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(name="dev"),
            None,
            "duckdb",
            "test_db__dev.test_model",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.SCHEMA),
            None,
            "duckdb",
            "test_db__dev.test_model",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.TABLE),
            None,
            "duckdb",
            "test_db.test_model__dev",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(
                name="dev",
                suffix_target=EnvironmentSuffixTarget.TABLE,
                catalog_name_override="catalog_override",
            ),
            None,
            "duckdb",
            "catalog_override.test_db.test_model__dev",
        ),
        (
            "original_catalog.test_db.test_model",
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.TABLE),
            "default_catalog",
            "duckdb",
            "original_catalog.test_db.test_model__dev",
        ),
        (
            "original_catalog.test_db.test_model",
            EnvironmentNamingInfo(
                name="dev",
                suffix_target=EnvironmentSuffixTarget.TABLE,
                catalog_name_override="catalog_override",
            ),
            "default_catalog",
            "duckdb",
            "catalog_override.test_db.test_model__dev",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(
                name="dev",
                suffix_target=EnvironmentSuffixTarget.TABLE,
                catalog_name_override="catalog_override",
            ),
            "default_catalog",
            "duckdb",
            "catalog_override.test_db.test_model__dev",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.TABLE),
            "default_catalog",
            "duckdb",
            "test_db.test_model__dev",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(
                name="dev",
                suffix_target=EnvironmentSuffixTarget.TABLE,
                catalog_name_override="catalog_override",
            ),
            "default_catalog",
            "snowflake",
            "CATALOG_OVERRIDE.test_db.test_model__DEV",
        ),
        # EnvironmentSuffixTarget.CATALOG
        (
            "test_db.test_model",
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.CATALOG),
            "default_catalog",
            "duckdb",
            "default_catalog__dev.test_db.test_model",
        ),
        (
            "test_db.test_model",
            EnvironmentNamingInfo(name="dev", suffix_target=EnvironmentSuffixTarget.CATALOG),
            "default_catalog",
            "snowflake",
            "DEFAULT_CATALOG__DEV.test_db.test_model",
        ),
    ),
)
def test_display_name(
    make_snapshot, model_name, environment_naming_info, default_catalog, dialect, expected
):
    input_model = SqlModel(
        name=model_name,
        query=parse_one("SELECT 1, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        default_catalog=default_catalog,
    )
    input_snapshot = make_snapshot(input_model)
    assert (
        display_name(input_snapshot, environment_naming_info, default_catalog, dialect=dialect)
        == expected
    )


def test_missing_intervals_node_start_end(make_snapshot):
    input_model = SqlModel(
        name="test_model",
        query=parse_one("SELECT 1, ds"),
        kind=FullKind(),
        start="2024-03-12",
        end="2024-03-12",
    )
    snapshot = make_snapshot(input_model)
    assert missing_intervals([snapshot])[snapshot] == [
        (to_timestamp("2024-03-12"), to_timestamp("2024-03-13"))
    ]
    assert missing_intervals([snapshot], start="2024-03-12")[snapshot] == [
        (to_timestamp("2024-03-12"), to_timestamp("2024-03-13"))
    ]
    assert missing_intervals([snapshot], start="2024-03-12", end="2024-03-12")[snapshot] == [
        (to_timestamp("2024-03-12"), to_timestamp("2024-03-13"))
    ]
    assert missing_intervals([snapshot], start="2024-03-12", end="2024-03-14")[snapshot] == [
        (to_timestamp("2024-03-12"), to_timestamp("2024-03-13"))
    ]
    assert missing_intervals([snapshot], start="2024-03-01", end="2024-03-30")[snapshot] == [
        (to_timestamp("2024-03-12"), to_timestamp("2024-03-13"))
    ]
    assert missing_intervals([snapshot], start="2024-03-11", end=to_datetime("2024-03-12")) == {}
    assert missing_intervals([snapshot], start="2024-03-12", end=to_datetime("2024-03-12")) == {}
    assert missing_intervals([snapshot], start="2024-03-01", end=to_datetime("2024-03-12")) == {}
    assert missing_intervals([snapshot], start="2024-03-01", end=to_datetime("2024-03-10")) == {}
    assert missing_intervals([snapshot], start="2024-03-13", end="2024-03-14") == {}
    assert missing_intervals([snapshot], start="2024-03-14", end="2024-03-30") == {}


def test_external_model_audits(sushi_context):
    snapshot = sushi_context.get_snapshot("raw.demographics")
    assert snapshot.evaluatable
    assert len(snapshot.model.audits) == 3
    assert len(snapshot.model.audit_definitions) == 1
    assert snapshot.intervals


def test_custom_model_kind(make_snapshot):
    from sqlmesh import CustomMaterialization

    class MyCustomStrategy(CustomMaterialization):
        pass

    snapshot = make_snapshot(
        SqlModel(
            name="test_model_name",
            kind=dict(
                name="CUSTOM",
                materialization="MyCustomStrategy",
                materialization_properties=parse_one("('test_key' = 'test_value')"),
            ),
            owner="owner",
            dialect="",
            cron="@hourly",
            start="2023-01-29",
            query=parse_one("SELECT id, @end_ds as ds FROM name"),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snapshot.custom_materialization == "MyCustomStrategy"

    table_info = snapshot.table_info
    assert table_info.custom_materialization == "MyCustomStrategy"

    parsed_table_info = SnapshotTableInfo.parse_raw(table_info.json())
    assert parsed_table_info.custom_materialization == "MyCustomStrategy"


def test_ttl_ms(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model_name",
            query=parse_one("SELECT 1"),
        ),
        ttl="in 1 week",
    )
    assert snapshot.ttl_ms == 604800000


def test_snapshot_cache(make_snapshot, tmp_path):
    cache_path = tmp_path / "snapshot_cache"
    cache = SnapshotCache(cache_path)

    snapshot = make_snapshot(
        SqlModel(
            name="test_model_name",
            query=parse_one("SELECT 1"),
        )
    )
    snapshot.add_interval("2024-01-01", "2024-01-02")
    snapshot.add_interval("2024-01-01", "2024-01-02", is_dev=True)

    loader_called_times = 0

    def _loader(snapshot_ids: t.Set[SnapshotId]) -> t.Collection[Snapshot]:
        nonlocal loader_called_times
        loader_called_times += 1
        return [snapshot]

    assert cache.get_or_load([snapshot.snapshot_id], _loader) == (
        {snapshot.snapshot_id: snapshot},
        set(),
    )
    assert cache.get_or_load([snapshot.snapshot_id], _loader) == (
        {snapshot.snapshot_id: snapshot},
        {snapshot.snapshot_id},
    )
    assert cache.get_or_load([snapshot.snapshot_id], _loader) == (
        {snapshot.snapshot_id: snapshot},
        {snapshot.snapshot_id},
    )
    assert loader_called_times == 1

    cached_snapshot = cache.get_or_load([snapshot.snapshot_id], _loader)[0][snapshot.snapshot_id]
    assert cached_snapshot.model._query_renderer._optimized_cache is not None
    assert cached_snapshot.model._data_hash is not None
    assert cached_snapshot.model._metadata_hash is not None
    assert not cached_snapshot.intervals
    assert not cached_snapshot.dev_intervals

    cache.clear()
    assert cache.get_or_load([snapshot.snapshot_id], _loader) == (
        {snapshot.snapshot_id: snapshot},
        set(),
    )
    assert loader_called_times == 2


def test_snapshot_pickle_intervals(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model_name",
            query=parse_one("SELECT 1"),
        )
    )
    snapshot.add_interval("2023-01-01", "2023-01-02")
    snapshot.add_interval("2023-01-01", "2023-01-02", is_dev=True)

    assert len(snapshot.intervals) > 0
    assert len(snapshot.dev_intervals) > 0

    loaded_snapshot = pickle.loads(pickle.dumps(snapshot))
    assert not loaded_snapshot.intervals
    assert not loaded_snapshot.dev_intervals
    assert len(snapshot.intervals) > 0
    assert len(snapshot.dev_intervals) > 0


def test_missing_intervals_end_override_per_model(make_snapshot):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            start="2023-01-04",
            query=parse_one("SELECT 1"),
        ),
        version="a",
    )

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            start="2023-01-04",
            query=parse_one("SELECT 2"),
        ),
        version="b",
    )

    assert missing_intervals(
        [snapshot_a, snapshot_b],
        start="2023-01-04",
        end="2023-01-10",
        end_override_per_model={
            snapshot_a.name: to_datetime("2023-01-09"),
            snapshot_b.name: to_datetime("2023-01-06"),
        },
    ) == {
        snapshot_a: [
            (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
            (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
            (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
            (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            (to_timestamp("2023-01-08"), to_timestamp("2023-01-09")),
        ],
        snapshot_b: [
            (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
            (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        ],
    }

    assert missing_intervals(
        [snapshot_a, snapshot_b],
        start="2023-01-08",
        end="2023-01-08",
        end_override_per_model={
            snapshot_a.name: to_datetime("2023-01-09"),
            snapshot_b.name: to_datetime(
                "2023-01-06"
            ),  # The interval end is before the start. The snapshot will be skipped
        },
    ) == {
        snapshot_a: [(to_timestamp("2023-01-08"), to_timestamp("2023-01-09"))],
    }


def test_physical_version_pin(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(
                time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True
            ),
            query=parse_one("SELECT 1, ds"),
            physical_version="1234",
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.version == "1234"


def test_physical_version_pin_for_new_forward_only_models(make_snapshot):
    # A new forward-only model.
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(
                time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True
            ),
            query=parse_one("SELECT 1, ds"),
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    # Another version of the new forward-only model created independently.
    snapshot_b = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(
                time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True
            ),
            query=parse_one("SELECT 2, ds"),
        ),
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snapshot_a.fingerprint != snapshot_b.fingerprint
    assert snapshot_a.version == snapshot_b.version

    # A change to the forward-only model.
    snapshot_c = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(
                time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True
            ),
            query=parse_one("SELECT 3, ds"),
        ),
    )
    snapshot_c.previous_versions = snapshot_b.all_versions
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    assert snapshot_b.fingerprint != snapshot_c.fingerprint
    assert snapshot_b.version == snapshot_c.version

    # Make model non-forward-only.
    snapshot_d = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE),
            query=parse_one("SELECT 4, ds"),
        ),
    )
    snapshot_d.previous_versions = snapshot_c.all_versions
    snapshot_d.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snapshot_c.fingerprint != snapshot_d.fingerprint
    assert snapshot_c.version != snapshot_d.version

    # Make it forward-only again.
    snapshot_e = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(
                time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True
            ),
            query=parse_one("SELECT 5, ds"),
        ),
    )
    snapshot_e.previous_versions = snapshot_d.all_versions
    snapshot_e.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    assert snapshot_d.fingerprint != snapshot_e.fingerprint
    assert snapshot_d.version == snapshot_e.version

    # Pin the version explicitly.
    snapshot_f = make_snapshot(
        SqlModel(
            name="a",
            kind=dict(
                time_column="ds", name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True
            ),
            query=parse_one("SELECT 5, ds"),
            physical_version="1234",
        ),
    )
    snapshot_f.previous_versions = snapshot_e.all_versions
    snapshot_f.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    assert snapshot_f.version == "1234"
    assert snapshot_f.fingerprint != snapshot_e.fingerprint


def test_contiguous_intervals():
    assert _contiguous_intervals([]) == []
    assert _contiguous_intervals([(0, 1)]) == [[(0, 1)]]
    assert _contiguous_intervals([(0, 1), (1, 2), (2, 3)]) == [[(0, 1), (1, 2), (2, 3)]]
    assert _contiguous_intervals([(0, 1), (3, 4), (4, 5), (6, 7)]) == [
        [(0, 1)],
        [(3, 4), (4, 5)],
        [(6, 7)],
    ]


def test_check_ready_intervals(mocker: MockerFixture):
    def assert_always_signal(intervals):
        assert (
            check_ready_intervals(lambda _: True, intervals, mocker.Mock(), mocker.Mock())
            == intervals
        )

    assert_always_signal([])
    assert_always_signal([(0, 1)])
    assert_always_signal([(0, 1), (1, 2)])
    assert_always_signal([(0, 1), (2, 3)])

    def assert_never_signal(intervals):
        assert check_ready_intervals(lambda _: False, intervals, mocker.Mock(), mocker.Mock()) == []

    assert_never_signal([])
    assert_never_signal([(0, 1)])
    assert_never_signal([(0, 1), (1, 2)])
    assert_never_signal([(0, 1), (2, 3)])

    def assert_empty_signal(intervals):
        assert check_ready_intervals(lambda _: [], intervals, mocker.Mock(), mocker.Mock()) == []

    assert_empty_signal([])
    assert_empty_signal([(0, 1)])
    assert_empty_signal([(0, 1), (1, 2)])
    assert_empty_signal([(0, 1), (2, 3)])

    def to_intervals(values: t.List[t.Tuple[int, int]]) -> DatetimeRanges:
        return [(to_datetime(s), to_datetime(e)) for s, e in values]

    def assert_check_intervals(
        intervals: t.List[t.Tuple[int, int]],
        ready: t.List[t.List[t.Tuple[int, int]]],
        expected: t.List[t.Tuple[int, int]],
    ):
        mock = mocker.Mock()
        mock.side_effect = [to_intervals(r) for r in ready]
        check_ready_intervals(mock, intervals, mocker.Mock(), mocker.Mock()) == expected

    assert_check_intervals([], [], [])
    assert_check_intervals([(0, 1)], [[]], [])
    assert_check_intervals(
        [(0, 1)],
        [[(0, 1)]],
        [(0, 1)],
    )
    assert_check_intervals(
        [(0, 1), (1, 2)],
        [[(0, 1)]],
        [(0, 1)],
    )
    assert_check_intervals(
        [(0, 1), (1, 2)],
        [[(1, 2)]],
        [(1, 2)],
    )
    assert_check_intervals(
        [(0, 1), (1, 2)],
        [[(0, 1), (1, 2)]],
        [(0, 1), (1, 2)],
    )
    assert_check_intervals(
        [(0, 1), (1, 2), (3, 4)],
        [[], []],
        [],
    )
    assert_check_intervals(
        [(0, 1), (1, 2), (3, 4)],
        [[(0, 1)], []],
        [(0, 1)],
    )
    assert_check_intervals(
        [(0, 1), (1, 2), (3, 4)],
        [[(0, 1)], [(3, 4)]],
        [(0, 1), (3, 4)],
    )

    with pytest.raises(SignalEvalError):
        check_ready_intervals(
            lambda _: (_ for _ in ()).throw(MemoryError("Some exception")),
            [(0, 1), (1, 2)],
            mocker.Mock(),
            mocker.Mock(),
        )


@pytest.mark.parametrize(
    "auto_restatement_intervals,expected_auto_restatement_start",
    [
        (None, "2020-01-01"),
        (2, "2020-01-04"),
    ],
)
def test_get_next_auto_restatement_interval(
    make_snapshot,
    auto_restatement_intervals: t.Optional[int],
    expected_auto_restatement_start: str,
):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            kind=IncrementalByTimeRangeKind(
                time_column=TimeColumn(column="ds"),
                auto_restatement_cron="0 10 * * *",
                auto_restatement_intervals=auto_restatement_intervals,
            ),
            cron="@daily",
            query=parse_one("SELECT 1, ds FROM name"),
        )
    )
    snapshot.add_interval("2020-01-01", "2020-01-05")
    snapshot.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    assert snapshot.get_next_auto_restatement_interval(to_timestamp("2020-01-06 09:59:00")) is None

    assert snapshot.get_next_auto_restatement_interval(to_timestamp("2020-01-06 10:01:00")) == (
        to_timestamp(expected_auto_restatement_start),
        to_timestamp("2020-01-06"),
    )


def test_apply_auto_restatements(make_snapshot):
    # Hourly upstream model with auto restatement intervals set to 24
    model_a = SqlModel(
        name="test_model_a",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=24,
        ),
        cron="@hourly",
        query=parse_one("SELECT 1, ds FROM name"),
    )
    snapshot_a = make_snapshot(model_a, version="1")
    snapshot_a.add_interval("2020-01-01", "2020-01-06 09:00:00")
    snapshot_a.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    # Daily downstream model with no auto restatement
    model_b = SqlModel(
        name="test_model_b",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
        ),
        cron="@daily",
        query=parse_one("SELECT ds FROM test_model_a"),
    )
    snapshot_b = make_snapshot(model_b, nodes={model_a.fqn: model_a}, version="2")
    snapshot_b.add_interval("2020-01-01", "2020-01-05")
    assert snapshot_a.snapshot_id in snapshot_b.parents

    # Daily downstream model with auto restatement intervals of 2
    model_c = SqlModel(
        name="test_model_c",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=2,
        ),
        cron="@daily",
        query=parse_one("SELECT ds FROM test_model_a"),
    )
    snapshot_c = make_snapshot(model_c, nodes={model_a.fqn: model_a}, version="3")
    snapshot_c.add_interval("2020-01-01", "2020-01-05")
    snapshot_c.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")
    assert snapshot_a.snapshot_id in snapshot_c.parents

    # Hourly downstream model with auto restatement intervals of 5
    model_d = SqlModel(
        name="test_model_d",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=5,
        ),
        cron="@hourly",
        query=parse_one("SELECT ds FROM test_model_a"),
    )
    snapshot_d = make_snapshot(model_d, nodes={model_a.fqn: model_a}, version="4")
    snapshot_d.add_interval("2020-01-01", "2020-01-06 09:00:00")
    snapshot_d.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")
    assert snapshot_a.snapshot_id in snapshot_d.parents

    # Hourly upstream model with auto restatement intervals set to 5
    model_e = SqlModel(
        name="test_model_e",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=5,
        ),
        cron="@hourly",
        query=parse_one("SELECT 1, ds FROM name"),
    )
    snapshot_e = make_snapshot(model_e, version="5")
    snapshot_e.add_interval("2020-01-01", "2020-01-06 09:00:00")
    snapshot_e.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    # Daily model downstream from model_e without auto restatement that should not be impacted by auto restatement
    # upstream.
    model_f = SqlModel(
        name="test_model_f",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
        ),
        cron="@daily",
        query=parse_one("SELECT ds FROM test_model_e"),
    )
    snapshot_f = make_snapshot(model_f, nodes={model_e.fqn: model_e}, version="6")
    snapshot_f.add_interval("2020-01-01", "2020-01-05")
    assert snapshot_e.snapshot_id in snapshot_f.parents

    assert snapshot_a.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06 09:00:00")),
    ]
    assert snapshot_b.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
    ]
    assert snapshot_c.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
    ]
    assert snapshot_d.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06 09:00:00")),
    ]
    assert snapshot_e.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06 09:00:00")),
    ]
    assert snapshot_f.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
    ]

    restated_intervals, _ = apply_auto_restatements(
        {
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
            snapshot_c.snapshot_id: snapshot_c,
            snapshot_d.snapshot_id: snapshot_d,
            snapshot_e.snapshot_id: snapshot_e,
            snapshot_f.snapshot_id: snapshot_f,
        },
        "2020-01-06 10:01:00",
    )
    assert sorted(restated_intervals, key=lambda x: x.name) == [
        SnapshotIntervals(
            name=snapshot_a.name,
            identifier=None,
            version=snapshot_a.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-05 10:00:00"), to_timestamp("2020-01-06 10:00:00"))
            ],
        ),
        SnapshotIntervals(
            name=snapshot_b.name,
            identifier=None,
            version=snapshot_b.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-05"), to_timestamp("2020-01-07"))
            ],
        ),
        SnapshotIntervals(
            name=snapshot_c.name,
            identifier=None,
            version=snapshot_c.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-04"), to_timestamp("2020-01-07"))
            ],
        ),
        SnapshotIntervals(
            name=snapshot_d.name,
            identifier=None,
            version=snapshot_d.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-05 10:00:00"), to_timestamp("2020-01-06 10:00:00"))
            ],
        ),
        SnapshotIntervals(
            name=snapshot_e.name,
            identifier=None,
            version=snapshot_e.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-06 05:00:00"), to_timestamp("2020-01-06 10:00:00"))
            ],
        ),
        SnapshotIntervals(
            name=snapshot_f.name,
            identifier=None,
            version=snapshot_f.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-06"), to_timestamp("2020-01-07"))
            ],
        ),
    ]

    assert snapshot_a.next_auto_restatement_ts == to_timestamp("2020-01-07 10:00:00")
    assert snapshot_b.next_auto_restatement_ts is None
    assert snapshot_c.next_auto_restatement_ts == to_timestamp("2020-01-07 10:00:00")
    assert snapshot_d.next_auto_restatement_ts == to_timestamp("2020-01-07 10:00:00")
    assert snapshot_e.next_auto_restatement_ts == to_timestamp("2020-01-07 10:00:00")
    assert snapshot_f.next_auto_restatement_ts is None

    assert snapshot_a.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-05 10:00:00")),
    ]
    assert snapshot_b.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-05")),
    ]
    assert snapshot_c.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-04")),
    ]
    assert snapshot_d.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-05 10:00:00")),
    ]
    assert snapshot_e.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06 05:00:00")),
    ]
    assert snapshot_f.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
    ]


def test_apply_auto_restatements_disable_restatement_downstream(make_snapshot):
    # Hourly upstream model with auto restatement intervals set to 24
    model_a = SqlModel(
        name="test_model_a",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=24,
        ),
        cron="@hourly",
        query=parse_one("SELECT 1, ds FROM name"),
    )
    snapshot_a = make_snapshot(model_a, version="1")
    snapshot_a.add_interval("2020-01-01", "2020-01-06 09:00:00")
    snapshot_a.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    # Daily downstream model with disable restatement
    model_b = SqlModel(
        name="test_model_b",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            disable_restatement=True,
        ),
        cron="@daily",
        query=parse_one("SELECT ds FROM test_model_a"),
    )
    snapshot_b = make_snapshot(model_b, nodes={model_a.fqn: model_a}, version="2")
    snapshot_b.add_interval("2020-01-01", "2020-01-05")
    assert snapshot_a.snapshot_id in snapshot_b.parents

    restated_intervals, _ = apply_auto_restatements(
        {
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        "2020-01-06 10:01:00",
    )
    assert sorted(restated_intervals, key=lambda x: x.name) == [
        SnapshotIntervals(
            name=snapshot_a.name,
            identifier=None,
            version=snapshot_a.version,
            dev_version=None,
            intervals=[],
            dev_intervals=[],
            pending_restatement_intervals=[
                (to_timestamp("2020-01-05 10:00:00"), to_timestamp("2020-01-06 10:00:00"))
            ],
        ),
    ]

    assert snapshot_a.next_auto_restatement_ts == to_timestamp("2020-01-07 10:00:00")
    assert snapshot_b.next_auto_restatement_ts is None

    assert snapshot_a.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-05 10:00:00")),
    ]
    assert snapshot_b.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
    ]

    snapshot_b.pending_restatement_intervals = [
        (to_timestamp("2020-01-03"), to_timestamp("2020-01-06"))
    ]
    snapshot_b.apply_pending_restatement_intervals()
    assert snapshot_b.intervals == [
        (to_timestamp("2020-01-01"), to_timestamp("2020-01-06")),
    ]


def test_auto_restatement_triggers(make_snapshot):
    # Auto restatements:
    #   a, c, d
    # dag:
    #   a -> b
    #   a -> c
    #   [b, c, d] -> e
    model_a = SqlModel(
        name="test_model_a",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=24,
        ),
        start="2020-01-01",
        cron="@daily",
        query=parse_one("SELECT 1 as ds"),
    )
    snapshot_a = make_snapshot(model_a, version="1")
    snapshot_a.add_interval("2020-01-01", "2020-01-05")
    snapshot_a.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    model_b = SqlModel(
        name="test_model_b",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
        ),
        start="2020-01-01",
        cron="@daily",
        query=parse_one("SELECT ds FROM test_model_a"),
    )
    snapshot_b = make_snapshot(model_b, nodes={model_a.fqn: model_a}, version="1")
    snapshot_b.add_interval("2020-01-01", "2020-01-05")

    model_c = SqlModel(
        name="test_model_c",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=24,
        ),
        start="2020-01-01",
        cron="@daily",
        query=parse_one("SELECT ds FROM test_model_a"),
    )
    snapshot_c = make_snapshot(model_c, nodes={model_a.fqn: model_a}, version="1")
    snapshot_c.add_interval("2020-01-01", "2020-01-05")
    snapshot_c.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    model_d = SqlModel(
        name="test_model_d",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
            auto_restatement_cron="0 10 * * *",
            auto_restatement_intervals=24,
        ),
        start="2020-01-01",
        cron="@daily",
        query=parse_one("SELECT 1 as ds"),
    )
    snapshot_d = make_snapshot(model_d, version="1")
    snapshot_d.add_interval("2020-01-01", "2020-01-05")
    snapshot_d.next_auto_restatement_ts = to_timestamp("2020-01-06 10:00:00")

    model_e = SqlModel(
        name="test_model_e",
        kind=IncrementalByTimeRangeKind(
            time_column=TimeColumn(column="ds"),
        ),
        start="2020-01-01",
        cron="@daily",
        query=parse_one(
            "SELECT ds from test_model_b UNION ALL SELECT ds from test_model_c UNION ALL SELECT ds from test_model_d"
        ),
    )
    snapshot_e = make_snapshot(
        model_e,
        nodes={
            model_a.fqn: model_a,
            model_b.fqn: model_b,
            model_c.fqn: model_c,
            model_d.fqn: model_d,
        },
        version="1",
    )
    snapshot_e.add_interval("2020-01-01", "2020-01-05")

    _, auto_restatement_triggers = apply_auto_restatements(
        {
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
            snapshot_c.snapshot_id: snapshot_c,
            snapshot_d.snapshot_id: snapshot_d,
            snapshot_e.snapshot_id: snapshot_e,
        },
        "2020-01-06 10:01:00",
    )

    assert auto_restatement_triggers[snapshot_a.snapshot_id] == [snapshot_a.snapshot_id]
    assert auto_restatement_triggers[snapshot_c.snapshot_id] == [snapshot_c.snapshot_id]
    assert auto_restatement_triggers[snapshot_d.snapshot_id] == [snapshot_d.snapshot_id]
    assert auto_restatement_triggers[snapshot_b.snapshot_id] == [snapshot_a.snapshot_id]
    # a via b, c and d directly
    assert sorted(auto_restatement_triggers[snapshot_e.snapshot_id]) == [
        snapshot_a.snapshot_id,
        snapshot_c.snapshot_id,
        snapshot_d.snapshot_id,
    ]


def test_render_signal(make_snapshot, mocker):
    @signal()
    def check_types(batch, env: str, sql: list[SQL], table: exp.Table, default: int = 0):
        if not (
            env == "in_memory"
            and default == 0
            and isinstance(sql, list)
            and isinstance(sql[0], str)
            and isinstance(table, exp.Table)
        ):
            raise
        return True

    sql_model = load_sql_based_model(
        parse(
            """
        MODEL (
            name test_schema.test_model,
            signals check_types(env := @gateway, sql := [a.b], table := b.c)
        );
        SELECT a FROM tbl;
        """
        ),
        variables={
            c.GATEWAY: "in_memory",
        },
        signal_definitions=signal.get_registry(),
    )
    snapshot_a = make_snapshot(sql_model)
    assert snapshot_a.check_ready_intervals([(0, 1)], mocker.Mock()) == [(0, 1)]


def test_partitioned_by_roundtrip(make_snapshot: t.Callable):
    sql_model = load_sql_based_model(
        parse("""
        MODEL (
            name test_schema.test_model,
            kind full,
            partitioned_by (a, bucket(4, b), truncate(3, c), month(d))
        );
        SELECT a, b, c, d FROM tbl;
        """)
    )
    snapshot = make_snapshot(sql_model)
    assert isinstance(snapshot, Snapshot)
    assert isinstance(snapshot.node, SqlModel)

    assert snapshot.node.partitioned_by == [
        exp.column("a", quoted=True),
        exp.PartitionedByBucket(
            this=exp.column("b", quoted=True), expression=exp.Literal.number(4)
        ),
        exp.PartitionByTruncate(
            this=exp.column("c", quoted=True), expression=exp.Literal.number(3)
        ),
        exp.Month(this=exp.column("d", quoted=True)),
    ]

    # roundtrip through json and ensure we get correct AST nodes on the other end
    serialized = snapshot.json()
    deserialized = snapshot.parse_raw(serialized)

    assert isinstance(deserialized.node, SqlModel)
    assert deserialized.node.partitioned_by == snapshot.node.partitioned_by


@pytest.mark.parametrize(
    "virtual_env_mode,is_deployable,expected_uses_name_as_is",
    [
        (VirtualEnvironmentMode.DEV_ONLY, True, True),
        (VirtualEnvironmentMode.DEV_ONLY, False, False),
        (VirtualEnvironmentMode.FULL, True, False),
        (VirtualEnvironmentMode.FULL, False, False),
    ],
)
def test_table_name_virtual_environment_mode(
    make_snapshot,
    virtual_env_mode: VirtualEnvironmentMode,
    is_deployable: bool,
    expected_uses_name_as_is: bool,
):
    model = SqlModel(
        name="my_schema.my_model",
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        query=parse_one("SELECT 1, ds"),
        virtual_environment_mode=virtual_env_mode,
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    table_name_result = snapshot.table_name(is_deployable=is_deployable)

    if expected_uses_name_as_is:
        assert table_name_result == '"my_schema"."my_model"'
    else:
        # Should contain the versioned table name with schema prefix
        assert "sqlmesh__my_schema" in table_name_result
        assert "my_schema__my_model" in table_name_result
        if is_deployable:
            assert table_name_result.endswith(snapshot.version)
        else:
            assert table_name_result.endswith(f"{snapshot.dev_version}__dev")


def test_snapshot_id_and_version_fingerprint_lazy_init():
    snapshot = SnapshotIdAndVersion(
        name="a",
        identifier="1234",
        version="2345",
        dev_version=None,
        fingerprint='{"data_hash":"1","metadata_hash":"2","parent_data_hash":"3","parent_metadata_hash":"4"}',
    )

    # starts off as a string in the private property
    assert isinstance(snapshot.fingerprint_, str)

    # gets parsed into SnapshotFingerprint on first access of public property
    fingerprint = snapshot.fingerprint
    assert isinstance(fingerprint, SnapshotFingerprint)
    assert isinstance(snapshot.fingerprint_, SnapshotFingerprint)

    assert fingerprint.data_hash == "1"
    assert fingerprint.metadata_hash == "2"
    assert fingerprint.parent_data_hash == "3"
    assert fingerprint.parent_metadata_hash == "4"
    assert snapshot.dev_version is not None  # dev version uses fingerprint

    # can also be supplied as a SnapshotFingerprint to begin with instead of a str
    snapshot = SnapshotIdAndVersion(
        name="a", identifier="1234", version="2345", dev_version=None, fingerprint=fingerprint
    )

    assert isinstance(snapshot.fingerprint_, SnapshotFingerprint)
    assert snapshot.fingerprint == fingerprint


def test_snapshot_id_and_version_optional_kind_name():
    snapshot = SnapshotIdAndVersion(
        name="a",
        identifier="1234",
        version="2345",
        dev_version=None,
        fingerprint="",
    )

    assert snapshot.model_kind_name is None

    snapshot = SnapshotIdAndVersion(
        name="a",
        identifier="1234",
        version="2345",
        kind_name="INCREMENTAL_UNMANAGED",
        dev_version=None,
        fingerprint="",
    )

    assert snapshot.model_kind_name
    assert snapshot.is_incremental_unmanaged
    assert snapshot.full_history_restatement_only
