from __future__ import annotations
import typing as t

from typing_extensions import Self
from unittest.mock import call, patch, Mock
import contextlib
import re
import logging
import pytest
import pandas as pd  # noqa: TID253
import json
from pydantic import model_validator
from pathlib import Path
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse, parse_one, select

from sqlmesh.core.audit import ModelAudit, StandaloneAudit
from sqlmesh.core import dialect as d
from sqlmesh.core.dialect import schema_, to_schema
from sqlmesh.core.engine_adapter import EngineAdapter, create_engine_adapter, BigQueryEngineAdapter
from sqlmesh.core.engine_adapter.base import MERGE_SOURCE_ALIAS, MERGE_TARGET_ALIAS
from sqlmesh.core.engine_adapter.shared import (
    DataObject,
    DataObjectType,
    InsertOverwriteStrategy,
)
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.macros import RuntimeStage, macro, MacroEvaluator, MacroFunc
from sqlmesh.core.model import (
    Model,
    FullKind,
    IncrementalByTimeRangeKind,
    IncrementalUnmanagedKind,
    IncrementalByPartitionKind,
    IncrementalByUniqueKeyKind,
    PythonModel,
    SqlModel,
    TimeColumn,
    ViewKind,
    CustomKind,
    load_sql_based_model,
    ExternalModel,
    model,
    create_sql_model,
)
from sqlmesh.core.model.kind import OnDestructiveChange, ExternalKind, OnAdditiveChange
from sqlmesh.core.model.meta import GrantsTargetLayer
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Intervals,
    Snapshot,
    SnapshotDataVersion,
    SnapshotFingerprint,
    SnapshotChangeCategory,
    SnapshotEvaluator,
    SnapshotTableCleanupTask,
)
from sqlmesh.core.snapshot.definition import to_view_mapping
from sqlmesh.core.snapshot.evaluator import (
    CustomMaterialization,
    EngineManagedStrategy,
    FullRefreshStrategy,
    IncrementalByPartitionStrategy,
    IncrementalByTimeRangeStrategy,
    IncrementalByUniqueKeyStrategy,
    IncrementalUnmanagedStrategy,
    MaterializableStrategy,
    SCDType2Strategy,
    SnapshotCreationFailedError,
    ViewStrategy,
)
from sqlmesh.utils.concurrency import NodeExecutionFailedError
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import (
    ConfigError,
    SQLMeshError,
    DestructiveChangeError,
    AdditiveChangeError,
)
from sqlmesh.utils.metaprogramming import Executable
from sqlmesh.utils.pydantic import list_of_fields_validator


if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter._typing import QueryOrDF


@pytest.fixture
def snapshot(duck_conn, make_snapshot) -> Snapshot:
    duck_conn.execute("CREATE VIEW tbl AS SELECT 1 AS a")

    model = SqlModel(
        name="db.model",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    return snapshot


@pytest.fixture
def date_kwargs() -> t.Dict[str, str]:
    return {
        "start": "2020-01-01",
        "end": "2020-01-01",
        "execution_time": "2020-01-01",
    }


@pytest.fixture
def adapter_mock(mocker: MockerFixture):
    def mock_exit(self, exc_type, exc_value, traceback):
        pass

    transaction_mock = mocker.Mock()
    transaction_mock.__enter__ = mocker.Mock()
    transaction_mock.__exit__ = mock_exit

    session_mock = mocker.Mock()
    session_mock.__enter__ = mocker.Mock()
    session_mock.__exit__ = mock_exit

    adapter_mock = mocker.Mock()
    adapter_mock.transaction.return_value = transaction_mock
    adapter_mock.session.return_value = session_mock
    adapter_mock.dialect = "duckdb"
    adapter_mock.HAS_VIEW_BINDING = False
    adapter_mock.wap_supported.return_value = False
    adapter_mock.get_data_objects.return_value = []
    adapter_mock.with_settings.return_value = adapter_mock
    return adapter_mock


@pytest.fixture
def adapters(mocker: MockerFixture):
    adapters = []
    for i in range(3):
        transaction_mock = mocker.Mock()
        transaction_mock.__enter__ = mocker.Mock()
        transaction_mock.__exit__ = mocker.Mock()

        session_mock = mocker.Mock()
        session_mock.__enter__ = mocker.Mock()
        session_mock.__exit__ = mocker.Mock()

        adapter_mock = mocker.Mock()
        adapter_mock.transaction.return_value = transaction_mock
        adapter_mock.session.return_value = session_mock
        adapter_mock.dialect = "duckdb"
        adapter_mock.HAS_VIEW_BINDING = False
        adapter_mock.wap_supported.return_value = False
        adapter_mock.get_data_objects.return_value = []
        adapter_mock.with_settings.return_value = adapter_mock
        adapters.append(adapter_mock)
    return adapters


def test_evaluate(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    payload = {"calls": 0}

    @macro()
    def x(evaluator, y=None) -> None:
        if "payload" not in evaluator.locals:
            return

        evaluator.locals["payload"]["calls"] += 1

        if y is not None:
            evaluator.locals["payload"]["y"] = y

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (time_column a),
                storage_format 'parquet',
            );

            @x();

            @DEF(a, 1);

            CREATE TABLE hook_called;

            SELECT a::int FROM tbl WHERE ds BETWEEN @start_ds and @end_ds;

            @x();

            @DEF(b, 2);

            @x(['a', 2, TRUE]);
            """
        ),
        macros=macro.get_registry(),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {})
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
        payload=payload,
    )

    assert payload["calls"] == 3
    assert payload["y"] == exp.convert(["a", 2, True])

    execute_calls = [call([parse_one('CREATE TABLE "hook_called"')])]
    adapter_mock.execute.assert_has_calls(execute_calls)

    common_kwargs = dict(
        target_columns_to_types={"a": exp.DataType.build("int")},
        table_format=None,
        storage_format="parquet",
        partitioned_by=[exp.to_column("a", quoted=True)],
        partition_interval_unit=IntervalUnit.DAY,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    # Create will be called once and only prod table will be created
    adapter_mock.create_table.assert_called_once_with(
        snapshot.table_name(),
        column_descriptions={},
        **common_kwargs,
    )


def test_runtime_stages(capsys, mocker, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    @macro()
    def increment_stage_counter(evaluator) -> None:
        # Hack which allows us to intercept the different runtime stage values
        print(f"RuntimeStage value: {evaluator.locals['runtime_stage']}")

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
            );

            @increment_stage_counter();
            @if(@runtime_stage = 'evaluating', ALTER TABLE test_schema.foo MODIFY COLUMN c SET MASKING POLICY p);

            SELECT 1 AS a, @runtime_stage AS b;
            """
        ),
        macros=macro.get_registry(),
    )

    capsys.readouterr()

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot.model.render_pre_statements()

    assert f"RuntimeStage value: {RuntimeStage.LOADING.value}" in capsys.readouterr().out

    evaluator.create([snapshot], {})
    assert f"RuntimeStage value: {RuntimeStage.CREATING.value}" in capsys.readouterr().out

    evaluator.evaluate(
        snapshot, start="2020-01-01", end="2020-01-02", execution_time="2020-01-02", snapshots={}
    )
    assert f"RuntimeStage value: {RuntimeStage.EVALUATING.value}" in capsys.readouterr().out

    empty_call = call([])
    non_empty_calls = [c for c in adapter_mock.execute.mock_calls if c != empty_call]
    assert len(non_empty_calls) == 1
    assert non_empty_calls[0] == call(
        [parse_one("ALTER TABLE test_schema.foo MODIFY COLUMN c SET MASKING POLICY p")]
    )

    assert snapshot.model.render_query().sql() == '''SELECT 1 AS "a", 'loading' AS "b"'''
    assert (
        snapshot.model.render_query(runtime_stage=RuntimeStage.CREATING).sql()
        == '''SELECT 1 AS "a", 'creating' AS "b"'''
    )
    assert (
        snapshot.model.render_query(runtime_stage=RuntimeStage.EVALUATING).sql()
        == '''SELECT 1 AS "a", 'evaluating' AS "b"'''
    )


def test_promote(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a"),
        storage_format="parquet",
        query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.transaction.assert_called()
    adapter_mock.session.assert_called()
    adapter_mock.create_schema.assert_called_once_with(to_schema("test_schema__test_env"))
    adapter_mock.create_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        parse_one(
            f"SELECT * FROM sqlmesh__test_schema.test_schema__test_model__{snapshot.version}"
        ),
        table_description=None,
        column_descriptions=None,
        view_properties={},
    )


def test_demote(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_model",
        kind=ViewKind(),
        query=parse_one("SELECT a FROM tbl"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.demote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.transaction.assert_called()
    adapter_mock.session.assert_called()
    adapter_mock.drop_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        cascade=False,
    )


def test_promote_default_catalog(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a"),
        storage_format="parquet",
        query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
        default_catalog="test_catalog",
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.create_schema.assert_called_once_with(
        schema_("test_schema__test_env", "test_catalog")
    )
    adapter_mock.create_view.assert_called_once_with(
        "test_catalog.test_schema__test_env.test_model",
        parse_one(
            f"SELECT * FROM test_catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.version}"
        ),
        table_description=None,
        column_descriptions=None,
        view_properties={},
    )


def test_promote_forward_only(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a"),
        query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.version = "test_version"

    evaluator.promote(
        [snapshot],
        EnvironmentNamingInfo(name="test_env"),
        deployability_index=DeployabilityIndex.none_deployable(),
    )

    snapshot.unpaused_ts = to_timestamp("2023-01-01")
    evaluator.promote(
        [snapshot],
        EnvironmentNamingInfo(name="test_env"),
        deployability_index=DeployabilityIndex(indexed_ids=[snapshot.snapshot_id]),
    )

    adapter_mock.create_schema.assert_has_calls(
        [
            call(to_schema("test_schema__test_env")),
            call(to_schema("test_schema__test_env")),
        ]
    )
    adapter_mock.create_view.assert_has_calls(
        [
            call(
                "test_schema__test_env.test_model",
                parse_one(
                    f"SELECT * FROM sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev"
                ),
                table_description=None,
                column_descriptions=None,
                view_properties={},
            ),
            call(
                "test_schema__test_env.test_model",
                parse_one(
                    "SELECT * FROM sqlmesh__test_schema.test_schema__test_model__test_version"
                ),
                table_description=None,
                column_descriptions=None,
                view_properties={},
            ),
        ]
    )


def test_cleanup(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    def create_and_cleanup(name: str, dev_table_only: bool):
        model = SqlModel(
            name=name,
            kind=IncrementalByTimeRangeKind(time_column="a"),
            storage_format="parquet",
            query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
        )

        snapshot = make_snapshot(model)
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
        snapshot.version = "test_version"

        on_cleanup_mock = mocker.Mock()

        evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))
        evaluator.cleanup(
            [SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=dev_table_only)],
            on_complete=on_cleanup_mock,
        )
        assert on_cleanup_mock.call_count == 1 if dev_table_only else 2
        return snapshot

    snapshot = create_and_cleanup("catalog.test_schema.test_model", True)
    adapter_mock.get_data_object.assert_not_called()
    adapter_mock.drop_table.assert_called_once_with(
        f"catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev",
        cascade=True,
    )
    adapter_mock.reset_mock()

    snapshot = create_and_cleanup("test_schema.test_model", False)
    adapter_mock.get_data_object.assert_not_called()
    adapter_mock.drop_table.assert_has_calls(
        [
            call(
                f"sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev",
                cascade=True,
            ),
            call(f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}", cascade=True),
        ]
    )
    adapter_mock.reset_mock()

    snapshot = create_and_cleanup("test_model", False)
    adapter_mock.get_data_object.assert_not_called()
    adapter_mock.drop_table.assert_has_calls(
        [
            call(
                f"sqlmesh__default.test_model__{snapshot.fingerprint.to_version()}__dev",
                cascade=True,
            ),
            call(f"sqlmesh__default.test_model__{snapshot.version}", cascade=True),
        ]
    )


def test_cleanup_view(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="catalog.test_schema.test_model",
        kind=ViewKind(materialized=False),
        query=parse_one("SELECT a FROM tbl"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))
    evaluator.cleanup([SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True)])

    adapter_mock.get_data_object.assert_not_called()
    adapter_mock.drop_view.assert_called_once_with(
        f"catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev",
        cascade=True,
        ignore_if_not_exists=False,
    )


def test_cleanup_materialized_view(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="catalog.test_schema.test_model",
        kind=ViewKind(materialized=True),
        query=parse_one("SELECT a FROM tbl"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    adapter_mock.drop_view.side_effect = [RuntimeError("failed to drop view"), None]

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))
    evaluator.cleanup([SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True)])

    adapter_mock.get_data_object.assert_not_called()
    adapter_mock.drop_view.assert_has_calls(
        [
            call(
                f"catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev",
                cascade=True,
                ignore_if_not_exists=False,
            ),
            call(
                f"catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev",
                materialized=True,
                cascade=True,
                ignore_if_not_exists=True,
            ),
        ]
    )


def test_cleanup_fails(adapter_mock, make_snapshot):
    adapter_mock.drop_table.side_effect = RuntimeError("test_error")

    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="catalog.test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a"),
        storage_format="parquet",
        query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.version = "test_version"

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))
    with pytest.raises(SQLMeshError) as exc_info:
        evaluator.cleanup(
            [SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True)]
        )

    assert "test_error" in str(exc_info.value)


def test_cleanup_skip_missing_table(adapter_mock, make_snapshot):
    adapter_mock.get_data_object.return_value = None
    adapter_mock.drop_table.side_effect = RuntimeError("fail")

    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="catalog.test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a"),
        storage_format="parquet",
        query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.version = "test_version"

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))
    evaluator.cleanup([SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True)])

    adapter_mock.get_data_object.assert_called_once_with(
        f"catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev"
    )
    adapter_mock.drop_table.assert_called_once_with(
        f"catalog.sqlmesh__test_schema.test_schema__test_model__{snapshot.fingerprint.to_version()}__dev",
        cascade=True,
    )


def test_cleanup_external_model(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    def create_and_cleanup_external_model(name: str, dev_table_only: bool):
        model = ExternalModel(
            name=name,
            kind=ExternalKind(),
        )

        snapshot = make_snapshot(model)
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
        snapshot.version = "test_version"

        evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))
        evaluator.cleanup(
            [SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=dev_table_only)]
        )
        return snapshot

    create_and_cleanup_external_model("catalog.test_schema.test_model", True)
    adapter_mock.drop_table.assert_not_called()


def test_cleanup_symbolic_and_audit_snapshots_no_callback(
    mocker: MockerFixture, adapter_mock, make_snapshot
):
    evaluator = SnapshotEvaluator(adapter_mock)
    on_complete_mock = mocker.Mock()

    # Test external model
    external_model = ExternalModel(
        name="test_schema.external_model",
        kind=ExternalKind(),
    )
    external_snapshot = make_snapshot(external_model)
    external_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # Test standalone audit
    audit = StandaloneAudit(name="test_audit", query=parse_one("SELECT NULL LIMIT 0"))
    audit_snapshot = make_snapshot(audit)
    audit_snapshot.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    evaluator.cleanup(
        [
            SnapshotTableCleanupTask(snapshot=external_snapshot.table_info, dev_table_only=False),
            SnapshotTableCleanupTask(snapshot=audit_snapshot.table_info, dev_table_only=False),
        ],
        on_complete=on_complete_mock,
    )

    # Verify that no physical tables were attempted to be dropped
    adapter_mock.drop_table.assert_not_called()
    adapter_mock.get_data_object.assert_not_called()
    on_complete_mock.assert_not_called()


@pytest.mark.parametrize("view_exists", [True, False])
def test_evaluate_materialized_view(
    mocker: MockerFixture, adapter_mock, make_snapshot, view_exists: bool
):
    adapter_mock.table_exists.return_value = view_exists
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW (
                    materialized true
                )
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.add_interval("2023-01-01", "2023-01-01")

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    # Ensure that the materialized view is recreated even if it exists
    assert adapter_mock.create_view.call_count == 1


def test_evaluate_materialized_view_with_partitioned_by_cluster_by(
    mocker: MockerFixture, adapter_mock, make_snapshot
):
    execute_mock = mocker.Mock()
    # Use an engine adapter that supports cluster by/partitioned by
    adapter = BigQueryEngineAdapter(lambda: mocker.Mock())
    adapter.table_exists = lambda *args, **kwargs: False  # type: ignore
    adapter.get_data_objects = lambda *args, **kwargs: []  # type: ignore
    adapter._execute = execute_mock  # type: ignore
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    evaluator = SnapshotEvaluator(adapter)

    model = SqlModel(
        name="test_schema.test_model",
        kind=ViewKind(
            materialized=True,
        ),
        partitioned_by=[exp.to_column("a")],
        clustered_by=[exp.to_column("b")],
        query=parse_one("SELECT a, b FROM tbl"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.add_interval("2023-01-01", "2023-01-01")

    evaluator.create(
        [snapshot],
        snapshots={},
    )

    execute_mock.assert_has_calls(
        [
            call(
                f"CREATE MATERIALIZED VIEW `sqlmesh__test_schema`.`test_schema__test_model__{snapshot.version}` PARTITION BY `a` CLUSTER BY `b` AS SELECT `a` AS `a`, `b` AS `b` FROM `tbl` AS `tbl`",
                False,
            ),
        ]
    )


def test_evaluate_materialized_view_with_execution_time_macro(
    mocker: MockerFixture, adapter_mock, make_snapshot
):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW (
                    materialized true
                )
            );

            SELECT a::int FROM tbl WHERE ds < @execution_ds;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.create_view.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(execution_time="2020-01-02"),
        model.columns_to_types,
        replace=True,
        materialized=True,
        view_properties={},
        table_description=None,
        column_descriptions={},
    )


@pytest.mark.parametrize("insert_overwrite", [False, True])
def test_evaluate_incremental_unmanaged_with_intervals(
    mocker: MockerFixture, make_snapshot, adapter_mock, insert_overwrite
):
    model = SqlModel(
        name="test_schema.test_model",
        query=parse_one("SELECT 1, ds FROM tbl_a"),
        kind=IncrementalUnmanagedKind(insert_overwrite=insert_overwrite),
        partitioned_by=["ds"],
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    adapter_mock.columns.return_value = model.columns_to_types

    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    if insert_overwrite:
        adapter_mock.insert_overwrite_by_partition.assert_called_once_with(
            snapshot.table_name(),
            model.render_query(),
            [exp.to_column("ds", quoted=True)],
            target_columns_to_types=model.columns_to_types,
            source_columns=None,
        )
    else:
        adapter_mock.insert_append.assert_called_once_with(
            snapshot.table_name(),
            model.render_query(),
            target_columns_to_types=model.columns_to_types,
            source_columns=None,
        )


@pytest.mark.parametrize("insert_overwrite", [False, True])
def test_evaluate_incremental_unmanaged_no_intervals(
    mocker: MockerFixture, make_snapshot, adapter_mock, insert_overwrite
):
    model = SqlModel(
        name="test_schema.test_model",
        query=parse_one("SELECT 1 as one, ds FROM tbl_a"),
        kind=IncrementalUnmanagedKind(insert_overwrite=insert_overwrite),
        partitioned_by=["ds"],
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    table_columns = {"one": exp.DataType.build("int"), "ds": exp.DataType.build("timestamp")}
    adapter_mock.columns.return_value = table_columns

    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.replace_query.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        clustered_by=[],
        column_descriptions={},
        target_columns_to_types=table_columns,
        partition_interval_unit=model.partition_interval_unit,
        partitioned_by=model.partitioned_by,
        table_format=None,
        storage_format=None,
        table_description=None,
        table_properties={},
        source_columns=None,
    )
    adapter_mock.columns.assert_called_once_with(snapshot.table_name())


def test_create_prod_table_exists(mocker: MockerFixture, adapter_mock, make_snapshot):
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            name=f"test_schema__test_model__{snapshot.version}",
            schema="sqlmesh__test_schema",
            type=DataObjectType.VIEW,
        ),
    ]
    evaluator = SnapshotEvaluator(adapter_mock)

    evaluator.create([snapshot], {})
    adapter_mock.create_view.assert_not_called()
    adapter_mock.create_schema.assert_not_called()
    adapter_mock.get_data_objects.assert_called_once_with(
        schema_("sqlmesh__test_schema"),
        {
            f"test_schema__test_model__{snapshot.version}",
        },
        safe_to_cache=True,
    )


def test_pre_hook_forward_only_clone(
    mocker: MockerFixture, make_mocked_engine_adapter, make_snapshot
):
    """
    Verifies that pre-statements are executed when creating a clone of a forward-only model.
    """
    pre_statement = """CREATE TEMPORARY FUNCTION "example_udf"("x" BIGINT) AS ("x" + 1)"""
    model = load_sql_based_model(
        parse(  # type: ignore
            f"""
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                )
            );

            {pre_statement};

            SELECT a::int, ds::string FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.previous_versions = snapshot.all_versions

    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.with_settings = lambda **kwargs: adapter
    adapter.table_exists = lambda _: True  # type: ignore
    adapter.SUPPORTS_CLONING = True
    mocker.patch.object(
        adapter,
        "get_data_objects",
        return_value=[],
    )
    mocker.patch.object(
        adapter,
        "get_alter_operations",
        return_value=[],
    )

    evaluator = SnapshotEvaluator(adapter)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())
    adapter.cursor.execute.assert_any_call(pre_statement)


def test_create_only_dev_table_exists(mocker: MockerFixture, adapter_mock, make_snapshot):
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            name=f"test_schema__test_model__{snapshot.version}__dev",
            schema="sqlmesh__test_schema",
            type=DataObjectType.VIEW,
        ),
    ]
    adapter_mock.table_exists.return_value = True
    evaluator = SnapshotEvaluator(adapter_mock)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())
    adapter_mock.create_view.assert_not_called()
    adapter_mock.get_data_objects.assert_called_once_with(
        schema_("sqlmesh__test_schema"),
        {
            f"test_schema__test_model__{snapshot.version}__dev",
        },
        safe_to_cache=True,
    )


def test_create_new_forward_only_model(mocker: MockerFixture, adapter_mock, make_snapshot):
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                )
            );

            SELECT a::int, '2024-01-01' as ds FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.get_data_objects.return_value = []
    adapter_mock.table_exists.return_value = False
    evaluator = SnapshotEvaluator(adapter_mock)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())
    # Only non-deployable table should be created
    adapter_mock.create_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.dev_version}__dev",
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("varchar"),
        },
        table_format=None,
        storage_format=None,
        partitioned_by=model.partitioned_by,
        partition_interval_unit=model.partition_interval_unit,
        clustered_by=[],
        table_properties={},
        table_description=None,
        column_descriptions=None,
    )
    adapter_mock.get_data_objects.assert_called_once_with(
        schema_("sqlmesh__test_schema"),
        {
            f"test_schema__test_model__{snapshot.dev_version}__dev",
        },
        safe_to_cache=True,
    )


@pytest.mark.parametrize(
    "deployability_index,  snapshot_category, forward_only",
    [
        (DeployabilityIndex.all_deployable(), SnapshotChangeCategory.BREAKING, False),
        (DeployabilityIndex.all_deployable(), SnapshotChangeCategory.NON_BREAKING, False),
        (DeployabilityIndex.all_deployable(), SnapshotChangeCategory.BREAKING, True),
        (
            DeployabilityIndex.all_deployable(),
            SnapshotChangeCategory.INDIRECT_BREAKING,
            False,
        ),
        (
            DeployabilityIndex.all_deployable(),
            SnapshotChangeCategory.INDIRECT_NON_BREAKING,
            False,
        ),
        (DeployabilityIndex.all_deployable(), SnapshotChangeCategory.METADATA, False),
        (
            DeployabilityIndex.none_deployable(),
            SnapshotChangeCategory.BREAKING,
            False,
        ),
        (
            DeployabilityIndex.none_deployable(),
            SnapshotChangeCategory.NON_BREAKING,
            False,
        ),
        (
            DeployabilityIndex.none_deployable(),
            SnapshotChangeCategory.BREAKING,
            True,
        ),
        (
            DeployabilityIndex.none_deployable(),
            SnapshotChangeCategory.INDIRECT_BREAKING,
            False,
        ),
        (
            DeployabilityIndex.none_deployable(),
            SnapshotChangeCategory.INDIRECT_NON_BREAKING,
            False,
        ),
        (
            DeployabilityIndex.none_deployable(),
            SnapshotChangeCategory.METADATA,
            False,
        ),
    ],
)
def test_create_tables_exist(
    snapshot: Snapshot,
    mocker: MockerFixture,
    adapter_mock,
    deployability_index: DeployabilityIndex,
    snapshot_category: SnapshotChangeCategory,
    forward_only: bool,
):
    adapter_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter")
    adapter_mock.dialect = "duckdb"
    adapter_mock.with_settings.return_value = adapter_mock

    evaluator = SnapshotEvaluator(adapter_mock)
    snapshot.categorize_as(category=snapshot_category, forward_only=forward_only)

    table_name = (
        f"db__model__{snapshot.version}"
        if deployability_index.is_deployable(snapshot)
        else f"db__model__{snapshot.version}__dev"
    )

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            name=table_name,
            schema="sqlmesh__db",
            type=DataObjectType.TABLE,
        ),
    ]

    evaluator.create(
        target_snapshots=[snapshot],
        snapshots={},
        deployability_index=deployability_index,
    )

    adapter_mock.get_data_objects.assert_called_once_with(
        schema_("sqlmesh__db"),
        {table_name},
        safe_to_cache=True,
    )
    adapter_mock.create_schema.assert_not_called()
    adapter_mock.create_table.assert_not_called()


def test_create_prod_table_exists_forward_only(mocker: MockerFixture, adapter_mock, make_snapshot):
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            name=f"test_schema__test_model__{snapshot.version}",
            schema="sqlmesh__test_schema",
            type=DataObjectType.TABLE,
        ),
    ]
    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.create([snapshot], {})

    adapter_mock.get_data_objects.assert_called_once_with(
        schema_("sqlmesh__test_schema"),
        {
            f"test_schema__test_model__{snapshot.version}",
        },
        safe_to_cache=True,
    )

    adapter_mock.create_table.assert_not_called()


def test_create_view_non_deployable_snapshot(mocker: MockerFixture, adapter_mock, make_snapshot):
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.get_data_objects.return_value = []
    adapter_mock.table_exists.return_value = False
    evaluator = SnapshotEvaluator(adapter_mock)

    deployability_index = DeployabilityIndex.none_deployable()
    evaluator.create([snapshot], {}, deployability_index=deployability_index)

    adapter_mock.create_view.assert_called_once_with(
        snapshot.table_name(is_deployable=False),
        model.render_query(),
        column_descriptions=None,
        view_properties={},
        table_description=None,
        materialized=False,
        replace=False,
        materialized_properties=None,
    )


def test_create_materialized_view(mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock.get_data_objects.return_value = []
    adapter_mock.table_exists.return_value = False

    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW (
                    materialized true
                )
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {})

    common_kwargs = dict(
        materialized=True,
        materialized_properties={
            "clustered_by": [],
            "partition_interval_unit": None,
            "partitioned_by": [],
        },
        view_properties={},
        table_description=None,
        replace=False,
    )

    adapter_mock.create_view.assert_called_once_with(
        snapshot.table_name(), model.render_query(), column_descriptions={}, **common_kwargs
    )


def test_create_view_with_properties(mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock.get_data_objects.return_value = []
    adapter_mock.table_exists.return_value = False

    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind VIEW (
                    materialized true
                ),
                physical_properties (
                    "key" = 'value'
                )
            );

            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {})

    common_kwargs = dict(
        materialized=True,
        view_properties={
            "key": exp.convert("value"),
        },
        materialized_properties={
            "clustered_by": [],
            "partition_interval_unit": None,
            "partitioned_by": [],
        },
        table_description=None,
        replace=False,
    )

    adapter_mock.create_view.assert_called_once_with(
        snapshot.table_name(), model.render_query(), column_descriptions={}, **common_kwargs
    )


def test_promote_model_info(mocker: MockerFixture, make_snapshot):
    adapter_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter")
    adapter_mock.dialect = "duckdb"
    adapter_mock.with_settings.return_value = adapter_mock

    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_model",
        kind=FullKind(),
        query=parse_one("SELECT a FROM tbl"),
    )

    snapshot = make_snapshot(model, version="1")
    snapshot.physical_schema_ = "physical_schema"
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.create_schema.assert_called_once_with(to_schema("test_schema__test_env"))
    adapter_mock.create_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        parse_one(f"SELECT * FROM physical_schema.test_schema__test_model__{snapshot.version}"),
        table_description=None,
        column_descriptions=None,
        view_properties={},
    )


def test_promote_deployable(mocker: MockerFixture, make_snapshot):
    adapter_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter")
    adapter_mock.dialect = "duckdb"
    adapter_mock.with_settings.return_value = adapter_mock

    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_model",
        kind=FullKind(),
        query=parse_one("SELECT a FROM tbl"),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            name=f"test_schema__test_model__{snapshot.version}",
            schema="sqlmesh__test_schema",
            type=DataObjectType.TABLE,
        ),
    ]

    evaluator.create([snapshot], {})
    adapter_mock.get_data_objects.assert_called_once_with(
        schema_("sqlmesh__test_schema"),
        {
            f"test_schema__test_model__{snapshot.version}",
        },
        safe_to_cache=True,
    )
    adapter_mock.create_table.assert_not_called()

    adapter_mock.get_data_objects.return_value = []
    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.create_schema.assert_called_once_with(to_schema("test_schema__test_env"))
    adapter_mock.create_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        parse_one(
            f"SELECT * FROM sqlmesh__test_schema.test_schema__test_model__{snapshot.version}"
        ),
        table_description=None,
        column_descriptions=None,
        view_properties={},
    )


def test_migrate(mocker: MockerFixture, make_snapshot, make_mocked_engine_adapter):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    session_spy = mocker.spy(adapter, "session")

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(
            time_column="a", on_destructive_change=OnDestructiveChange.ALLOW
        ),
        storage_format="parquet",
        query=parse_one("SELECT c, a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    current_table = snapshot.table_name()

    def columns(table_name):
        if table_name == current_table:
            return {
                "c": exp.DataType.build("int"),
                "b": exp.DataType.build("int"),
            }
        return {
            "c": exp.DataType.build("int"),
            "a": exp.DataType.build("int"),
        }

    adapter.columns = columns  # type: ignore
    adapter.table_exists = lambda _: True  # type: ignore
    mocker.patch.object(
        adapter,
        "get_data_objects",
        return_value=[
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model__{snapshot.version}",
                type="table",
            )
        ],
    )

    evaluator = SnapshotEvaluator(adapter)

    evaluator.migrate([snapshot], {})

    adapter.cursor.execute.assert_has_calls(
        [
            call(
                f'ALTER TABLE "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}" DROP COLUMN "b"'
            ),
            call(
                f'ALTER TABLE "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}" ADD COLUMN "a" INT'
            ),
        ]
    )

    session_spy.assert_called_once()


def test_migrate_missing_table(mocker: MockerFixture, make_snapshot, make_mocked_engine_adapter):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.table_exists = lambda _: False  # type: ignore
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    mocker.patch.object(adapter, "get_data_object", return_value=None)

    evaluator = SnapshotEvaluator(adapter)

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(
            time_column="a", on_destructive_change=OnDestructiveChange.ALLOW
        ),
        storage_format="parquet",
        query=parse_one("SELECT c, a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
        pre_statements=[parse_one("CREATE TABLE pre (a INT)")],
        post_statements=[parse_one("DROP TABLE pre")],
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    evaluator.migrate([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    adapter.cursor.execute.assert_not_called()


@pytest.mark.parametrize(
    "change_category, forward_only",
    [
        (SnapshotChangeCategory.BREAKING, True),
        (SnapshotChangeCategory.INDIRECT_NON_BREAKING, False),
    ],
)
def test_migrate_view(
    mocker: MockerFixture,
    make_snapshot,
    make_mocked_engine_adapter,
    change_category: SnapshotChangeCategory,
    forward_only: bool,
):
    model = SqlModel(
        name="test_schema.test_model",
        kind=ViewKind(),
        storage_format="parquet",
        query=parse_one("SELECT c, a FROM tbl"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = change_category
    snapshot.forward_only = forward_only

    adapter = make_mocked_engine_adapter(EngineAdapter)
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.get_data_objects",
        return_value=[
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model__{snapshot.version}",
                type="view",
            )
        ],
    )

    evaluator = SnapshotEvaluator(adapter)
    evaluator.migrate([snapshot], {})

    adapter.cursor.execute.assert_has_calls(
        [
            call(
                f'CREATE OR REPLACE VIEW "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}" ("c", "a") AS SELECT "c" AS "c", "a" AS "a" FROM "tbl" AS "tbl"'
            ),
        ]
    )


def test_migrate_snapshot_data_object_type_mismatch(
    mocker: MockerFixture,
    make_snapshot,
    make_mocked_engine_adapter,
):
    model = SqlModel(
        name="test_schema.test_model",
        kind=FullKind(),
        storage_format="parquet",
        query=parse_one("SELECT c, a FROM tbl"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.get_data_objects",
        return_value=[
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model__{snapshot.version}",
                type="view",
            )
        ],
    )
    mocker.patch.object(adapter, "table_exists", return_value=False)

    evaluator = SnapshotEvaluator(adapter)

    evaluator.migrate([snapshot], {})

    adapter.cursor.execute.assert_has_calls(
        [
            call(
                f'DROP VIEW IF EXISTS "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}"'
            ),
        ]
    )


def test_evaluate_creation_duckdb(
    snapshot: Snapshot,
    duck_conn,
    date_kwargs: t.Dict[str, str],
):
    evaluator = SnapshotEvaluator(create_engine_adapter(lambda: duck_conn, "duckdb"))
    evaluator.create_physical_schemas([snapshot], DeployabilityIndex.all_deployable())
    evaluator.create([snapshot], {})
    version = snapshot.version

    def assert_tables_exist() -> None:
        assert duck_conn.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables"
        ).fetchall() == [
            ("sqlmesh__db", f"db__model__{version}", "BASE TABLE"),
            ("main", "tbl", "VIEW"),
        ]

    # test that a clean run works
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots={},
    )
    assert_tables_exist()
    assert duck_conn.execute(f"SELECT * FROM sqlmesh__db.db__model__{version}").fetchall() == [(1,)]

    # test that existing tables work
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots={},
    )
    assert_tables_exist()
    assert duck_conn.execute(f"SELECT * FROM sqlmesh__db.db__model__{version}").fetchall() == [(1,)]


def test_migrate_duckdb(snapshot: Snapshot, duck_conn, make_snapshot):
    evaluator = SnapshotEvaluator(create_engine_adapter(lambda: duck_conn, "duckdb"))
    evaluator.create_physical_schemas([snapshot], DeployabilityIndex.all_deployable())
    evaluator.create([snapshot], {})

    updated_model_dict = snapshot.model.dict()
    updated_model_dict["query"] = "SELECT a::int, 1 as b FROM tbl"
    updated_model = SqlModel.parse_obj(updated_model_dict)

    new_snapshot = make_snapshot(updated_model)
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.version = snapshot.version

    evaluator.create([new_snapshot], {})
    evaluator.migrate([new_snapshot], {})

    evaluator.evaluate(
        new_snapshot,
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots={},
    )

    assert duck_conn.execute(
        f"SELECT b FROM sqlmesh__db.db__model__{snapshot.version}"
    ).fetchall() == [(1,)]


def test_audit_unversioned(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    snapshot = make_snapshot(
        SqlModel(
            name="db.model",
            kind=FullKind(),
            query=parse_one("SELECT a::int FROM tbl"),
        )
    )

    with pytest.raises(
        ConfigError,
        match="""Cannot audit '"db"."model"' because it has not been versioned yet. Apply a plan first.""",
    ):
        evaluator.audit(snapshot=snapshot, snapshots={})


@pytest.mark.parametrize(
    "input_dfs, output_dict",
    [
        (
            """pd.DataFrame({"a": [1, 2, 3], "ds": ["2023-01-01", "2023-01-02", "2023-01-03"]}),
        pd.DataFrame({"a": [4, 5, 6], "ds": ["2023-01-04", "2023-01-05", "2023-01-06"]}),
        pd.DataFrame({"a": [7, 8, 9], "ds": ["2023-01-07", "2023-01-08", "2023-01-09"]})""",
            {
                "a": {
                    0: 1,
                    1: 2,
                    2: 3,
                    3: 4,
                    4: 5,
                    5: 6,
                    6: 7,
                    7: 8,
                    8: 9,
                },
                "ds": {
                    0: "2023-01-01",
                    1: "2023-01-02",
                    2: "2023-01-03",
                    3: "2023-01-04",
                    4: "2023-01-05",
                    5: "2023-01-06",
                    6: "2023-01-07",
                    7: "2023-01-08",
                    8: "2023-01-09",
                },
            },
        ),
        (
            """pd.DataFrame({"a": [1, 2, 3], "ds": ["2023-01-01", "2023-01-02", "2023-01-03"]})""",
            {
                "a": {
                    0: 1,
                    1: 2,
                    2: 3,
                },
                "ds": {
                    0: "2023-01-01",
                    1: "2023-01-02",
                    2: "2023-01-03",
                },
            },
        ),
    ],
)
def test_snapshot_evaluator_yield_pd(adapter_mock, make_snapshot, input_dfs, output_dict):
    adapter_mock.is_pyspark_df.return_value = False
    adapter_mock.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE
    adapter_mock.try_get_df = lambda x: x
    evaluator = SnapshotEvaluator(adapter_mock)

    snapshot = make_snapshot(
        PythonModel(
            name="db.model",
            entrypoint="python_func",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds", format="%Y-%m-%d")),
            columns={
                "a": "INT",
                "ds": "STRING",
            },
            python_env={
                "python_func": Executable(
                    name="python_func",
                    alias="python_func",
                    path="test_snapshot_evaluator.py",
                    payload=f"""import pandas as pd  # noqa: TID253
def python_func(**kwargs):
    for df in [
        {input_dfs}
    ]:
        yield df""",
                )
            },
        )
    )

    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.create([snapshot], {})

    evaluator.evaluate(
        snapshot,
        start="2023-01-01",
        end="2023-01-09",
        execution_time="2023-01-09",
        snapshots={},
    )

    assert adapter_mock.insert_overwrite_by_time_partition.call_args[0][1].to_dict() == output_dict


def test_snapshot_evaluator_yield_empty_pd(adapter_mock, make_snapshot):
    adapter_mock.is_pyspark_df.return_value = False
    adapter_mock.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE
    adapter_mock.try_get_df = lambda x: x
    evaluator = SnapshotEvaluator(adapter_mock)

    snapshot = make_snapshot(
        PythonModel(
            name="db.model",
            entrypoint="python_func",
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ds", format="%Y-%m-%d")),
            columns={
                "a": "INT",
                "ds": "STRING",
            },
            python_env={
                "python_func": Executable(
                    name="python_func",
                    alias="python_func",
                    path="test_snapshot_evaluator.py",
                    payload="""def python_func(**kwargs):
    yield from ()""",
                )
            },
        )
    )

    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.create([snapshot], {})

    # This should not raise a TypeError from reduce() with empty sequence
    evaluator.evaluate(
        snapshot,
        start="2023-01-01",
        end="2023-01-09",
        execution_time="2023-01-09",
        snapshots={},
    )

    # When there are no dataframes to process, insert_overwrite_by_time_partition should not be called
    adapter_mock.insert_overwrite_by_time_partition.assert_not_called()


def test_create_clone_in_dev(mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock.SUPPORTS_CLONING = True
    adapter_mock.get_alter_operations.return_value = []
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                )
            );

            SELECT 1::INT as a, ds::DATE FROM a;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.previous_versions = snapshot.all_versions

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    adapter_mock.create_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev_schema_tmp",
        target_columns_to_types={"a": exp.DataType.build("int"), "ds": exp.DataType.build("date")},
        table_format=None,
        storage_format=None,
        partitioned_by=[exp.to_column("ds", quoted=True)],
        partition_interval_unit=IntervalUnit.DAY,
        clustered_by=[],
        table_properties={},
        table_description=None,
        column_descriptions=None,
    )

    adapter_mock.clone_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.dev_version}__dev",
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}",
        rendered_physical_properties={},
    )

    adapter_mock.get_alter_operations.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev",
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev_schema_tmp",
        ignore_destructive=False,
        ignore_additive=False,
    )

    adapter_mock.alter_table.assert_called_once_with([])

    adapter_mock.drop_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev_schema_tmp"
    )


def test_drop_clone_in_dev_when_migration_fails(mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock.SUPPORTS_CLONING = True
    adapter_mock.get_alter_operations.return_value = []
    evaluator = SnapshotEvaluator(adapter_mock)

    adapter_mock.alter_table.side_effect = DestructiveChangeError("Migration failed")

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                )
            );

            SELECT 1::INT as a, ds::DATE FROM a;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.previous_versions = snapshot.all_versions

    with pytest.raises(SnapshotCreationFailedError):
        evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    adapter_mock.clone_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev",
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}",
        rendered_physical_properties={},
    )

    adapter_mock.get_alter_operations.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev",
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev_schema_tmp",
        ignore_destructive=False,
        ignore_additive=False,
    )

    adapter_mock.alter_table.assert_called_once_with([])

    adapter_mock.drop_table.assert_has_calls(
        [
            call(
                f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev_schema_tmp"
            ),
            call(f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev"),
        ]
    )


@pytest.mark.parametrize("use_this_model", [True, False])
def test_create_clone_in_dev_self_referencing(
    mocker: MockerFixture, adapter_mock, make_snapshot, use_this_model: bool
):
    adapter_mock.SUPPORTS_CLONING = True
    adapter_mock.get_alter_operations.return_value = []
    evaluator = SnapshotEvaluator(adapter_mock)

    from_table = "test_schema.test_model" if not use_this_model else "@this_model"
    model = load_sql_based_model(
        parse(  # type: ignore
            f"""
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                )
            );

            SELECT 1::INT as a, ds::DATE FROM {from_table};
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.previous_versions = snapshot.all_versions

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    adapter_mock.create_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev_schema_tmp",
        target_columns_to_types={"a": exp.DataType.build("int"), "ds": exp.DataType.build("date")},
        table_format=None,
        storage_format=None,
        partitioned_by=[exp.to_column("ds", quoted=True)],
        partition_interval_unit=IntervalUnit.DAY,
        clustered_by=[],
        table_properties={},
        table_description=None,
        column_descriptions=None,
    )

    # Make sure the dry run references the correct ("..._schema_tmp") table.
    table_alias = (
        "test_model"
        if not use_this_model
        else f"test_schema__test_model__{snapshot.version}__dev_schema_tmp"
    )
    dry_run_query = adapter_mock.fetchall.call_args[0][0].sql()
    assert (
        dry_run_query
        == f'SELECT CAST(1 AS INT) AS "a", CAST("ds" AS DATE) AS "ds" FROM "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}__dev_schema_tmp" AS "{table_alias}" /* test_schema.test_model */ WHERE FALSE LIMIT 0'
    )


def test_on_destructive_change_runtime_check(
    mocker: MockerFixture,
    make_snapshot,
    make_mocked_engine_adapter,
):
    # SQLMesh default: ERROR
    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a"),
        query=parse_one("SELECT c, a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore

    current_table = snapshot.table_name()

    def columns(table_name):
        if table_name == current_table:
            return {
                "c": exp.DataType.build("int"),
                "b": exp.DataType.build("int"),
            }
        return {
            "c": exp.DataType.build("int"),
            "a": exp.DataType.build("int"),
        }

    adapter.columns = columns  # type: ignore
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.get_data_objects",
        return_value=[
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model__{snapshot.version}",
                type=DataObjectType.TABLE,
            )
        ],
    )

    evaluator = SnapshotEvaluator(adapter)

    with pytest.raises(NodeExecutionFailedError) as ex:
        evaluator.migrate([snapshot], {})

    destructive_change_err = ex.value.__cause__
    assert isinstance(destructive_change_err, DestructiveChangeError)
    assert (
        str(destructive_change_err)
        == "\nPlan requires destructive change to forward-only model '\"test_schema\".\"test_model\"'s schema that drops column 'b'.\n\nSchema changes:\n  ALTER TABLE sqlmesh__test_schema.test_schema__test_model__1 DROP COLUMN b\n  ALTER TABLE sqlmesh__test_schema.test_schema__test_model__1 ADD COLUMN a INT\n\nTo allow the destructive change, set the model's `on_destructive_change` setting to `warn`, `allow`, or `ignore` or include the model in the plan's `--allow-destructive-model` option.\n"
    )

    # WARN
    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(
            time_column="a", on_destructive_change=OnDestructiveChange.WARN
        ),
        query=parse_one("SELECT c, a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    logger = logging.getLogger("sqlmesh.core.snapshot.evaluator")
    with patch.object(logger, "warning") as mock_logger:
        evaluator.migrate([snapshot], {})
        assert (
            mock_logger.call_args[0][0]
            == "\nPlan requires destructive change to forward-only model '\"test_schema\".\"test_model\"'s schema that drops column 'b'.\n\nSchema changes:\n  ALTER TABLE sqlmesh__test_schema.test_schema__test_model__1 DROP COLUMN b\n  ALTER TABLE sqlmesh__test_schema.test_schema__test_model__1 ADD COLUMN a INT"
        )

    # allow destructive
    with patch.object(logger, "warning") as mock_logger:
        evaluator.migrate(
            [snapshot],
            {},
            {'"test_schema"."test_model"'},
        )
        assert mock_logger.call_count == 0


def test_on_additive_change_runtime_check(
    mocker: MockerFixture,
    make_snapshot,
    make_mocked_engine_adapter,
):
    # SQLMesh default: ERROR
    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="a", on_additive_change=OnAdditiveChange.ERROR),
        query=parse_one("SELECT c, a, b FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    current_table = snapshot.table_name()

    def columns(table_name):
        if table_name == current_table:
            return {
                "c": exp.DataType.build("int"),
                "a": exp.DataType.build("int"),
            }
        return {
            "c": exp.DataType.build("int"),
            "a": exp.DataType.build("int"),
            "b": exp.DataType.build("int"),
        }

    adapter.columns = columns  # type: ignore
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.get_data_objects",
        return_value=[
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model__{snapshot.version}",
                type=DataObjectType.TABLE,
            )
        ],
    )

    evaluator = SnapshotEvaluator(adapter)

    with pytest.raises(NodeExecutionFailedError) as ex:
        evaluator.migrate([snapshot], {})

    additive_change_error = ex.value.__cause__
    assert isinstance(additive_change_error, AdditiveChangeError)
    assert (
        str(additive_change_error)
        == "\nPlan requires additive change to forward-only model '\"test_schema\".\"test_model\"'s schema that adds column 'b'.\n\nSchema changes:\n  ALTER TABLE sqlmesh__test_schema.test_schema__test_model__1 ADD COLUMN b INT\n\nTo allow the additive change, set the model's `on_additive_change` setting to `warn`, `allow`, or `ignore` or include the model in the plan's `--allow-additive-model` option.\n"
    )

    # WARN
    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(
            time_column="a", on_additive_change=OnDestructiveChange.WARN
        ),
        query=parse_one("SELECT c, a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.change_category = SnapshotChangeCategory.BREAKING
    snapshot.forward_only = True
    snapshot.previous_versions = snapshot.all_versions

    logger = logging.getLogger("sqlmesh.core.snapshot.evaluator")
    with patch.object(logger, "warning") as mock_logger:
        evaluator.migrate([snapshot], {})
        assert (
            mock_logger.call_args[0][0]
            == "\nPlan requires additive change to forward-only model '\"test_schema\".\"test_model\"'s schema that adds column 'b'.\n\nSchema changes:\n  ALTER TABLE sqlmesh__test_schema.test_schema__test_model__1 ADD COLUMN b INT"
        )


def test_temp_table_includes_schema_for_ignore_changes(
    mocker: MockerFixture,
    make_snapshot,
    make_mocked_engine_adapter,
):
    """Test that temp table creation includes the physical schema when on_destructive_change or on_additive_change is IGNORE."""
    # Create a model with on_destructive_change=IGNORE to trigger temp table creation
    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(
            time_column="ds", on_destructive_change=OnDestructiveChange.IGNORE
        ),
        query=parse_one("SELECT c, a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot = make_snapshot(model, version="1")
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # Set up the mocked adapter
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    adapter.table_exists = lambda _: True  # type: ignore

    # Mock columns method to return existing columns
    def columns(table_name):
        return {
            "c": exp.DataType.build("int"),
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("timestamp"),
        }

    adapter.columns = columns  # type: ignore

    # Create a mock for the temp_table context manager
    temp_table_name_captured = None

    @contextlib.contextmanager
    def mock_temp_table(query_or_df, name="diff", **kwargs):
        nonlocal temp_table_name_captured
        temp_table_name_captured = exp.to_table(name)
        # Return a table that temp_table would normally return
        yield exp.table_("__temp_diff_12345", db=temp_table_name_captured.db)

    adapter.temp_table = mock_temp_table  # type: ignore
    adapter.insert_append = lambda *args, **kwargs: None  # type: ignore

    evaluator = SnapshotEvaluator(adapter)

    # Call the append method which will trigger _get_target_and_source_columns
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    # Verify that temp_table was called with a name that includes the schema
    assert temp_table_name_captured is not None
    assert temp_table_name_captured.name == "diff"
    assert temp_table_name_captured.db == model.physical_schema
    assert str(temp_table_name_captured.db) == "sqlmesh__test_schema"


def test_forward_only_snapshot_for_added_model(mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock.SUPPORTS_CLONING = False
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                )
            );

            SELECT 1::INT as a, ds::DATE FROM a;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    common_create_args = dict(
        target_columns_to_types={"a": exp.DataType.build("int"), "ds": exp.DataType.build("date")},
        table_format=None,
        storage_format=None,
        partitioned_by=[exp.to_column("ds", quoted=True)],
        partition_interval_unit=IntervalUnit.DAY,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )
    adapter_mock.create_table.assert_has_calls(
        [
            call(
                f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}__dev",
                column_descriptions=None,
                **common_create_args,
            ),
        ]
    )


def test_create_scd_type_2_by_time(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind SCD_TYPE_2 (
                    unique_key [id, name],
                    time_data_type TIMESTAMPTZ
                )
            );

            SELECT id::int, name::string, updated_at::timestamp FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    common_kwargs = dict(
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
            "updated_at": exp.DataType.build("TIMESTAMPTZ"),
            # Make sure that the call includes these extra columns
            "valid_from": exp.DataType.build("TIMESTAMPTZ"),
            "valid_to": exp.DataType.build("TIMESTAMPTZ"),
        },
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    adapter_mock.create_table.assert_has_calls(
        [
            call(
                snapshot.table_name(is_deployable=False),
                column_descriptions=None,
                **common_kwargs,
            ),
        ]
    )


def test_create_ctas_scd_type_2_by_time(adapter_mock, make_snapshot, mocker):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind SCD_TYPE_2 (
                    unique_key id,
                    time_data_type TIMESTAMPTZ,
                    invalidate_hard_deletes false
                ),
                partitioned_by cola
            );

            SELECT * FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    source_query = parse_one('SELECT * FROM "tbl" AS "tbl"')
    query = parse_one(
        """SELECT *, CAST(NULL AS TIMESTAMPTZ) AS valid_from, CAST(NULL AS TIMESTAMPTZ) AS valid_to FROM "tbl" AS "tbl" WHERE FALSE LIMIT 0"""
    )

    # Verify that managed columns are included in CTAS with types
    common_kwargs = dict(
        table_format=None,
        storage_format=None,
        partitioned_by=[
            exp.to_column("cola", quoted=True),
        ],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    adapter_mock.ctas.assert_has_calls(
        [
            call(
                snapshot.table_name(is_deployable=False),
                query,
                None,
                column_descriptions=None,
                **common_kwargs,
            ),
        ]
    )

    adapter_mock.reset_mock()

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
        deployability_index=DeployabilityIndex.none_deployable(),
    )

    adapter_mock.scd_type_2_by_time.assert_has_calls(
        [
            call(
                column_descriptions={},
                execution_time="2020-01-02",
                invalidate_hard_deletes=False,
                source_columns=None,
                source_table=source_query,
                target_columns_to_types=mocker.ANY,
                target_table=snapshot.table_name(is_deployable=False),
                truncate=True,
                unique_key=[exp.to_column("id", quoted=True)],
                updated_at_as_valid_from=False,
                updated_at_col=exp.column("updated_at", quoted=True),
                valid_from_col=exp.column("valid_from", quoted=True),
                valid_to_col=exp.column("valid_to", quoted=True),
                **common_kwargs,
            ),
        ]
    )


@pytest.mark.parametrize(
    "intervals,truncate",
    [
        ([(to_timestamp("2024-01-01"), to_timestamp("2024-01-02"))], False),
        ([], True),
    ],
)
def test_insert_into_scd_type_2_by_time(
    adapter_mock, make_snapshot, intervals: Intervals, truncate: bool
):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind SCD_TYPE_2 (
                    unique_key id,
                )
            );

            SELECT id::int, name::string, updated_at::timestamp FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = intervals

    table_columns = {
        "id": exp.DataType.build("INT"),
        "name": exp.DataType.build("STRING"),
        "updated_at": exp.DataType.build("TIMESTAMP"),
        # Make sure that the call includes these extra columns
        "valid_from": exp.DataType.build("TIMESTAMP"),
        "valid_to": exp.DataType.build("TIMESTAMP"),
    }
    adapter_mock.columns.return_value = table_columns

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.scd_type_2_by_time.assert_called_once_with(
        target_table=snapshot.table_name(),
        source_table=model.render_query(),
        target_columns_to_types=table_columns,
        table_format=None,
        unique_key=[exp.to_column("id", quoted=True)],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        execution_time="2020-01-02",
        updated_at_col=exp.column("updated_at", quoted=True),
        invalidate_hard_deletes=False,
        table_description=None,
        column_descriptions={},
        updated_at_as_valid_from=False,
        truncate=truncate,
        source_columns=None,
        clustered_by=[],
        partition_interval_unit=None,
        partitioned_by=[],
        storage_format=None,
        table_properties={},
    )
    adapter_mock.columns.assert_called_once_with(snapshot.table_name())


def test_create_scd_type_2_by_column(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind SCD_TYPE_2_BY_COLUMN (
                    unique_key id,
                    columns *,
                    invalidate_hard_deletes false,
                )
            );

            SELECT id::int, name::string, FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    common_kwargs = dict(
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
            # Make sure that the call includes these extra columns
            "valid_from": exp.DataType.build("TIMESTAMP"),
            "valid_to": exp.DataType.build("TIMESTAMP"),
        },
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    adapter_mock.create_table.assert_has_calls(
        [
            call(
                snapshot.table_name(is_deployable=False),
                **{**common_kwargs, "column_descriptions": None},
            ),
        ]
    )


def test_create_ctas_scd_type_2_by_column(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind SCD_TYPE_2_BY_COLUMN (
                    unique_key id,
                    columns *
                )
            );

            SELECT * FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    query = parse_one(
        """SELECT *, CAST(NULL AS TIMESTAMP) AS valid_from, CAST(NULL AS TIMESTAMP) AS valid_to FROM "tbl" AS "tbl" WHERE FALSE LIMIT 0"""
    )

    # Verify that managed columns are included in CTAS with types
    common_kwargs = dict(
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    adapter_mock.ctas.assert_has_calls(
        [
            call(
                snapshot.table_name(is_deployable=False),
                query,
                None,
                **{**common_kwargs, "column_descriptions": None},
            ),
        ]
    )


@pytest.mark.parametrize(
    "intervals,truncate",
    [
        ([(to_timestamp("2024-01-01"), to_timestamp("2024-01-02"))], False),
        ([], True),
    ],
)
def test_insert_into_scd_type_2_by_column(
    adapter_mock, make_snapshot, intervals: Intervals, truncate: bool
):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind SCD_TYPE_2_BY_COLUMN (
                    unique_key id,
                    columns *
                )
            );

            SELECT id::int, name::string FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = intervals

    table_columns = {
        "id": exp.DataType.build("INT"),
        "name": exp.DataType.build("STRING"),
        # Make sure that the call includes these extra columns
        "valid_from": exp.DataType.build("TIMESTAMP"),
        "valid_to": exp.DataType.build("TIMESTAMP"),
    }
    adapter_mock.columns.return_value = table_columns

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.scd_type_2_by_column.assert_called_once_with(
        target_table=snapshot.table_name(),
        source_table=model.render_query(),
        target_columns_to_types=table_columns,
        table_format=None,
        unique_key=[exp.to_column("id", quoted=True)],
        check_columns=[exp.Star()],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        execution_time="2020-01-02",
        execution_time_as_valid_from=False,
        invalidate_hard_deletes=False,
        table_description=None,
        column_descriptions={},
        truncate=truncate,
        source_columns=None,
        clustered_by=[],
        partition_interval_unit=None,
        partitioned_by=[],
        storage_format=None,
        table_properties={},
    )
    adapter_mock.columns.assert_called_once_with(snapshot.table_name())


def test_create_incremental_by_unique_key_updated_at_exp(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key [id],
                    when_matched WHEN MATCHED THEN UPDATE SET target.name = source.name, target.updated_at = COALESCE(source.updated_at, target.updated_at)
                )
            );

            SELECT id::int, name::string, updated_at::timestamp FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.merge.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
            "updated_at": exp.DataType.build("TIMESTAMP"),
        },
        unique_key=[exp.to_column("id", quoted=True)],
        merge_filter=None,
        when_matched=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    source=False,
                    then=exp.Update(
                        expressions=[
                            exp.column("name", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.column("name", MERGE_SOURCE_ALIAS, quoted=True)
                            ),
                            exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.Coalesce(
                                    this=exp.column("updated_at", MERGE_SOURCE_ALIAS, quoted=True),
                                    expressions=[
                                        exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True)
                                    ],
                                )
                            ),
                        ],
                    ),
                )
            ]
        ),
        physical_properties={},
        source_columns=None,
    )


def test_create_incremental_by_unique_key_multiple_updated_at_exp(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key [id],
                    when_matched WHEN MATCHED AND source.id = 1 THEN UPDATE SET target.name = source.name, target.updated_at = COALESCE(source.updated_at, target.updated_at),
                    WHEN MATCHED THEN UPDATE SET target.name = source.name, target.updated_at = COALESCE(source.updated_at, target.updated_at)
                )
            );

            SELECT id::int, name::string, updated_at::timestamp FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.merge.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
            "updated_at": exp.DataType.build("TIMESTAMP"),
        },
        unique_key=[exp.to_column("id", quoted=True)],
        merge_filter=None,
        when_matched=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    condition=exp.column("id", MERGE_SOURCE_ALIAS, quoted=True).eq(
                        exp.Literal.number(1)
                    ),
                    then=exp.Update(
                        expressions=[
                            exp.column("name", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.column("name", MERGE_SOURCE_ALIAS, quoted=True)
                            ),
                            exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.Coalesce(
                                    this=exp.column("updated_at", MERGE_SOURCE_ALIAS, quoted=True),
                                    expressions=[
                                        exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True)
                                    ],
                                )
                            ),
                        ],
                    ),
                ),
                exp.When(
                    matched=True,
                    source=False,
                    then=exp.Update(
                        expressions=[
                            exp.column("name", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.column("name", MERGE_SOURCE_ALIAS, quoted=True)
                            ),
                            exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.Coalesce(
                                    this=exp.column("updated_at", MERGE_SOURCE_ALIAS, quoted=True),
                                    expressions=[
                                        exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True)
                                    ],
                                )
                            ),
                        ],
                    ),
                ),
            ],
        ),
        physical_properties={},
        source_columns=None,
    )


def test_create_incremental_by_unique_no_intervals(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key [id],
                )
            );

            SELECT id::int, name::string, updated_at::timestamp FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    table_columns = {
        "id": exp.DataType.build("int"),
        "name": exp.DataType.build("string"),
        "updated_at": exp.DataType.build("timestamp"),
    }
    adapter_mock.columns.return_value = table_columns

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    adapter_mock.replace_query.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        clustered_by=[],
        column_descriptions={},
        target_columns_to_types=table_columns,
        partition_interval_unit=model.partition_interval_unit,
        partitioned_by=model.partitioned_by,
        table_format=None,
        storage_format=None,
        table_description=None,
        table_properties={},
        source_columns=None,
    )
    adapter_mock.columns.assert_called_once_with(snapshot.table_name())


def test_create_incremental_by_unique_key_merge_filter(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        d.parse(
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key [id],
                    merge_filter source.id > 0 and target.updated_at < @end_ds and source.updated_at > @start_ds,
                    when_matched WHEN MATCHED THEN UPDATE SET target.updated_at = COALESCE(source.updated_at, target.updated_at),
                )
            );

            SELECT id::int, updated_at::timestamp FROM tbl;
            """
        )
    )

    # At load time macros should remain unresolved
    assert model.merge_filter == exp.And(
        this=exp.And(
            this=exp.GT(
                this=exp.column("id", MERGE_SOURCE_ALIAS, quoted=True),
                expression=exp.Literal(this="0", is_string=False),
            ),
            expression=exp.LT(
                this=exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True),
                expression=d.MacroVar(this="end_ds"),
            ),
        ),
        expression=exp.GT(
            this=exp.column("updated_at", MERGE_SOURCE_ALIAS, quoted=True),
            expression=d.MacroVar(this="start_ds"),
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    # The macros for merge_filter must now be rendered at evaluation time.
    adapter_mock.merge.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "updated_at": exp.DataType.build("TIMESTAMP"),
        },
        unique_key=[exp.to_column("id", quoted=True)],
        when_matched=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    then=exp.Update(
                        expressions=[
                            exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True).eq(
                                exp.Coalesce(
                                    this=exp.column("updated_at", MERGE_SOURCE_ALIAS, quoted=True),
                                    expressions=[
                                        exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True)
                                    ],
                                )
                            ),
                        ],
                    ),
                )
            ]
        ),
        merge_filter=exp.And(
            this=exp.And(
                this=exp.GT(
                    this=exp.column("id", MERGE_SOURCE_ALIAS, quoted=True),
                    expression=exp.Literal(this="0", is_string=False),
                ),
                expression=exp.LT(
                    this=exp.column("updated_at", MERGE_TARGET_ALIAS, quoted=True),
                    expression=exp.Literal(this="2020-01-02", is_string=True),
                ),
            ),
            expression=exp.GT(
                this=exp.column("updated_at", MERGE_SOURCE_ALIAS, quoted=True),
                expression=exp.Literal(this="2020-01-01", is_string=True),
            ),
        ),
        physical_properties={},
        source_columns=None,
    )


def test_create_seed(mocker: MockerFixture, adapter_mock, make_snapshot):
    expressions = d.parse(
        """
        MODEL (
            name db.seed,
            kind SEED (
              path '../seeds/waiter_names.csv',
              batch_size 5,
            )
        );
    """
    )

    model = load_sql_based_model(expressions, path=Path("./examples/sushi/models/test_model.sql"))

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.create([snapshot], {})

    common_create_kwargs: t.Dict[str, t.Any] = dict(
        target_columns_to_types={
            "id": exp.DataType.build("bigint"),
            "name": exp.DataType.build("text"),
        },
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    adapter_mock.replace_query.assert_called_once_with(
        f"sqlmesh__db.db__seed__{snapshot.version}",
        mocker.ANY,
        column_descriptions={},
        source_columns=["id", "name"],
        **common_create_kwargs,
    )

    adapter_mock.create_table.assert_called_once_with(
        f"sqlmesh__db.db__seed__{snapshot.version}",
        column_descriptions={},
        **common_create_kwargs,
    )

    replace_query_calls = adapter_mock.replace_query.call_args_list
    assert len(replace_query_calls) == 1

    df = replace_query_calls[0][0][1]
    assert list(df.itertuples(index=False)) == [
        (0, "Toby"),
        (1, "Tyson"),
        (2, "Ryan"),
        (3, "George"),
        (4, "Chris"),
    ]

    insert_append_calls = adapter_mock.insert_append.call_args_list
    assert len(insert_append_calls) == 1

    df = insert_append_calls[0][0][1]
    assert list(df.itertuples(index=False)) == [
        (5, "Max"),
        (6, "Vincent"),
        (7, "Iaroslav"),
        (8, "Emma"),
        (9, "Maia"),
    ]


def test_create_seed_on_error(mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock.insert_append.side_effect = Exception("test error")

    expressions = d.parse(
        """
        MODEL (
            name db.seed,
            kind SEED (
              path '../seeds/waiter_names.csv',
              batch_size 5,
            )
        );
    """
    )

    model = load_sql_based_model(expressions, path=Path("./examples/sushi/models/test_model.sql"))

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator = SnapshotEvaluator(adapter_mock)

    with pytest.raises(SnapshotCreationFailedError):
        evaluator.create([snapshot], {})

    adapter_mock.replace_query.assert_called_once_with(
        f"sqlmesh__db.db__seed__{snapshot.version}",
        mocker.ANY,
        column_descriptions={},
        target_columns_to_types={
            "id": exp.DataType.build("bigint"),
            "name": exp.DataType.build("text"),
        },
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
        source_columns=["id", "name"],
    )

    adapter_mock.drop_table.assert_called_once_with(f"sqlmesh__db.db__seed__{snapshot.version}")


def test_create_seed_no_intervals(mocker: MockerFixture, adapter_mock, make_snapshot):
    expressions = d.parse(
        """
        MODEL (
            name db.seed,
            kind SEED (
              path '../seeds/waiter_names.csv',
            )
        );
    """
    )

    model = load_sql_based_model(expressions, path=Path("./examples/sushi/models/test_model.sql"))

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.intervals = [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    # Simulate that the table already exists.
    adapter_mock.get_data_objects.return_value = [
        DataObject(
            name=f"db__seed__{snapshot.version}",
            schema="sqlmesh__db",
            type=DataObjectType.TABLE,
        ),
    ]

    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.create([snapshot], {})

    snapshot.intervals = []
    evaluator.create([snapshot], {})

    # The replace query should only be called once when there are no intervals.
    adapter_mock.replace_query.assert_called_once_with(
        f"sqlmesh__db.db__seed__{snapshot.version}",
        mocker.ANY,
        column_descriptions={},
        target_columns_to_types={
            "id": exp.DataType.build("bigint"),
            "name": exp.DataType.build("text"),
        },
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
        source_columns=["id", "name"],
    )


def test_standalone_audit(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    audit = StandaloneAudit(name="test_standalone_audit", query=parse_one("SELECT NULL LIMIT 0"))

    snapshot = make_snapshot(audit)
    snapshot.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    # Create
    evaluator.create([snapshot], {})

    adapter_mock.assert_not_called()
    adapter_mock.transaction.assert_not_called()
    adapter_mock.session.assert_not_called()

    # Evaluate
    payload = {"calls": 0}
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
        payload=payload,
    )

    adapter_mock.assert_not_called()
    adapter_mock.transaction.assert_not_called()
    adapter_mock.session.assert_not_called()
    assert payload["calls"] == 0

    # Audit
    adapter_mock.fetchone.return_value = (0,)
    evaluator.audit(snapshot=snapshot, snapshots={})

    query = audit.render_audit_query()
    adapter_mock.fetchone.assert_called_once_with(
        select("COUNT(*)").from_(query.subquery("audit")), quote_identifiers=True
    )

    # Promote
    adapter_mock.reset_mock()

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.assert_not_called()
    adapter_mock.transaction.assert_not_called()
    adapter_mock.session.assert_not_called()

    # Migrate
    evaluator.migrate([snapshot], snapshots={})

    adapter_mock.assert_not_called()
    adapter_mock.transaction.assert_not_called()
    adapter_mock.session.assert_not_called()

    # Demote
    evaluator.demote([snapshot], EnvironmentNamingInfo(name="test_env"))

    adapter_mock.assert_not_called()
    adapter_mock.transaction.assert_not_called()
    adapter_mock.session.assert_not_called()

    # Cleanup
    evaluator.cleanup(
        [SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=False)]
    )

    adapter_mock.assert_not_called()
    adapter_mock.transaction.assert_not_called()
    adapter_mock.session.assert_not_called()


def test_audit_wap(adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    custom_audit = ModelAudit(
        name="test_audit",
        query="SELECT * FROM test_schema.test_table WHERE 1 = 2",
    )

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
        audits=[
            ("not_null", {"columns": exp.to_column("a")}),
            ("test_audit", {}),
        ],
        audit_definitions={custom_audit.name: custom_audit},
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    wap_id = "test_wap_id"
    expected_table_name = f"spark_catalog.test_schema.test_table.branch_wap_{wap_id}"
    adapter_mock.wap_table_name.return_value = expected_table_name
    adapter_mock.fetchone.return_value = (0,)

    evaluator.audit(snapshot, snapshots={}, wap_id=wap_id)

    call_args = adapter_mock.fetchone.call_args_list
    assert len(call_args) == 2

    not_null_query = call_args[0][0][0]
    assert (
        not_null_query.sql(dialect="spark")
        == "SELECT COUNT(*) FROM (SELECT * FROM `spark_catalog`.`test_schema`.`test_table`.`branch_wap_test_wap_id` AS `branch_wap_test_wap_id` WHERE `a` IS NULL AND TRUE) AS audit"
    )

    custom_audit_query = call_args[1][0][0]
    assert (
        custom_audit_query.sql(dialect="spark")
        == "SELECT COUNT(*) FROM (SELECT * FROM `spark_catalog`.`test_schema`.`test_table`.`branch_wap_test_wap_id` AS `test_table` /* test_schema.test_table */ WHERE 1 = 2) AS audit"
    )

    adapter_mock.wap_table_name.assert_called_once_with(snapshot.table_name(), wap_id)
    adapter_mock.wap_publish.assert_called_once_with(snapshot.table_name(), wap_id)


def test_audit_with_datetime_macros(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=IncrementalByUniqueKeyKind(unique_key="a"),
        query=parse_one("SELECT a, start_ds FROM tbl"),
        audits=[
            (
                "unique_combination_of_columns",
                {
                    "columns": exp.Array(expressions=[exp.to_column("a")]),
                    "condition": d.MacroVar(this="start_ds").neq("2020-01-01"),
                },
            ),
        ],
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.fetchone.return_value = (0,)
    evaluator.audit(snapshot, snapshots={}, start="2020-01-01")

    call_args = adapter_mock.fetchone.call_args_list
    assert len(call_args) == 1

    unique_combination_of_columns_query = call_args[0][0][0]
    assert (
        unique_combination_of_columns_query.sql(dialect="duckdb")
        == """SELECT COUNT(*) FROM (SELECT "a" AS "a" FROM "test_schema"."test_table" AS "test_table" WHERE '2020-01-01' <> '2020-01-01' GROUP BY "a" HAVING COUNT(*) > 1) AS audit"""
    )


def test_audit_set_blocking_at_use_site(adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    always_failing_audit = ModelAudit(
        name="always_fail",
        query="SELECT * FROM test_schema.test_table",
    )

    @macro()
    def blocking_value(evaluator):
        return False

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_table,
                kind FULL,
                audits (
                    always_fail (blocking := @blocking_value())
                )
            );

            SELECT a::int FROM tbl
            """
        ),
        audit_definitions={always_failing_audit.name: always_failing_audit},
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # Return a non-zero count to indicate audit failure
    adapter_mock.fetchone.return_value = (1,)

    results = evaluator.audit(snapshot, snapshots={})
    assert len(results) == 1
    assert results[0].count == 1
    assert not results[0].blocking

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
        audits=[
            ("always_fail", {"blocking": exp.true()}),
        ],
        audit_definitions={always_failing_audit.name: always_failing_audit},
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    adapter_mock.fetchone.return_value = (1,)

    results = evaluator.audit(snapshot, snapshots={})
    assert len(results) == 1
    assert results[0].count == 1
    assert results[0].blocking


def test_create_post_statements_use_non_deployable_table(
    mocker: MockerFixture, adapter_mock, make_snapshot
):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
                dialect postgres,
            );

            CREATE INDEX IF NOT EXISTS test_idx ON test_schema.test_model(a);

            SELECT a::int FROM tbl;

            CREATE INDEX IF NOT EXISTS test_idx ON test_schema.test_model(a);
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    expected_call = f'CREATE INDEX IF NOT EXISTS "test_idx" ON "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}__dev" /* test_schema.test_model */("a" NULLS FIRST)'

    evaluator.create([snapshot], {}, DeployabilityIndex.none_deployable())

    call_args = adapter_mock.execute.call_args_list
    pre_calls = call_args[1][0][0]
    assert len(pre_calls) == 1
    assert pre_calls[0].sql(dialect="postgres") == expected_call

    post_calls = call_args[2][0][0]
    assert len(post_calls) == 1
    assert post_calls[0].sql(dialect="postgres") == expected_call


def test_create_pre_post_statements_python_model(
    mocker: MockerFixture, adapter_mock, make_snapshot
):
    evaluator = SnapshotEvaluator(adapter_mock)

    @macro()
    def create_index(
        evaluator: MacroEvaluator,
        index_name: str,
        model_name: str,
        column: str,
    ):
        if evaluator.runtime_stage == "creating":
            return f"CREATE INDEX IF NOT EXISTS {index_name} ON {model_name}({column});"

    @model(
        "db.test_model",
        kind="full",
        columns={"id": "string", "name": "string"},
        pre_statements=["CREATE INDEX IF NOT EXISTS idx ON db.test_model(id);"],
        post_statements=["@CREATE_INDEX('idx', 'db.test_model', id)"],
    )
    def model_with_statements(context, **kwargs):
        return pd.DataFrame(
            [
                {
                    "id": context.var("1"),
                    "name": context.var("var"),
                }
            ]
        )

    python_model = model.get_registry()["db.test_model"].model(
        module_path=Path("."),
        path=Path("."),
        macros=macro.get_registry(),
        dialect="postgres",
    )

    assert len(python_model.python_env) == 3
    assert len(python_model.pre_statements) == 1
    assert len(python_model.post_statements) == 1
    assert isinstance(python_model.python_env["create_index"], Executable)
    assert isinstance(python_model.pre_statements[0], exp.Create)
    assert isinstance(python_model.post_statements[0], MacroFunc)

    snapshot = make_snapshot(python_model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, DeployabilityIndex.none_deployable())
    expected_call = f'CREATE INDEX IF NOT EXISTS "idx" ON "sqlmesh__db"."db__test_model__{snapshot.version}__dev" /* db.test_model */("id")'

    call_args = adapter_mock.execute.call_args_list
    pre_calls = call_args[1][0][0]
    assert len(pre_calls) == 1
    assert pre_calls[0].sql(dialect="postgres") == expected_call

    post_calls = call_args[2][0][0]
    assert len(post_calls) == 1
    assert post_calls[0].sql(dialect="postgres") == expected_call


def test_on_virtual_update_statements(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    @macro()
    def create_log_table(evaluator, view_name):
        return f"CREATE OR REPLACE TABLE log_table AS SELECT '{view_name}' as fqn_this_model, '{evaluator.this_model}' as eval_this_model"

    model = load_sql_based_model(
        d.parse(
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
                dialect postgres,
            );

            SELECT a::int FROM tbl;

            CREATE INDEX IF NOT EXISTS test_idx ON test_schema.test_model(a);

            ON_VIRTUAL_UPDATE_BEGIN;
            JINJA_STATEMENT_BEGIN;
            GRANT SELECT ON VIEW test_schema.test_model TO ROLE admin;
            JINJA_END;
            GRANT REFERENCES, SELECT ON FUTURE VIEWS IN DATABASE demo_db TO ROLE owner_name;
            @create_log_table(@this_model);
            ON_VIRTUAL_UPDATE_END;

            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, DeployabilityIndex.none_deployable())

    snapshots = {snapshot.name: snapshot}
    environment_naming_info = EnvironmentNamingInfo(name="test_env")
    evaluator.promote(
        [snapshot],
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots=snapshots,
        environment_naming_info=environment_naming_info,
        table_mapping=to_view_mapping(
            snapshots.values(),
            environment_naming_info,
        ),
    )

    call_args = adapter_mock.execute.call_args_list
    post_calls = call_args[2][0][0]
    assert len(post_calls) == 1
    assert (
        post_calls[0].sql(dialect="postgres")
        == f'CREATE INDEX IF NOT EXISTS "test_idx" ON "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}__dev" /* test_schema.test_model */("a")'
    )

    on_virtual_update_calls = call_args[4][0][0]
    assert (
        on_virtual_update_calls[0].sql(dialect="postgres")
        == 'GRANT SELECT ON VIEW "test_schema__test_env"."test_model" /* test_schema.test_model */ TO ROLE "admin"'
    )
    assert (
        on_virtual_update_calls[1].sql(dialect="postgres")
        == "GRANT REFERENCES, SELECT ON FUTURE VIEWS IN DATABASE demo_db TO ROLE owner_name"
    )

    # Validation that within the macro the environment specific view is used
    assert (
        on_virtual_update_calls[2].sql(dialect="postgres")
        == 'CREATE OR REPLACE TABLE "log_table" AS SELECT \'"test_schema__test_env"."test_model" /* test_schema.test_model */\' AS "fqn_this_model", \'"test_schema__test_env"."test_model"\' AS "eval_this_model"'
    )


def test_on_virtual_update_python_model_macro(mocker: MockerFixture, adapter_mock, make_snapshot):
    evaluator = SnapshotEvaluator(adapter_mock)

    @macro()
    def create_index_2(
        evaluator: MacroEvaluator,
        index_name: str,
        model_name: str,
        column: str,
    ):
        return f"CREATE INDEX IF NOT EXISTS {index_name} ON {model_name}({column});"

    @model(
        "db.test_model_3",
        kind="full",
        columns={"id": "string", "name": "string"},
        on_virtual_update=["@CREATE_INDEX_2('idx', 'db.test_model_3', id)"],
    )
    def model_with_statements(context, **kwargs):
        return pd.DataFrame(
            [
                {
                    "id": context.var("1"),
                    "name": context.var("var"),
                }
            ]
        )

    python_model = model.get_registry()["db.test_model_3"].model(
        module_path=Path("."),
        path=Path("."),
        macros=macro.get_registry(),
        dialect="postgres",
    )

    assert len(python_model.python_env) == 3
    assert len(python_model.on_virtual_update) == 1
    assert isinstance(python_model.python_env["create_index_2"], Executable)
    assert isinstance(python_model.on_virtual_update[0], MacroFunc)

    snapshot = make_snapshot(python_model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, DeployabilityIndex.none_deployable())

    snapshots = {snapshot.name: snapshot}
    environment_naming_info = EnvironmentNamingInfo(name="prod")
    evaluator.promote(
        [snapshot],
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots=snapshots,
        environment_naming_info=environment_naming_info,
        table_mapping=to_view_mapping(
            snapshots.values(),
            environment_naming_info,
        ),
    )

    call_args = adapter_mock.execute.call_args_list
    on_virtual_update_call = call_args[4][0][0][0]
    assert (
        on_virtual_update_call.sql(dialect="postgres")
        == 'CREATE INDEX IF NOT EXISTS "idx" ON "db"."test_model_3" /* db.test_model_3 */("id")'
    )


def test_evaluate_incremental_by_partition(mocker: MockerFixture, make_snapshot, adapter_mock):
    model = SqlModel(
        name="test_schema.test_model",
        query=parse_one("SELECT 1, ds, b FROM tbl_a"),
        kind=IncrementalByPartitionKind(),
        partitioned_by=["ds", "b"],
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.columns.return_value = model.columns_to_types

    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    # uses `replace_query` on first model execution
    adapter_mock.replace_query.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        partitioned_by=[
            exp.to_column("ds", quoted=True),
            exp.to_column("b", quoted=True),
        ],
        target_columns_to_types=model.columns_to_types,
        clustered_by=[],
        table_properties={},
        column_descriptions={},
        partition_interval_unit=None,
        storage_format=None,
        table_description=None,
        table_format=None,
        source_columns=None,
    )

    adapter_mock.reset_mock()
    snapshot.intervals = [(to_timestamp("2020-01-01"), to_timestamp("2020-01-02"))]

    evaluator.evaluate(
        snapshot,
        start="2020-01-02",
        end="2020-01-03",
        execution_time="2020-01-03",
        snapshots={},
    )

    # uses `insert_overwrite_by_partition` on all subsequent model executions
    adapter_mock.insert_overwrite_by_partition.assert_called_once_with(
        snapshot.table_name(),
        model.render_query(),
        partitioned_by=[
            exp.to_column("ds", quoted=True),
            exp.to_column("b", quoted=True),
        ],
        target_columns_to_types=model.columns_to_types,
        source_columns=None,
    )


def test_custom_materialization_strategy(adapter_mock, make_snapshot):
    custom_insert_kind = None
    custom_insert_query_or_df = None

    class TestCustomMaterializationStrategy(CustomMaterialization):
        NAME = "custom_materialization_test"

        def insert(
            self,
            table_name: str,
            query_or_df: QueryOrDF,
            model: Model,
            is_first_insert: bool,
            render_kwargs: t.Dict[str, t.Any],
            **kwargs: t.Any,
        ) -> None:
            nonlocal custom_insert_kind
            nonlocal custom_insert_query_or_df

            custom_insert_kind = model.kind
            custom_insert_query_or_df = query_or_df

    evaluator = SnapshotEvaluator(adapter_mock)
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind CUSTOM (
                    materialization 'custom_materialization_test',
                    materialization_properties (
                        'test_property' = 'test_value'
                    )
                )
            );

            SELECT * FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    assert custom_insert_kind
    assert isinstance(custom_insert_kind, CustomKind)
    assert model.custom_materialization_properties == {"test_property": "test_value"}

    assert isinstance(custom_insert_query_or_df, exp.Query)
    assert custom_insert_query_or_df.sql() == 'SELECT * FROM "tbl" AS "tbl"'


def test_custom_materialization_strategy_with_custom_properties(adapter_mock, make_snapshot):
    custom_insert_kind = None

    class TestCustomKind(CustomKind):
        _primary_key: t.List[exp.Expr]  # type: ignore[no-untyped-def]

        @model_validator(mode="after")
        def _validate_model(self) -> Self:
            self._primary_key = list_of_fields_validator(
                self.materialization_properties.get("primary_key"), {}
            )
            if not self.primary_key:
                raise ConfigError("primary_key must be specified")
            return self

        @property
        def primary_key(self) -> t.List[exp.Expr]:
            return self._primary_key

    class TestCustomMaterializationStrategy(CustomMaterialization[TestCustomKind]):
        NAME = "custom_materialization_test_1"

        def insert(
            self,
            table_name: str,
            query_or_df: QueryOrDF,
            model: Model,
            is_first_insert: bool,
            render_kwargs: t.Dict[str, t.Any],
            **kwargs: t.Any,
        ) -> None:
            nonlocal custom_insert_kind
            custom_insert_kind = model.kind

    evaluator = SnapshotEvaluator(adapter_mock)

    with pytest.raises(ConfigError, match=r"primary_key must be specified"):
        model = load_sql_based_model(
            parse(  # type: ignore
                """
                MODEL (
                    name test_schema.test_model,
                    kind CUSTOM (
                        materialization 'custom_materialization_test_1',
                    )
                );

                SELECT * FROM tbl;
                """
            )
        )

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind CUSTOM (
                    materialization 'custom_materialization_test_1',
                    materialization_properties (
                        primary_key = id
                    )
                )
            );

            SELECT * FROM tbl;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    assert custom_insert_kind
    assert isinstance(custom_insert_kind, TestCustomKind)
    assert custom_insert_kind.primary_key == [exp.column("id", quoted=True)]
    assert model.custom_materialization_properties["primary_key"]

    # show that the _primary_key property is transient
    as_json = json.loads(model.json())
    assert "primary_key" not in as_json["kind"]
    assert "_primary_key" not in as_json["kind"]


def test_custom_materialization_strategy_with_custom_kind_must_be_correct_type():
    # note: deliberately doesnt extend CustomKind
    class TestCustomKind:
        pass

    class TestCustomMaterializationStrategy(CustomMaterialization[TestCustomKind]):  # type: ignore
        NAME = "custom_materialization_test_2"

    model = load_sql_based_model(
        parse(  # type: ignore
            """
                MODEL (
                    name test_schema.test_model,
                    kind CUSTOM (
                        materialization 'custom_materialization_test_2',
                    )
                );

                SELECT * FROM tbl;
                """
        )
    )

    with pytest.raises(
        SQLMeshError, match=r"kind 'TestCustomKind' must be a subclass of CustomKind"
    ):
        model.validate_definition()


def test_create_managed(adapter_mock, make_snapshot, mocker: MockerFixture):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind MANAGED,
                physical_properties (
                    warehouse = 'small',
                    target_lag = '10 minutes'
                ),
                clustered_by a
            );

            select a, b from foo;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.all_deployable())

    adapter_mock.create_managed_table.assert_called_with(
        table_name=snapshot.table_name(),
        query=mocker.ANY,
        target_columns_to_types=model.columns_to_types,
        partitioned_by=model.partitioned_by,
        clustered_by=model.clustered_by,
        table_properties=model.physical_properties,
        table_description=model.description,
        column_descriptions=model.column_descriptions,
        table_format=None,
    )


def test_create_managed_dev(adapter_mock, make_snapshot, mocker: MockerFixture):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind MANAGED,
                physical_properties (
                    warehouse = 'small',
                    target_lag = '10 minutes'
                ),
                clustered_by a
            );

            select a, b from foo;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, deployability_index=DeployabilityIndex.none_deployable())

    adapter_mock.ctas.assert_called_once_with(
        f"{snapshot.table_name()}__dev",
        mocker.ANY,
        model.columns_to_types,
        table_format=model.table_format,
        storage_format=model.storage_format,
        partitioned_by=model.partitioned_by,
        partition_interval_unit=model.partition_interval_unit,
        clustered_by=model.clustered_by,
        table_properties=model.physical_properties,
        table_description=None,
        column_descriptions=None,
    )


def test_evaluate_managed(adapter_mock, make_snapshot, mocker: MockerFixture):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind MANAGED,
                physical_properties (
                    warehouse = 'small',
                    target_lag = '10 minutes'
                ),
                clustered_by a
            );

            select a, b from foo;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # insert() on the evaluation strategy with a deployable snapshot is a no-op because the table data is already loaded
    # when the managed table is created
    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
        deployability_index=DeployabilityIndex.all_deployable(),
    )

    adapter_mock.create_managed_table.assert_not_called()
    adapter_mock.replace_table.assert_not_called()
    adapter_mock.ctas.assert_not_called()

    # insert() on the evaluation strategy with a non-deployable snapshot causes the temp table to be updated
    adapter_mock.reset_mock()
    adapter_mock.assert_not_called()

    table_colmns = {
        "a": exp.DataType.build("int"),
        "b": exp.DataType.build("string"),
    }
    adapter_mock.columns.return_value = table_colmns

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
        deployability_index=DeployabilityIndex.none_deployable(),
    )

    adapter_mock.create_managed_table.assert_not_called()
    adapter_mock.replace_query.assert_called_with(
        snapshot.table_name(is_deployable=False),
        mocker.ANY,
        target_columns_to_types=table_colmns,
        table_format=model.table_format,
        storage_format=model.storage_format,
        partitioned_by=model.partitioned_by,
        partition_interval_unit=model.partition_interval_unit,
        clustered_by=model.clustered_by,
        table_properties=model.physical_properties,
        table_description=model.description,
        column_descriptions=model.column_descriptions,
        source_columns=None,
    )
    adapter_mock.columns.assert_called_once_with(snapshot.table_name(is_deployable=False))


def test_cleanup_managed(adapter_mock, make_snapshot, mocker: MockerFixture):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind MANAGED,
                physical_properties (
                    warehouse = 'small',
                    target_lag = '10 minutes'
                ),
                clustered_by a
            );

            select a, b from foo;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.assert_not_called()

    cleanup_task = SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=False)

    evaluator.cleanup(target_snapshots=[cleanup_task])

    physical_name = f"sqlmesh__test_schema.test_schema__test_model__{snapshot.version}"
    adapter_mock.drop_table.assert_called_once_with(f"{physical_name}__dev")
    adapter_mock.drop_managed_table.assert_called_once_with(f"{physical_name}")


def test_create_managed_forward_only_with_previous_version_doesnt_clone_for_dev_preview(
    adapter_mock, make_snapshot
):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind MANAGED
            );

            select a, b from foo;
            """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            change_category=SnapshotChangeCategory.BREAKING,
            dev_table_suffix="dev",
        ),
    )

    evaluator.create(
        target_snapshots=[snapshot],
        snapshots={},
        deployability_index=DeployabilityIndex.none_deployable(),
    )

    # We dont clone managed tables to create dev previews, we use normal tables
    adapter_mock.clone_table.assert_not_called()
    adapter_mock.create_managed_table.assert_not_called()
    adapter_mock.create_table.assert_not_called()

    # The table gets created using ctas() because the model column types arent known
    adapter_mock.ctas.assert_called_once()
    assert adapter_mock.ctas.call_args_list[0].args[0] == snapshot.table_name(is_deployable=False)


def test_migrate_snapshot(snapshot: Snapshot, mocker: MockerFixture, adapter_mock, make_snapshot):
    adapter_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter")
    adapter_mock.dialect = "duckdb"
    adapter_mock.with_settings.return_value = adapter_mock

    evaluator = SnapshotEvaluator(adapter_mock)
    evaluator.create([snapshot], {})

    updated_model_dict = snapshot.model.dict()
    updated_model_dict["query"] = "SELECT a::int, b::int FROM tbl"
    updated_model = SqlModel.parse_obj(updated_model_dict)

    new_snapshot = make_snapshot(updated_model)
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    new_snapshot.previous_versions = snapshot.all_versions
    new_snapshot.version = snapshot.version

    assert new_snapshot.table_name() == snapshot.table_name()

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            schema="sqlmesh__db",
            name=f"db__model__{new_snapshot.version}",
            type=DataObjectType.TABLE,
        )
    ]
    adapter_mock.drop_data_object_on_type_mismatch.return_value = False

    evaluator.migrate([new_snapshot], {})

    common_kwargs: t.Dict[str, t.Any] = dict(
        table_format=None,
        storage_format=None,
        partitioned_by=[],
        partition_interval_unit=None,
        clustered_by=[],
        table_properties={},
        table_description=None,
    )

    adapter_mock.create_table.assert_has_calls(
        [
            call(
                new_snapshot.table_name(),
                target_columns_to_types={"a": exp.DataType.build("int")},
                column_descriptions={},
                **common_kwargs,
            ),
            call(
                f"{new_snapshot.table_name()}_schema_tmp",
                target_columns_to_types={
                    "a": exp.DataType.build("int"),
                    "b": exp.DataType.build("int"),
                },
                column_descriptions=None,
                **common_kwargs,
            ),
        ]
    )

    adapter_mock.fetchall.assert_has_calls(
        [
            call(
                parse_one('SELECT CAST("a" AS INT) AS "a" FROM "tbl" AS "tbl" WHERE FALSE LIMIT 0')
            ),
            call(
                parse_one(
                    'SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM "tbl" AS "tbl" WHERE FALSE LIMIT 0'
                )
            ),
        ]
    )

    adapter_mock.get_alter_operations.assert_called_once_with(
        snapshot.table_name(),
        f"{new_snapshot.table_name()}_schema_tmp",
        ignore_destructive=False,
        ignore_additive=False,
    )


def test_migrate_only_processes_target_snapshots(
    mocker: MockerFixture, adapter_mock, make_snapshot
):
    evaluator = SnapshotEvaluator(adapter_mock)

    target_model = SqlModel(
        name="test_schema.target_model",
        kind=FullKind(),
        query=parse_one("SELECT 1 AS a"),
    )
    extra_model = SqlModel(
        name="test_schema.extra_model",
        kind=FullKind(),
        query=parse_one("SELECT 1 AS a"),
    )

    target_snapshot = make_snapshot(target_model)
    extra_snapshot = make_snapshot(extra_model)
    target_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    extra_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    target_snapshots = [target_snapshot]
    snapshots = {
        target_snapshot.snapshot_id: target_snapshot,
        extra_snapshot.snapshot_id: extra_snapshot,
    }

    mocker.patch.object(
        evaluator,
        "_get_data_objects",
        return_value={target_snapshot.snapshot_id: mocker.Mock()},
    )
    migrate_mock = mocker.patch.object(evaluator, "_migrate_snapshot")

    def apply_side_effect(snapshot_iterable, fn, *_args, **_kwargs):
        for snapshot in snapshot_iterable:
            fn(snapshot)
        return ([], [])

    apply_mock = mocker.patch(
        "sqlmesh.core.snapshot.evaluator.concurrent_apply_to_snapshots",
        side_effect=apply_side_effect,
    )

    evaluator.migrate(target_snapshots=target_snapshots, snapshots=snapshots)

    assert apply_mock.call_count == 1
    called_snapshots = list(apply_mock.call_args.args[0])
    assert called_snapshots == target_snapshots

    migrate_mock.assert_called_once()
    called_snapshot, snapshots_by_name, *_ = migrate_mock.call_args.args
    assert called_snapshot is target_snapshot
    assert target_snapshot.name in snapshots_by_name
    assert extra_snapshot.name in snapshots_by_name


def test_migrate_managed(adapter_mock, make_snapshot, mocker: MockerFixture):
    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind MANAGED
            );

            select a, b from foo;
            """
        )
    )
    snapshot: Snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot.previous_versions = snapshot.all_versions

    adapter_mock.get_data_objects.return_value = [
        DataObject(
            schema="sqlmesh__test_schema",
            name=f"test_schema__test_model__{snapshot.version}",
            type=DataObjectType.MANAGED_TABLE,
        )
    ]
    adapter_mock.drop_data_object_on_type_mismatch.return_value = False

    # no schema changes - no-op
    adapter_mock.get_alter_operations.return_value = []
    evaluator.migrate(
        target_snapshots=[snapshot],
        snapshots={},
    )

    adapter_mock.create_table.assert_not_called()
    adapter_mock.create_managed_table.assert_not_called()
    adapter_mock.ctas.assert_called_once()
    adapter_mock.reset_mock()

    # schema changes - exception thrown
    adapter_mock.get_alter_operations.return_value = [exp.Alter()]

    with pytest.raises(NodeExecutionFailedError) as ex:
        evaluator.migrate(
            target_snapshots=[snapshot],
            snapshots={},
        )

    sqlmesh_err = ex.value.__cause__
    assert isinstance(sqlmesh_err, SQLMeshError)
    assert re.match(
        "The schema of the managed model '.*?' cannot be updated in a forward-only fashion",
        str(sqlmesh_err),
    )

    adapter_mock.create_table.assert_not_called()
    adapter_mock.ctas.assert_called_once()
    adapter_mock.create_managed_table.assert_not_called()


def test_multiple_engine_creation(snapshot: Snapshot, adapters, make_snapshot):
    engine_adapters = {"default": adapters[0], "secondary": adapters[1], "third": adapters[2]}
    evaluator = SnapshotEvaluator(engine_adapters)

    assert len(evaluator.adapters) == 3
    assert evaluator.adapter == engine_adapters["default"]
    assert evaluator.get_adapter() == engine_adapters["default"]
    assert evaluator.get_adapter("third") == engine_adapters["third"]
    assert evaluator.get_adapter("secondary") == engine_adapters["secondary"]

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
                gateway secondary,
                dialect postgres,
            );
            CREATE INDEX IF NOT EXISTS test_idx ON test_schema.test_model(a);
            SELECT a::int FROM tbl;
            CREATE INDEX IF NOT EXISTS test_idx ON test_schema.test_model(a);
            """
        ),
    )

    snapshot_2 = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_2.categorize_as(SnapshotChangeCategory.BREAKING)
    expected_call = f'CREATE INDEX IF NOT EXISTS "test_idx" ON "sqlmesh__test_schema"."test_schema__test_model__{snapshot_2.version}" /* test_schema.test_model */("a" NULLS FIRST)'
    evaluator.create([snapshot_2, snapshot], {}, DeployabilityIndex.all_deployable())

    # Default gateway adapter
    create_args = engine_adapters["default"].create_table.call_args_list
    assert len(create_args) == 1
    assert create_args[0][0] == (f"sqlmesh__db.db__model__{snapshot.version}",)

    # Secondary gateway for gateway-specicied model
    create_args_2 = engine_adapters["secondary"].create_table.call_args_list
    assert len(create_args_2) == 1
    assert create_args_2[0][0] == (
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot_2.version}",
    )

    engine_adapters["third"].create_table.assert_not_called()
    evaluator.promote([snapshot, snapshot_2], EnvironmentNamingInfo(name="test_env"))

    # Virtual layer will use the default adapter
    engine_adapters["secondary"].create_view.assert_not_called()
    engine_adapters["third"].create_view.assert_not_called()
    view_args = engine_adapters["default"].create_view.call_args_list
    assert len(view_args) == 2
    assert view_args[0][0][0] == "db__test_env.model"
    assert view_args[1][0][0] == "test_schema__test_env.test_model"

    call_args = engine_adapters["secondary"].execute.call_args_list
    pre_calls = call_args[1][0][0]
    assert len(pre_calls) == 1
    assert pre_calls[0].sql(dialect="postgres") == expected_call

    post_calls = call_args[2][0][0]
    assert len(post_calls) == 1
    assert post_calls[0].sql(dialect="postgres") == expected_call

    evaluator.demote([snapshot_2], EnvironmentNamingInfo(name="test_env"))
    engine_adapters["secondary"].drop_view.assert_not_called()
    engine_adapters["default"].drop_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        cascade=False,
    )


def test_multiple_engine_promotion(mocker: MockerFixture, adapter_mock, make_snapshot):
    connection_mock = mocker.NonCallableMock()
    cursor_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock
    adapter = EngineAdapter(lambda: connection_mock, "")
    adapter.with_settings = lambda **kwargs: adapter  # type: ignore
    adapter._get_data_objects = lambda *args, **kwargs: []  # type: ignore
    engine_adapters = {"default": adapter_mock, "secondary": adapter}

    def columns(table_name):
        return {
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("timestamp"),
        }

    adapter.columns = columns  # type: ignore

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        gateway="secondary",
        query=parse_one("SELECT a FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )

    evaluator = SnapshotEvaluator(engine_adapters)
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-02",
        execution_time="2020-01-02",
        snapshots={},
    )

    evaluator.promote([snapshot], EnvironmentNamingInfo(name="test_env"))

    # Verify that the model was evaluated using the gateway specific adapter "secondary"
    cursor_mock.execute.assert_has_calls(
        [
            call(
                f'DELETE FROM "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}" WHERE "ds" BETWEEN CAST(\'2020-01-01 00:00:00\' AS TIMESTAMP) AND CAST(\'2020-01-02 23:59:59.999999\' AS TIMESTAMP)'
            ),
            call(
                f'INSERT INTO "sqlmesh__test_schema"."test_schema__test_model__{snapshot.version}" ("a", "ds") SELECT "a", "ds" FROM (SELECT "a" AS "a" FROM "tbl" AS "tbl" WHERE "ds" BETWEEN \'2020-01-01\' AND \'2020-01-02\') AS "_subquery" WHERE "ds" BETWEEN CAST(\'2020-01-01 00:00:00\' AS TIMESTAMP) AND CAST(\'2020-01-02 23:59:59.999999\' AS TIMESTAMP)'
            ),
        ]
    )

    # Verify that the snapshot was promoted using the default adapter "default" (adapter_mock in this case)
    adapter_mock.create_schema.assert_called_once_with(schema_("test_schema__test_env"))
    adapter_mock.create_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        parse_one(
            f"SELECT * FROM sqlmesh__test_schema.test_schema__test_model__{snapshot.version}"
        ),
        table_description=None,
        column_descriptions=None,
        view_properties={},
    )


def test_multiple_engine_migration(
    mocker: MockerFixture, adapter_mock, make_snapshot, make_mocked_engine_adapter
):
    adapter_one = make_mocked_engine_adapter(EngineAdapter)
    adapter_one.with_settings = lambda **kwargs: adapter_one  # type: ignore
    adapter_two = adapter_mock
    adapter_two.with_settings.return_value = adapter_two
    engine_adapters = {"one": adapter_one, "two": adapter_two}

    model = SqlModel(
        name="test_schema.test_model",
        kind=IncrementalByTimeRangeKind(
            time_column="a", on_destructive_change=OnDestructiveChange.ALLOW
        ),
        query=parse_one("SELECT c FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot_1 = make_snapshot(model, version="1")
    snapshot_1.change_category = SnapshotChangeCategory.BREAKING
    snapshot_1.forward_only = True
    snapshot_1.previous_versions = snapshot_1.all_versions
    model_2 = SqlModel(
        name="test_schema.test_model_2",
        kind=IncrementalByTimeRangeKind(
            time_column="a", on_destructive_change=OnDestructiveChange.ALLOW
        ),
        gateway="two",
        query=parse_one("SELECT c FROM tbl WHERE ds BETWEEN @start_ds and @end_ds"),
    )
    snapshot_2 = make_snapshot(model_2, version="1")
    snapshot_2.change_category = SnapshotChangeCategory.BREAKING
    snapshot_2.forward_only = True
    snapshot_2.previous_versions = snapshot_2.all_versions

    def columns(table_name):
        if table_name == snapshot_1.table_name():
            return {
                "c": exp.DataType.build("int"),
                "b": exp.DataType.build("int"),
            }
        return {
            "c": exp.DataType.build("int"),
            "a": exp.DataType.build("int"),
        }

    adapter_two.columns.side_effect = columns
    adapter_two.drop_data_object_on_type_mismatch.return_value = False

    mocker.patch.object(adapter_one, "columns", side_effect=columns)
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.get_data_objects",
        return_value=[
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model__{snapshot_1.version}",
                type=DataObjectType.TABLE,
            ),
            DataObject(
                schema="sqlmesh__test_schema",
                name=f"test_schema__test_model_2__{snapshot_2.version}",
                type=DataObjectType.TABLE,
            ),
        ],
    )

    evaluator = SnapshotEvaluator(engine_adapters)
    evaluator.migrate([snapshot_1, snapshot_2], {})

    adapter_one.cursor.execute.assert_has_calls(
        [
            call('ALTER TABLE "sqlmesh__test_schema"."test_schema__test_model__1" DROP COLUMN "b"'),
            call(
                'ALTER TABLE "sqlmesh__test_schema"."test_schema__test_model__1" ADD COLUMN "a" INT'
            ),
        ]
    )

    # The second mock adapter has to be called only for the gateway-specific model
    adapter_mock.get_alter_operations.assert_called_once_with(
        snapshot_2.table_name(True),
        f"{snapshot_2.table_name(True)}_schema_tmp",
        ignore_destructive=False,
        ignore_additive=False,
    )


def test_multiple_engine_cleanup(snapshot: Snapshot, adapters, make_snapshot):
    engine_adapters = {"default": adapters[0], "secondary": adapters[1]}
    evaluator = SnapshotEvaluator(engine_adapters)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
                gateway secondary,
            );
            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot_2 = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_2.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.create([snapshot_2, snapshot], {}, DeployabilityIndex.all_deployable())

    assert engine_adapters["default"].create_table.call_args_list[0][0] == (
        f"sqlmesh__db.db__model__{snapshot.version}",
    )
    assert engine_adapters["secondary"].create_table.call_args_list[0][0] == (
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot_2.version}",
    )

    evaluator.cleanup(
        [
            SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True),
            SnapshotTableCleanupTask(snapshot=snapshot_2.table_info, dev_table_only=True),
        ],
    )

    # The clean up will happen using the specific gateway the model was created with
    engine_adapters["default"].drop_table.assert_called_once_with(
        f"sqlmesh__db.db__model__{snapshot.version}__dev", cascade=True
    )
    engine_adapters["secondary"].drop_table.assert_called_once_with(
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot_2.version}__dev", cascade=True
    )


def test_cleanup_skips_unavailable_gateway(snapshot: Snapshot, adapters, make_snapshot):
    engine_adapters = {"default": adapters[0]}
    evaluator = SnapshotEvaluator(engine_adapters)

    model_with_missing_gw = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
                gateway nonexistent_gateway,
            );
            SELECT a::int FROM tbl;
            """
        ),
    )

    snapshot_missing_gw = make_snapshot(model_with_missing_gw)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_missing_gw.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, DeployabilityIndex.all_deployable())

    evaluator.cleanup(
        [
            SnapshotTableCleanupTask(snapshot=snapshot.table_info, dev_table_only=True),
            SnapshotTableCleanupTask(snapshot=snapshot_missing_gw.table_info, dev_table_only=True),
        ],
    )

    engine_adapters["default"].drop_table.assert_called_once_with(
        f"sqlmesh__db.db__model__{snapshot.version}__dev", cascade=True
    )


def test_multi_engine_python_model_with_macros(adapters, make_snapshot):
    engine_adapters = {"default": adapters[0], "secondary": adapters[1]}
    evaluator = SnapshotEvaluator(engine_adapters)

    @macro()
    def validate_engine_call(
        evaluator: MacroEvaluator,
    ):
        if evaluator.runtime_stage == "creating":
            # To validate the model-specified gateway is used for the macro evaluator
            evaluator.engine_adapter.get_catalog_type()
            return None

    @model(
        "db.multi_engine_test_model",
        kind="full",
        gateway="secondary",
        columns={"id": "string", "name": "string"},
        pre_statements=["@VALIDATE_ENGINE_CALL()"],
        post_statements=["@VALIDATE_ENGINE_CALL()"],
    )
    def model_with_statements(context, **kwargs):
        return pd.DataFrame(
            [
                {
                    "id": context.var("1"),
                    "name": context.var("var"),
                }
            ]
        )

    python_model = model.get_registry()["db.multi_engine_test_model"].model(
        module_path=Path("."),
        path=Path("."),
        macros=macro.get_registry(),
        dialect="postgres",
    )

    assert len(python_model.python_env) == 3
    assert isinstance(python_model.python_env["validate_engine_call"], Executable)

    snapshot = make_snapshot(python_model)
    assert snapshot.model_gateway == "secondary"
    assert evaluator.adapter == engine_adapters["default"]
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {}, DeployabilityIndex.all_deployable())

    # Validate model-specific gateway usage during table creation
    create_args = engine_adapters["secondary"].create_table.call_args_list
    assert len(create_args) == 1
    assert create_args[0][0] == (f"sqlmesh__db.db__multi_engine_test_model__{snapshot.version}",)

    environment_naming_info = EnvironmentNamingInfo(name="test_env")
    evaluator.promote([snapshot], environment_naming_info)

    # Verify that the default gateway creates the view for the virtual layer
    engine_adapters["secondary"].create_view.assert_not_called()
    view_args = engine_adapters["default"].create_view.call_args_list
    assert len(view_args) == 1
    assert view_args[0][0][0] == "db__test_env.multi_engine_test_model"

    # For the pre/post statements verify the model-specific gateway was used
    engine_adapters["default"].execute.assert_called_once()
    assert len(engine_adapters["secondary"].execute.call_args_list) == 4

    # Validate that the get_catalog_type method was called only on the secondary engine from the macro evaluator
    engine_adapters["default"].get_catalog_type.assert_not_called()
    assert len(engine_adapters["secondary"].get_catalog_type.call_args_list) == 2

    evaluator.demote([snapshot], environment_naming_info)
    engine_adapters["default"].drop_view.assert_called_once_with(
        "db__test_env.multi_engine_test_model",
        cascade=False,
    )

    environment_naming_info_gw = EnvironmentNamingInfo(name="test_env", gateway_managed=True)
    # Validate that promoting with gateway_managed leads to this gateway being used for virtual layer
    evaluator.promote([snapshot], environment_naming_info_gw)
    view_args = engine_adapters["secondary"].create_view.call_args_list
    assert len(view_args) == 1
    assert view_args[0][0][0] == "db__test_env.multi_engine_test_model"

    # Similarly for demotion
    evaluator.demote([snapshot], environment_naming_info_gw)
    engine_adapters["secondary"].drop_view.assert_called_once_with(
        "db__test_env.multi_engine_test_model",
        cascade=False,
    )


def test_multiple_engine_virtual_layer(snapshot: Snapshot, adapters, make_snapshot):
    engine_adapters = {"default": adapters[0], "secondary": adapters[1], "third": adapters[2]}
    evaluator = SnapshotEvaluator(engine_adapters)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind FULL,
                gateway secondary,
                dialect postgres,
            );
            SELECT a::int FROM tbl;
            CREATE INDEX IF NOT EXISTS test_idx ON test_schema.test_model(a);
            """
        ),
    )

    snapshot_2 = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_2.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.create([snapshot_2, snapshot], {}, DeployabilityIndex.all_deployable())

    # Default gateway adapter to create table without gateway
    create_args = engine_adapters["default"].create_table.call_args_list
    assert len(create_args) == 1
    assert create_args[0][0] == (f"sqlmesh__db.db__model__{snapshot.version}",)

    # Secondary gateway for gateway-specicied model
    create_args_2 = engine_adapters["secondary"].create_table.call_args_list
    assert len(create_args_2) == 1
    assert create_args_2[0][0] == (
        f"sqlmesh__test_schema.test_schema__test_model__{snapshot_2.version}",
    )

    environment_naming_info = EnvironmentNamingInfo(name="test_env", gateway_managed=True)
    engine_adapters["third"].create_table.assert_not_called()
    evaluator.promote([snapshot, snapshot_2], environment_naming_info)

    # Virtual layer will use the model-specified gateway adapter for the second model and default otherwise
    view_args_default = engine_adapters["default"].create_view.call_args_list
    engine_adapters["third"].create_view.assert_not_called()
    view_args_secondary = engine_adapters["secondary"].create_view.call_args_list

    assert len(view_args_default) == 1
    assert view_args_default[0][0][0] == "db__test_env.model"
    assert len(view_args_secondary) == 1
    assert view_args_secondary[0][0][0] == "test_schema__test_env.test_model"

    # Demotion will follow with the same pattern
    evaluator.demote([snapshot_2, snapshot], environment_naming_info)
    engine_adapters["default"].drop_view.assert_called_once_with(
        "db__test_env.model",
        cascade=False,
    )
    engine_adapters["secondary"].drop_view.assert_called_once_with(
        "test_schema__test_env.test_model",
        cascade=False,
    )


def test_wap_basic(
    adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot], mocker: MockerFixture
) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.wap_supported.return_value = True

    expected_wap_table = "test_schema.test_table.branch_wap_12345678"
    adapter_mock.wap_prepare.return_value = expected_wap_table

    wap_id = evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots={},
        target_table_exists=True,  # Use parameter to control table existence
    )

    assert wap_id is not None
    assert len(wap_id) == 8

    expected_table_name = snapshot.table_name()
    adapter_mock.wap_prepare.assert_called_once_with(expected_table_name, wap_id)
    adapter_mock.replace_query.assert_called_once_with(
        expected_wap_table,
        mocker.ANY,
        table_format=mocker.ANY,
        storage_format=mocker.ANY,
        partitioned_by=mocker.ANY,
        partition_interval_unit=mocker.ANY,
        clustered_by=mocker.ANY,
        table_properties=mocker.ANY,
        table_description=mocker.ANY,
        column_descriptions=mocker.ANY,
        target_columns_to_types=mocker.ANY,
        source_columns=mocker.ANY,
    )


def test_wap_model_wap_supported(
    adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot], mocker: MockerFixture
) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
        storage_format="iceberg",  # Model supports WAP via iceberg format
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.wap_supported.return_value = False

    expected_wap_table = "test_schema.test_table.branch_wap_12345678"
    adapter_mock.wap_prepare.return_value = expected_wap_table

    wap_id = evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots={},
        target_table_exists=True,  # Use parameter to control table existence
    )
    assert wap_id is not None

    expected_table_name = snapshot.table_name()
    adapter_mock.wap_prepare.assert_called_once_with(expected_table_name, wap_id)
    adapter_mock.replace_query.assert_called_once_with(
        expected_wap_table,
        mocker.ANY,
        table_format=mocker.ANY,
        storage_format=mocker.ANY,
        partitioned_by=mocker.ANY,
        partition_interval_unit=mocker.ANY,
        clustered_by=mocker.ANY,
        table_properties=mocker.ANY,
        table_description=mocker.ANY,
        column_descriptions=mocker.ANY,
        target_columns_to_types=mocker.ANY,
        source_columns=mocker.ANY,
    )


def test_wap_no_wap_support(adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.wap_supported.return_value = False

    wap_id = evaluator.evaluate(
        snapshot,
        start="2020-01-01",
        end="2020-01-01",
        execution_time="2020-01-01",
        snapshots={},
        target_table_exists=True,
    )

    assert wap_id is None
    adapter_mock.wap_prepare.assert_not_called()


def test_wap_non_materialized_snapshot(
    adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]
) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=ViewKind(),  # View kind is not materialized
        query=parse_one("SELECT a::int FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.wap_supported.return_value = True

    wap_id = evaluator.evaluate(
        snapshot, start="2020-01-01", end="2020-01-01", execution_time="2020-01-01", snapshots={}
    )

    assert wap_id is None
    adapter_mock.wap_prepare.assert_not_called()


def test_wap_publish_snapshot(adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    wap_id = "test_wap_id"
    deployability_index = DeployabilityIndex.all_deployable()

    evaluator.wap_publish_snapshot(snapshot, wap_id, deployability_index)

    expected_table_name = snapshot.table_name(is_deployable=True)
    adapter_mock.wap_publish.assert_called_once_with(expected_table_name, wap_id)


def test_wap_during_audit(adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    custom_audit = ModelAudit(
        name="custom_audit",
        query="SELECT * FROM test_schema.test_table WHERE invalid_condition",
    )

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
        audits=[
            ("not_null", {"columns": exp.to_column("a")}),
            ("custom_audit", {}),
        ],
        audit_definitions={custom_audit.name: custom_audit},
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    wap_id = "test_wap_id"
    expected_wap_table_name = f"test_schema.test_table.branch_wap_{wap_id}"
    adapter_mock.wap_table_name.return_value = expected_wap_table_name
    adapter_mock.fetchone.return_value = (0,)

    results = evaluator.audit(snapshot, snapshots={}, wap_id=wap_id)

    assert len(results) == 2

    adapter_mock.wap_table_name.assert_called_once_with(snapshot.table_name(), wap_id)
    adapter_mock.wap_publish.assert_called_once_with(snapshot.table_name(), wap_id)


def test_wap_prepare_failure(adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]) -> None:
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    adapter_mock.wap_supported.return_value = True

    adapter_mock.wap_prepare.side_effect = Exception("WAP prepare failed")

    with pytest.raises(Exception, match="WAP prepare failed"):
        evaluator.evaluate(
            snapshot,
            start="2020-01-01",
            end="2020-01-01",
            execution_time="2020-01-01",
            snapshots={},
            target_table_exists=True,
        )


def test_wap_publish_failure(adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]) -> None:
    """Test error handling when WAP publish fails."""
    evaluator = SnapshotEvaluator(adapter_mock)

    model = SqlModel(
        name="test_schema.test_table",
        kind=FullKind(),
        query=parse_one("SELECT a::int FROM tbl"),
        audits=[("not_null", {"columns": exp.to_column("a")})],
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    wap_id = "test_wap_id"
    expected_wap_table_name = f"test_schema.test_table.branch_wap_{wap_id}"
    adapter_mock.wap_table_name.return_value = expected_wap_table_name
    adapter_mock.fetchone.return_value = (0,)

    # Mock WAP publish to raise an exception
    adapter_mock.wap_publish.side_effect = Exception("WAP publish failed")

    # Execute audit with WAP ID and expect it to raise the exception
    with pytest.raises(Exception, match="WAP publish failed"):
        evaluator.audit(snapshot, snapshots={}, wap_id=wap_id)


def test_properties_are_preserved_in_both_create_statements(
    adapter_mock: Mock, make_snapshot: t.Callable[..., Snapshot]
) -> None:
    # the below mocks are needed to create a situation
    # where we trigger two create statements during evaluation
    transaction_mock = Mock()
    transaction_mock.__enter__ = Mock()
    transaction_mock.__exit__ = Mock()
    session_mock = Mock()
    session_mock.__enter__ = Mock()
    session_mock.__exit__ = Mock()
    adapter_mock = Mock()
    adapter_mock.transaction.return_value = transaction_mock
    adapter_mock.session.return_value = session_mock
    adapter_mock.dialect = "trino"
    adapter_mock.HAS_VIEW_BINDING = False
    adapter_mock.wap_supported.return_value = False
    adapter_mock.get_data_objects.return_value = []
    adapter_mock.with_settings.return_value = adapter_mock
    adapter_mock.table_exists.return_value = False

    props = []

    def mutate_view_properties(*args, **kwargs):
        view_props = kwargs.get("view_properties")
        if isinstance(view_props, dict):
            props.append(view_props["creatable_type"].sql())
            # simulate view pop
            view_props.pop("creatable_type")
        return None

    adapter_mock.create_view.side_effect = mutate_view_properties

    evaluator = SnapshotEvaluator(adapter_mock)

    # create a view model with SECURITY INVOKER physical property
    # AND self referenctial to trigger two create statements
    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.security_view,
                kind VIEW,
                physical_properties (
                    'creatable_type' = 'SECURITY INVOKER'
                )
            );

            SELECT 1 as col from test_schema.security_view;
            """
        ),
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.evaluate(
        snapshot,
        start="2024-01-01",
        end="2024-01-02",
        execution_time="2024-01-02",
        snapshots={},
    )

    # Verify create_view was called twice
    assert adapter_mock.create_view.call_count == 2
    first_call = adapter_mock.create_view.call_args_list[0]
    second_call = adapter_mock.create_view.call_args_list[1]

    # First call should be CREATE VIEW (replace=False) second CREATE OR REPLACE VIEW (replace=True)
    assert first_call.kwargs.get("replace") == False
    assert second_call.kwargs.get("replace") == True

    # Both calls should have view_properties with security invoker
    assert props == ["'SECURITY INVOKER'", "'SECURITY INVOKER'"]


def _create_grants_test_model(
    grants=None, kind="FULL", grants_target_layer=None, virtual_environment_mode=None
):
    if kind == "SEED":
        from sqlmesh.core.model.definition import create_seed_model
        from sqlmesh.core.model.kind import SeedKind
        import tempfile
        import os

        # Create a temporary CSV file for the test
        temp_csv = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
        temp_csv.write("id,name\n1,test\n2,test2\n")
        temp_csv.flush()
        temp_csv.close()

        seed_kind_config = {"name": "SEED", "path": temp_csv.name}
        seed_kind = SeedKind(**seed_kind_config)

        kwargs = {}
        if grants is not None:
            kwargs["grants"] = grants
        if grants_target_layer is not None:
            kwargs["grants_target_layer"] = grants_target_layer

        model = create_seed_model("test_model", seed_kind, **kwargs)

        # Clean up the temporary file
        os.unlink(temp_csv.name)

        return model

    # Handle regular SQL models
    kwargs = {
        "kind": kind,
    }
    if grants is not None:
        kwargs["grants"] = grants
    if grants_target_layer is not None:
        kwargs["grants_target_layer"] = grants_target_layer
    if virtual_environment_mode is not None:
        kwargs["virtual_environment_mode"] = virtual_environment_mode

    # Add column annotations for non-SEED models to ensure table creation
    if kind != "SEED":
        kwargs["columns"] = {
            "id": "INT",
            "ds": "DATE",
            "updated_at": "TIMESTAMP",
        }

    # Add required fields for specific model kinds
    if kind == "INCREMENTAL_BY_TIME_RANGE":
        kwargs["kind"] = {"name": "INCREMENTAL_BY_TIME_RANGE", "time_column": "ds"}
    elif kind == "INCREMENTAL_BY_PARTITION":
        kwargs["kind"] = {"name": "INCREMENTAL_BY_PARTITION"}
        kwargs["partitioned_by"] = ["ds"]  # This goes on the model, not the kind
    elif kind == "INCREMENTAL_BY_UNIQUE_KEY":
        kwargs["kind"] = {"name": "INCREMENTAL_BY_UNIQUE_KEY", "unique_key": ["id"]}
    elif kind == "INCREMENTAL_UNMANAGED":
        kwargs["kind"] = {"name": "INCREMENTAL_UNMANAGED"}
    elif kind == "SCD_TYPE_2":
        kwargs["kind"] = {
            "name": "SCD_TYPE_2",
            "unique_key": ["id"],
            "updated_at_name": "updated_at",
        }

    return create_sql_model(
        "test_model",
        parse_one("SELECT 1 as id, CURRENT_DATE as ds, CURRENT_TIMESTAMP as updated_at"),
        **kwargs,
    )


@pytest.mark.parametrize(
    "target_layer,apply_layer,expected_call_count",
    [
        (GrantsTargetLayer.ALL, GrantsTargetLayer.PHYSICAL, 1),
        (GrantsTargetLayer.ALL, GrantsTargetLayer.VIRTUAL, 1),
        (GrantsTargetLayer.PHYSICAL, GrantsTargetLayer.PHYSICAL, 1),
        (GrantsTargetLayer.PHYSICAL, GrantsTargetLayer.VIRTUAL, 0),
        (GrantsTargetLayer.VIRTUAL, GrantsTargetLayer.PHYSICAL, 0),
        (GrantsTargetLayer.VIRTUAL, GrantsTargetLayer.VIRTUAL, 1),
    ],
)
def test_apply_grants_target_layer(
    target_layer: GrantsTargetLayer,
    apply_layer: GrantsTargetLayer,
    expected_call_count: int,
    adapter_mock: Mock,
    mocker: MockerFixture,
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")
    strategy = ViewStrategy(adapter_mock)

    model = _create_grants_test_model(
        grants={"select": ["user1"]}, grants_target_layer=target_layer
    )

    strategy._apply_grants(model, "test_table", apply_layer)

    if expected_call_count > 0:
        assert sync_grants_mock.call_count == expected_call_count
    else:
        sync_grants_mock.assert_not_called()


@pytest.mark.parametrize(
    "model_kind_name",
    [
        "FULL",
        "INCREMENTAL_BY_TIME_RANGE",
        "SEED",
        "MANAGED",
        "SCD_TYPE_2",
        "VIEW",
    ],
)
def test_grants_create_model_kind(
    model_kind_name: str,
    adapter_mock: Mock,
    mocker: MockerFixture,
    make_snapshot: t.Callable[..., Snapshot],
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")

    grants = {"select": ["user1"]}
    model = _create_grants_test_model(
        grants=grants, kind=model_kind_name, grants_target_layer=GrantsTargetLayer.ALL
    )
    snapshot = make_snapshot(model)

    evaluator = SnapshotEvaluator(adapter_mock)
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.create([snapshot], {})

    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == grants


@pytest.mark.parametrize(
    "target_layer",
    [
        GrantsTargetLayer.PHYSICAL,
        GrantsTargetLayer.VIRTUAL,
        GrantsTargetLayer.ALL,
    ],
)
def test_grants_target_layer(
    target_layer: GrantsTargetLayer,
    adapter_mock: Mock,
    mocker: MockerFixture,
    make_snapshot: t.Callable[..., Snapshot],
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")
    evaluator = SnapshotEvaluator(adapter_mock)

    grants = {"select": ["user1"]}
    model = create_sql_model(
        "test_schema.test_model",
        parse_one("SELECT 1 as id"),
        kind="FULL",
        grants=grants,
        grants_target_layer=target_layer,
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {})
    if target_layer == GrantsTargetLayer.VIRTUAL:
        assert sync_grants_mock.call_count == 0
    else:
        assert sync_grants_mock.call_count == 1
        assert sync_grants_mock.call_args[0][1] == grants
    sync_grants_mock.reset_mock()
    evaluator.promote([snapshot], EnvironmentNamingInfo(name="prod"))
    if target_layer == GrantsTargetLayer.VIRTUAL:
        assert sync_grants_mock.call_count == 1
    elif target_layer == GrantsTargetLayer.PHYSICAL:
        # Physical layer: no grants applied during promotion (already applied during create)
        assert sync_grants_mock.call_count == 0
    else:  # target_layer == GrantsTargetLayer.ALL
        # All layers: only virtual grants applied during promotion (physical already done in create)
        assert sync_grants_mock.call_count == 1


def test_grants_update(
    adapter_mock: Mock, mocker: MockerFixture, make_snapshot: t.Callable[..., Snapshot]
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")

    evaluator = SnapshotEvaluator(adapter_mock)

    model = create_sql_model(
        "test_schema.test_model",
        parse_one("SELECT 1 as id"),
        kind="FULL",
        grants={"select": ["user1"]},
        grants_target_layer=GrantsTargetLayer.ALL,
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator.create([snapshot], {})

    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == {"select": ["user1"]}

    # Update model query AND change grants
    updated_model_dict = model.dict()
    updated_model_dict["query"] = parse_one("SELECT 1 as id, 2 as value")
    updated_model_dict["grants"] = {"select": ["user2", "user3"], "insert": ["admin"]}
    updated_model = SqlModel.parse_obj(updated_model_dict)

    new_snapshot = make_snapshot(updated_model)
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    sync_grants_mock.reset_mock()
    evaluator.create([new_snapshot], {})

    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == {"select": ["user2", "user3"], "insert": ["admin"]}

    # Update model query AND remove grants
    updated_model_dict = model.dict()
    updated_model_dict["query"] = parse_one("SELECT 1 as id, 'updated' as status")
    updated_model_dict["grants"] = {}
    updated_model = SqlModel.parse_obj(updated_model_dict)

    new_snapshot = make_snapshot(updated_model)
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    sync_grants_mock.reset_mock()
    evaluator.create([new_snapshot], {})

    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == {}


def test_grants_create_and_evaluate(
    adapter_mock: Mock, mocker: MockerFixture, make_snapshot: t.Callable[..., Snapshot]
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")

    evaluator = SnapshotEvaluator(adapter_mock)

    model = load_sql_based_model(
        parse(  # type: ignore
            """
            MODEL (
                name test_schema.test_model,
                kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
                grants (
                    'select' = ['reader1', 'reader2'],
                    'insert' = ['writer']
                ),
                grants_target_layer 'all'
            );
            SELECT ds::DATE, value::INT FROM source WHERE ds BETWEEN @start_ds AND @end_ds;
        """
        )
    )

    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.create([snapshot], {})
    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == {
        "select": ["reader1", "reader2"],
        "insert": ["writer"],
    }

    sync_grants_mock.reset_mock()
    evaluator.evaluate(
        snapshot, start="2020-01-01", end="2020-01-02", execution_time="2020-01-02", snapshots={}
    )
    # Evaluate should not reapply grants
    sync_grants_mock.assert_not_called()


@pytest.mark.parametrize(
    "strategy_class",
    [
        EngineManagedStrategy,
        FullRefreshStrategy,
        IncrementalByTimeRangeStrategy,
        IncrementalByPartitionStrategy,
        IncrementalUnmanagedStrategy,
        IncrementalByUniqueKeyStrategy,
        SCDType2Strategy,
        # SeedStrategy excluded because seeds do not support migrations
    ],
)
def test_grants_materializable_strategy_migrate(
    strategy_class: t.Type[MaterializableStrategy],
    adapter_mock: Mock,
    mocker: MockerFixture,
    make_snapshot: t.Callable[..., Snapshot],
):
    adapter_mock.SUPPORTS_GRANTS = True
    adapter_mock.get_alter_operations.return_value = []
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")
    strategy = strategy_class(adapter_mock)
    grants = {"select": ["user1"]}
    model = _create_grants_test_model(grants=grants, grants_target_layer=GrantsTargetLayer.ALL)
    snapshot = make_snapshot(model)

    strategy.migrate(
        "target_table",
        "source_table",
        snapshot,
        ignore_destructive=False,
        ignore_additive=False,
        allow_destructive_snapshots=set(),
        allow_additive_snapshots=set(),
    )

    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == grants


def test_grants_clone_snapshot_in_dev(
    adapter_mock: Mock, mocker: MockerFixture, make_snapshot: t.Callable[..., Snapshot]
):
    adapter_mock.SUPPORTS_CLONING = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")

    evaluator = SnapshotEvaluator(adapter_mock)
    grants = {"select": ["user1", "user2"]}
    model = _create_grants_test_model(grants=grants, grants_target_layer=GrantsTargetLayer.ALL)
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator._clone_snapshot_in_dev(
        snapshot, {}, DeployabilityIndex.all_deployable(), {}, {}, set(), set()
    )

    sync_grants_mock.assert_called_once()
    assert (
        sync_grants_mock.call_args[0][0].sql()
        == f"sqlmesh__default.test_model__{snapshot.version}__dev"
    )
    assert sync_grants_mock.call_args[0][1] == grants


@pytest.mark.parametrize(
    "model_kind_name",
    [
        "INCREMENTAL_BY_TIME_RANGE",
        "SEED",
    ],
)
def test_grants_evaluator_insert_without_replace_query_for_model(
    model_kind_name: str,
    adapter_mock: Mock,
    mocker: MockerFixture,
    make_snapshot: t.Callable[..., Snapshot],
):
    adapter_mock.SUPPORTS_GRANTS = True
    adapter_mock.table_exists.return_value = False  # Table doesn't exist
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")

    evaluator = SnapshotEvaluator(adapter_mock)

    grants = {"select": ["reader1", "reader2"]}
    model = _create_grants_test_model(
        grants=grants, kind=model_kind_name, grants_target_layer=GrantsTargetLayer.ALL
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluator.evaluate(
        snapshot,
        start="2023-01-01",
        end="2023-01-01",
        execution_time="2023-01-01",
        snapshots={},
    )

    # Grants are applied during the table creation phase, not during insert
    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == grants

    sync_grants_mock.reset_mock()
    adapter_mock.table_exists.return_value = True
    snapshot.add_interval("2023-01-01", "2023-01-01")
    evaluator.evaluate(
        snapshot,
        start="2023-01-02",  # Different date from existing interval
        end="2023-01-02",
        execution_time="2023-01-02",
        snapshots={},
    )

    # Should not apply grants since it's not the first insert
    sync_grants_mock.assert_not_called()


@pytest.mark.parametrize(
    "model_kind_name",
    [
        "INCREMENTAL_BY_PARTITION",
        "INCREMENTAL_BY_UNIQUE_KEY",
        "INCREMENTAL_UNMANAGED",
        "FULL",
        "SCD_TYPE_2",
    ],
)
def test_grants_evaluator_insert_with_replace_query_for_model(
    model_kind_name: str,
    adapter_mock: Mock,
    mocker: MockerFixture,
    make_snapshot: t.Callable[..., Snapshot],
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")
    adapter_mock.table_exists.return_value = False  # Table doesn't exist
    adapter_mock.columns.return_value = {
        "id": exp.DataType.build("int"),
        "ds": exp.DataType.build("date"),
    }

    evaluator = SnapshotEvaluator(adapter_mock)

    grants = {"select": ["user1"]}
    model = _create_grants_test_model(
        grants=grants, kind=model_kind_name, grants_target_layer=GrantsTargetLayer.ALL
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # Now evaluate the snapshot (this should apply grants during first insert)
    evaluator.evaluate(
        snapshot,
        start="2023-01-01",
        end="2023-01-01",
        execution_time="2023-01-01",
        snapshots={},
    )

    # Should be called twice more during evaluate: once creating table,
    # once during first insert with _replace_query_for_model()
    assert sync_grants_mock.call_count == 2
    assert sync_grants_mock.call_args[0][1] == grants

    sync_grants_mock.reset_mock()
    adapter_mock.table_exists.return_value = True
    snapshot.add_interval("2023-01-01", "2023-01-01")
    evaluator.evaluate(
        snapshot,
        start="2023-01-02",  # Different date from existing interval
        end="2023-01-02",
        execution_time="2023-01-02",
        snapshots={},
    )

    if model_kind_name in ("FULL", "SCD_TYPE_2"):
        # Full refresh and SCD_TYPE_2 always recreate the table, so grants are always applied
        sync_grants_mock.assert_called_once()
        assert sync_grants_mock.call_args[0][1] == grants
    else:
        # Should not apply grants since it's not the first insert
        sync_grants_mock.assert_not_called()


@pytest.mark.parametrize(
    "model_grants_target_layer",
    [
        GrantsTargetLayer.ALL,
        GrantsTargetLayer.VIRTUAL,
        GrantsTargetLayer.PHYSICAL,
    ],
)
def test_grants_in_production_with_dev_only_vde(
    adapter_mock: Mock,
    mocker: MockerFixture,
    make_snapshot: t.Callable[..., Snapshot],
    model_grants_target_layer: GrantsTargetLayer,
):
    adapter_mock.SUPPORTS_GRANTS = True
    sync_grants_mock = mocker.patch.object(adapter_mock, "sync_grants_config")

    from sqlmesh.core.model.meta import VirtualEnvironmentMode, GrantsTargetLayer
    from sqlmesh.core.snapshot.definition import DeployabilityIndex

    model_virtual_grants = _create_grants_test_model(
        grants={"select": ["user1"], "insert": ["role1"]},
        grants_target_layer=model_grants_target_layer,
        virtual_environment_mode=VirtualEnvironmentMode.DEV_ONLY,
    )

    snapshot = make_snapshot(model_virtual_grants)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    evaluator = SnapshotEvaluator(adapter_mock)
    # create will apply grants to physical layer tables
    deployability_index = DeployabilityIndex.all_deployable()
    evaluator.create([snapshot], {}, deployability_index=deployability_index)

    sync_grants_mock.assert_called_once()
    assert sync_grants_mock.call_args[0][1] == {"select": ["user1"], "insert": ["role1"]}

    # Non-deployable (dev) env
    sync_grants_mock.reset_mock()
    deployability_index = DeployabilityIndex.none_deployable()
    evaluator.create([snapshot], {}, deployability_index=deployability_index)
    if model_grants_target_layer == GrantsTargetLayer.VIRTUAL:
        sync_grants_mock.assert_not_called()
    else:
        # Should still apply grants to physical table when target layer is ALL or PHYSICAL
        sync_grants_mock.assert_called_once()
        assert sync_grants_mock.call_args[0][1] == {"select": ["user1"], "insert": ["role1"]}
