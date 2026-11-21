import logging
import typing as t
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
    PostgresConnectionConfig,
)
from sqlmesh.core.console import TerminalConsole
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.metaprogramming import Executable
from tests.core.test_table_diff import create_test_console
import time_machine
from pytest_mock.plugin import MockerFixture
from sqlglot import parse_one, exp

from sqlmesh.core import dialect as d
from sqlmesh.core.context import Context
from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.environment import EnvironmentNamingInfo, EnvironmentStatements
from sqlmesh.core.model import (
    EmbeddedKind,
    ExternalModel,
    FullKind,
    IncrementalByPartitionKind,
    IncrementalByTimeRangeKind,
    IncrementalUnmanagedKind,
    Model,
    SCDType2ByTimeKind,
    SeedKind,
    SeedModel,
    SqlModel,
    ModelKindName,
)
from sqlmesh.core.model.kind import OnDestructiveChange, OnAdditiveChange, ViewKind
from sqlmesh.core.model.schema import update_model_schemas
from sqlmesh.core.model.seed import Seed
from sqlmesh.core.plan import Plan, PlanBuilder, SnapshotIntervals
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotChangeCategory,
    SnapshotDataVersion,
    SnapshotFingerprint,
)
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import (
    now,
    to_date,
    to_datetime,
    to_timestamp,
    yesterday_ds,
)
from sqlmesh.utils.errors import PlanError, NoChangesPlanError
from sqlmesh.utils.rich import strip_ansi_codes


def test_forward_only_plan_sets_version(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), dialect="duckdb")
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select 2, ds"), dialect="duckdb")
    )
    snapshot_b.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            change_category=SnapshotChangeCategory.NON_BREAKING,
            dev_table_suffix="dev",
        ),
    )
    assert not snapshot_b.version

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot_b.name: (snapshot_b, snapshot_b)},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={snapshot_b.snapshot_id: snapshot_b},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(context_diff, forward_only=True)

    plan_builder.build()
    assert snapshot_b.version == "test_version"


def test_forward_only_dev(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            dialect="duckdb",
            query=parse_one("select 1 as a, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    # Simulate a direct change.
    updated_snapshot = make_snapshot(
        SqlModel(
            **{
                **snapshot.model.dict(),
                "query": parse_one("select 2 as a, ds"),
            }
        )
    )

    expected_start = to_date("2022-01-02")
    expected_end = to_date("2022-01-03")
    expected_interval_end = to_timestamp(to_date("2022-01-04"))

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    yesterday_ds_mock = mocker.patch("sqlmesh.core.plan.builder.yesterday_ds")
    yesterday_ds_mock.return_value = expected_start

    now_mock = mocker.patch("sqlmesh.core.snapshot.definition.now")
    now_mock.return_value = expected_end

    mocker.patch("sqlmesh.core.plan.builder.now").return_value = expected_end
    mocker.patch("sqlmesh.core.plan.definition.now").return_value = expected_end

    plan = PlanBuilder(context_diff, forward_only=True, is_dev=True).build()

    assert plan.restatements == {
        updated_snapshot.snapshot_id: (to_timestamp(expected_start), expected_interval_end)
    }
    assert plan.start == to_date(expected_start)
    assert plan.end == expected_end


def test_forward_only_metadata_change_dev(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            dialect="duckdb",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    # Simulate a metadata change.
    updated_snapshot = make_snapshot(
        SqlModel(
            **{
                **snapshot.model.dict(),
                "owner": "new_owner",
            }
        )
    )

    expected_start = to_date("2022-01-02")
    expected_end = to_date("2022-01-03")

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: snapshot},
        new_snapshots={updated_snapshot.snapshot_id: snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    yesterday_ds_mock = mocker.patch("sqlmesh.core.plan.builder.yesterday_ds")
    yesterday_ds_mock.return_value = expected_start

    now_mock = mocker.patch("sqlmesh.core.snapshot.definition.now")
    now_mock.return_value = expected_end

    mocker.patch("sqlmesh.core.plan.builder.now").return_value = expected_end
    mocker.patch("sqlmesh.core.plan.definition.now").return_value = expected_end

    plan = PlanBuilder(context_diff, forward_only=True, is_dev=True).build()

    assert not plan.restatements


def test_forward_only_plan_added_models(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1 as a, ds"), dialect="duckdb")
    )

    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select a, ds from a"), dialect="duckdb"),
        nodes={"a": snapshot_a.node},
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_b.snapshot_id},
        removed_snapshots={},
        modified_snapshots={snapshot_a.name: (snapshot_a, snapshot_a)},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, forward_only=True).build()
    assert snapshot_a.change_category == SnapshotChangeCategory.METADATA
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING
    assert snapshot_a.is_forward_only
    assert snapshot_b.is_forward_only


def test_forward_only_plan_categorizes_change_model_kind_as_breaking(
    make_snapshot, mocker: MockerFixture
):
    snapshot_old = make_snapshot(
        SqlModel(
            name="a",
            dialect="duckdb",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    # Simulate a change in the model kind.
    updated_snapshot = make_snapshot(
        SqlModel(
            **{
                **snapshot_old.model.dict(),
                "kind": dict(name=ModelKindName.VIEW),
            }
        )
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot_old)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, forward_only=True).build()

    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING
    assert not updated_snapshot.is_forward_only


def test_paused_forward_only_parent(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_a.previous_versions = (
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
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    snapshot_b_old = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds from a")))
    snapshot_b_old.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=False)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 3, ds from a")))
    assert not snapshot_b.version

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot_b.name: (snapshot_b, snapshot_b_old)},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={snapshot_b.snapshot_id: snapshot_b},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, forward_only=False).build()
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING


def test_forward_only_plan_allow_destructive_models(
    make_snapshot, make_snapshot_on_destructive_change
):
    # forward-only model, not forward-only plan
    snapshot_a_old, snapshot_a = make_snapshot_on_destructive_change()

    context_diff_a = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot_a.name: (snapshot_a, snapshot_a_old)},
        snapshots={snapshot_a.snapshot_id: snapshot_a, snapshot_a_old.snapshot_id: snapshot_a_old},
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(
        PlanError, match="Plan requires a destructive change to a forward-only model"
    ):
        PlanBuilder(context_diff_a, forward_only=False).build()

    logger = logging.getLogger("sqlmesh.core.plan.builder")
    with patch.object(logger, "warning") as mock_logger:
        assert PlanBuilder(
            context_diff_a, forward_only=False, allow_destructive_models=['"a"']
        ).build()
        assert mock_logger.call_count == 0

    # forward-only plan, no forward-only model
    snapshot_b_old = make_snapshot(
        SqlModel(
            name="b",
            dialect="duckdb",
            query=parse_one("select '1' as one, '2022-01-01' ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            dialect="duckdb",
            query=parse_one("select 1 as one, '2022-01-01' ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    snapshot_c_old = make_snapshot(
        SqlModel(
            name="c",
            dialect="duckdb",
            query=parse_one("select '1' as one"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    snapshot_c = make_snapshot(
        SqlModel(
            name="c",
            dialect="duckdb",
            query=parse_one("select 1 as one"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    context_diff_b = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_b.name: (snapshot_b, snapshot_b_old),
            snapshot_c.name: (snapshot_c, snapshot_c_old),
        },
        snapshots={
            snapshot_b.snapshot_id: snapshot_b,
            snapshot_b_old.snapshot_id: snapshot_b_old,
            snapshot_c.snapshot_id: snapshot_c,
            snapshot_c_old.snapshot_id: snapshot_c_old,
        },
        new_snapshots={snapshot_b.snapshot_id: snapshot_b, snapshot_c.snapshot_id: snapshot_c},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(
        PlanError,
        match="""Plan requires a destructive change to a forward-only model.""",
    ):
        PlanBuilder(context_diff_b, forward_only=True).build()

    with pytest.raises(
        PlanError,
        match="""Plan requires a destructive change to a forward-only model.""",
    ):
        PlanBuilder(context_diff_b, forward_only=True, allow_destructive_models=['"b"']).build()

    logger = logging.getLogger("sqlmesh.core.plan.builder")
    with patch.object(logger, "warning") as mock_logger:
        PlanBuilder(
            context_diff_b,
            forward_only=True,
            allow_destructive_models=['"b"', '"c"'],
        ).build()
        assert mock_logger.call_count == 0


def test_forward_only_plan_allow_additive_models(
    mocker, make_snapshot, make_snapshot_on_additive_change
):
    # forward-only model, not forward-only plan
    snapshot_a_old, snapshot_a = make_snapshot_on_additive_change()

    context_diff_a = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot_a.name: (snapshot_a, snapshot_a_old)},
        snapshots={snapshot_a.snapshot_id: snapshot_a, snapshot_a_old.snapshot_id: snapshot_a_old},
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(PlanError, match="Plan requires an additive change to a forward-only model"):
        PlanBuilder(context_diff_a, forward_only=False).build()

    console = TerminalConsole()
    log_warning_spy = mocker.spy(console, "log_warning")
    assert PlanBuilder(
        context_diff_a, forward_only=False, allow_additive_models=['"a"'], console=console
    ).build()
    assert log_warning_spy.call_count == 0

    snapshot_a_old, snapshot_a = make_snapshot_on_additive_change(
        on_additive_change=OnAdditiveChange.WARN
    )

    context_diff_a = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot_a.name: (snapshot_a, snapshot_a_old)},
        snapshots={snapshot_a.snapshot_id: snapshot_a, snapshot_a_old.snapshot_id: snapshot_a_old},
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    log_warning_spy.reset_mock()
    assert PlanBuilder(context_diff_a, forward_only=False, console=console).build()
    log_warning_spy.assert_called_once_with("""
Plan requires additive change to forward-only model '"a"'s schema that adds column 'three'.

Schema changes:
  ALTER TABLE "a" ADD COLUMN three TEXT""")


def test_forward_only_model_on_destructive_change(
    make_snapshot, make_snapshot_on_destructive_change
):
    # direct change to A
    snapshot_a_old, snapshot_a = make_snapshot_on_destructive_change()

    context_diff_1 = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a.name: (snapshot_a, snapshot_a_old),
        },
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
        },
        new_snapshots={
            snapshot_a.snapshot_id: snapshot_a,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(
        PlanError,
        match="""Plan requires a destructive change to a forward-only model.""",
    ):
        PlanBuilder(context_diff_1).build()

    # allow A, indirect change to B
    snapshot_a_old2, snapshot_a2 = make_snapshot_on_destructive_change(
        on_destructive_change=OnDestructiveChange.ALLOW
    )

    snapshot_b_old2 = make_snapshot(
        SqlModel(
            name="b",
            dialect="duckdb",
            query=parse_one("select one, '2022-01-01' ds from a"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        ),
        nodes={'"a"': snapshot_a_old2.model},
    )
    snapshot_b2 = make_snapshot(snapshot_b_old2.model, nodes={'"a"': snapshot_a2.model})
    snapshot_b2.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            dev_table_suffix="dev",
        ),
    )

    context_diff_2 = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a2.name: (snapshot_a2, snapshot_a_old2),
            snapshot_b2.name: (snapshot_b2, snapshot_b_old2),
        },
        snapshots={
            snapshot_a2.snapshot_id: snapshot_a2,
            snapshot_b2.snapshot_id: snapshot_b2,
        },
        new_snapshots={
            snapshot_a2.snapshot_id: snapshot_a2,
            snapshot_b2.snapshot_id: snapshot_b2,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff_2).build()

    # allow A and B, indirect change to C
    snapshot_a_old3, snapshot_a3 = make_snapshot_on_destructive_change(
        on_destructive_change=OnDestructiveChange.ALLOW
    )

    snapshot_b_old3 = make_snapshot(
        SqlModel(
            name="b",
            dialect="duckdb",
            query=parse_one("select one, '2022-01-01' ds from a"),
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=True,
                on_destructive_change=OnDestructiveChange.ALLOW,
            ),
        ),
        nodes={'"a"': snapshot_a_old3.model},
    )
    snapshot_b3 = make_snapshot(snapshot_b_old3.model, nodes={'"a"': snapshot_a3.model})
    snapshot_b3.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            dev_table_suffix="dev",
        ),
    )

    snapshot_c_old3 = make_snapshot(
        SqlModel(
            name="c",
            dialect="duckdb",
            query=parse_one("select one, '2022-01-01' ds from b"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        ),
        nodes={'"a"': snapshot_a_old3.model, '"b"': snapshot_b_old3.model},
    )
    snapshot_c3 = make_snapshot(
        snapshot_c_old3.model, nodes={'"a"': snapshot_a3.model, '"b"': snapshot_b3.model}
    )
    snapshot_c3.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            dev_table_suffix="dev",
        ),
    )

    context_diff_3 = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a3.name: (snapshot_a3, snapshot_a_old3),
            snapshot_b3.name: (snapshot_b3, snapshot_b_old3),
            snapshot_c3.name: (snapshot_c3, snapshot_c_old3),
        },
        snapshots={
            snapshot_a3.snapshot_id: snapshot_a3,
            snapshot_b3.snapshot_id: snapshot_b3,
            snapshot_c3.snapshot_id: snapshot_c3,
        },
        new_snapshots={
            snapshot_a3.snapshot_id: snapshot_a3,
            snapshot_b3.snapshot_id: snapshot_b3,
            snapshot_c3.snapshot_id: snapshot_c3,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff_3).build()


def test_forward_only_model_on_destructive_change_no_column_types(
    make_snapshot_on_destructive_change,
):
    snapshot_a_old, snapshot_a = make_snapshot_on_destructive_change(
        old_query="select 1 as one, '2022-01-01' ds", new_query="select one, '2022-01-01' ds"
    )

    context_diff_1 = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a.name: (snapshot_a, snapshot_a_old),
        },
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_a_old.snapshot_id: snapshot_a_old,
        },
        new_snapshots={
            snapshot_a.snapshot_id: snapshot_a,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    logger = logging.getLogger("sqlmesh.core.plan.builder")
    with patch.object(logger, "warning") as mock_logger:
        PlanBuilder(context_diff_1).build()
        assert mock_logger.call_count == 0


def test_missing_intervals_lookback(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, '2022-01-01' ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", lookback=2),
        )
    )
    snapshot_a.intervals = [(to_timestamp("2022-01-01"), to_timestamp("2022-01-05"))]
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        modified_snapshots={},
        removed_snapshots={},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
        },
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = Plan(
        context_diff=context_diff,
        plan_id="",
        provided_start="2022-01-01",
        provided_end="2022-01-04",
        execution_time="2022-01-05 12:00",
        is_dev=True,
        skip_backfill=False,
        empty_backfill=False,
        no_gaps=False,
        forward_only=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        include_unmodified=True,
        environment_naming_info=EnvironmentNamingInfo(),
        directly_modified={snapshot_a.snapshot_id},
        indirectly_modified={},
        deployability_index=DeployabilityIndex.all_deployable(),
        restatements={},
        restate_all_snapshots=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        start_override_per_model=None,
        end_override_per_model=None,
        explain=False,
    )

    assert not plan.missing_intervals


@pytest.mark.slow
@time_machine.travel(now(), tick=False)
def test_restate_models(sushi_context_pre_scheduling: Context):
    plan = sushi_context_pre_scheduling.plan(
        restate_models=["sushi.waiter_revenue_by_day", "tag:expensive"], no_prompts=True
    )

    start = to_timestamp(plan.start)
    tomorrow = to_timestamp(to_date("tomorrow"))

    assert plan.restatements == {
        sushi_context_pre_scheduling.get_snapshot(
            "sushi.waiter_revenue_by_day", raise_if_missing=True
        ).snapshot_id: (
            start,
            tomorrow,
        ),
        sushi_context_pre_scheduling.get_snapshot(
            "sushi.top_waiters", raise_if_missing=True
        ).snapshot_id: (
            start,
            tomorrow,
        ),
        sushi_context_pre_scheduling.get_snapshot(
            "sushi.customer_revenue_by_day", raise_if_missing=True
        ).snapshot_id: (
            start,
            tomorrow,
        ),
        sushi_context_pre_scheduling.get_snapshot(
            "sushi.customer_revenue_lifetime", raise_if_missing=True
        ).snapshot_id: (
            start,
            tomorrow,
        ),
    }
    assert plan.requires_backfill
    assert plan.models_to_backfill == {
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_lifetime"',
        '"memory"."sushi"."items"',
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."orders"',
        '"memory"."sushi"."top_waiters"',
        '"memory"."sushi"."waiter_revenue_by_day"',
    }

    with pytest.raises(
        PlanError,
        match="Selector did not return any models. Please check your model selection and try again.",
    ):
        sushi_context_pre_scheduling.plan(restate_models=["unknown_model"], no_prompts=True)

    with pytest.raises(
        PlanError,
        match="Selector did not return any models. Please check your model selection and try again.",
    ):
        sushi_context_pre_scheduling.plan(restate_models=["tag:unknown_tag"], no_prompts=True)

    plan = sushi_context_pre_scheduling.plan(restate_models=["raw.demographics"], no_prompts=True)
    assert not plan.has_changes
    assert plan.restatements
    assert plan.models_to_backfill == {
        '"memory"."raw"."demographics"',
        '"memory"."sushi"."active_customers"',
        '"memory"."sushi"."customers"',
        '"memory"."sushi"."marketing"',
        '"memory"."sushi"."orders"',
        '"memory"."sushi"."raw_marketing"',
        '"memory"."sushi"."waiter_as_customer_by_day"',
        '"memory"."sushi"."waiter_names"',
        '"memory"."sushi"."waiters"',
        '"memory"."sushi"."count_customers_active"',
        '"memory"."sushi"."count_customers_inactive"',
    }


@pytest.mark.slow
@time_machine.travel(now(minute_floor=False), tick=False)
def test_restate_models_with_existing_missing_intervals(init_and_plan_context: t.Callable):
    sushi_context, plan = init_and_plan_context("examples/sushi")
    sushi_context.apply(plan)

    yesterday_ts = to_timestamp(yesterday_ds())

    assert not sushi_context.plan(no_prompts=True).requires_backfill
    waiter_revenue_by_day = sushi_context.snapshots['"memory"."sushi"."waiter_revenue_by_day"']
    sushi_context.state_sync.remove_intervals(
        [(waiter_revenue_by_day, (yesterday_ts, waiter_revenue_by_day.intervals[0][1]))]
    )
    assert sushi_context.plan(no_prompts=True).requires_backfill

    plan = sushi_context.plan(restate_models=["sushi.waiter_revenue_by_day"], no_prompts=True)

    one_day_ms = 24 * 60 * 60 * 1000

    today_ts = to_timestamp(to_date("today"))
    plan_start_ts = to_timestamp(plan.start)
    assert plan_start_ts == today_ts - 7 * one_day_ms

    expected_missing_intervals = [
        (i, i + one_day_ms) for i in range(plan_start_ts, today_ts, one_day_ms)
    ]
    assert len(expected_missing_intervals) == 7

    waiter_revenue_by_day_snapshot_id = sushi_context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    ).snapshot_id
    top_waiters_snapshot_id = sushi_context.get_snapshot(
        "sushi.top_waiters", raise_if_missing=True
    ).snapshot_id

    assert plan.restatements == {
        waiter_revenue_by_day_snapshot_id: (
            plan_start_ts,
            today_ts,
        ),
        top_waiters_snapshot_id: (
            plan_start_ts,
            to_timestamp(to_date("tomorrow")),
        ),
    }
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot_id,
            intervals=expected_missing_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot_id,
            intervals=expected_missing_intervals,
        ),
    ]
    assert plan.requires_backfill
    assert plan.models_to_backfill == {
        top_waiters_snapshot_id.name,
        waiter_revenue_by_day_snapshot_id.name,
        '"memory"."sushi"."items"',
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."orders"',
    }


def test_restate_symbolic_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, key"),
            kind="EMBEDDED",
        )
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot_a.snapshot_id: snapshot_a},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff, restate_models=[snapshot_a.name]).build()
    assert plan.restatements


def test_restate_seed_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="new_content"),
            column_hashes={"col": "hash2"},
            depends_on=set(),
        )
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot_a.snapshot_id: snapshot_a},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff, restate_models=[snapshot_a.name]).build()
    assert not plan.restatements


def test_restate_missing_model(make_snapshot, mocker: MockerFixture):
    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(
        PlanError,
        match=r"Cannot restate model 'missing'. Model does not exist.",
    ):
        PlanBuilder(context_diff, restate_models=["missing"]).build()


def test_end_validation(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_a.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot_a.snapshot_id: snapshot_a},
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    dev_plan_builder = PlanBuilder(context_diff, end="2022-01-03", is_dev=True)
    assert dev_plan_builder.build().end == "2022-01-03"
    dev_plan_builder.set_end("2022-01-04")
    assert dev_plan_builder.build().end == "2022-01-04"

    start_end_not_allowed_message = (
        "The start and end dates can't be set for a production plan without restatements."
    )

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        PlanBuilder(context_diff, end="2022-01-03").build()

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        PlanBuilder(context_diff, start="2022-01-03").build()

    prod_plan_builder = PlanBuilder(context_diff)

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        prod_plan_builder.set_end("2022-01-03").build()

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        prod_plan_builder.set_start("2022-01-03").build()

    context_diff.new_snapshots = {}
    restatement_prod_plan_builder = PlanBuilder(
        context_diff,
        start="2022-01-01",
        end="2022-01-03",
        restate_models=['"a"'],
    )
    assert restatement_prod_plan_builder.build().end == "2022-01-03"
    restatement_prod_plan_builder.set_end("2022-01-04")
    assert restatement_prod_plan_builder.build().end == "2022-01-04"


def test_forward_only_plan_seed_models(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="content"),
            column_hashes={"col": "hash1"},
            depends_on=set(),
        )
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a_updated = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="new_content"),
            column_hashes={"col": "hash2"},
            depends_on=set(),
        )
    )
    assert snapshot_a_updated.version is None
    assert snapshot_a_updated.change_category is None

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot_a_updated.name: (snapshot_a_updated, snapshot_a)},
        snapshots={snapshot_a_updated.snapshot_id: snapshot_a_updated},
        new_snapshots={snapshot_a_updated.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, forward_only=True).build()
    assert snapshot_a_updated.version == snapshot_a_updated.fingerprint.to_version()
    assert snapshot_a_updated.change_category == SnapshotChangeCategory.BREAKING
    assert not snapshot_a_updated.is_forward_only


def test_seed_model_metadata_change_no_missing_intervals(
    make_snapshot: t.Callable[..., Snapshot],
):
    snapshot_a = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="content"),
            column_hashes={"col": "hash1"},
            depends_on=set(),
        )
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_a.add_interval("2022-01-01", now())

    snapshot_a_metadata_updated = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="content"),
            column_hashes={"col": "hash1"},
            depends_on=set(),
            description="foo",
        )
    )
    snapshot_a_metadata_updated.previous_versions = snapshot_a.all_versions
    assert snapshot_a_metadata_updated.version is None
    assert snapshot_a_metadata_updated.change_category is None

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a_metadata_updated.name: (snapshot_a_metadata_updated, snapshot_a)
        },
        snapshots={snapshot_a_metadata_updated.snapshot_id: snapshot_a_metadata_updated},
        new_snapshots={snapshot_a_metadata_updated.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff).build()
    assert snapshot_a_metadata_updated.change_category == SnapshotChangeCategory.METADATA
    assert not snapshot_a_metadata_updated.is_forward_only
    assert not plan.missing_intervals  # plan should have no missing intervals
    assert (
        snapshot_a_metadata_updated.intervals == snapshot_a.intervals
    )  # intervals should have been copied


def test_start_inference(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), start="2022-01-01")
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_b.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    snapshot_b.add_interval("2022-01-01", now())

    plan = PlanBuilder(context_diff).build()
    assert len(plan.missing_intervals) == 1
    assert plan.missing_intervals[0].snapshot_id == snapshot_a.snapshot_id
    assert plan.start == to_timestamp("2022-01-01")

    # Test inference from existing intervals
    context_diff.snapshots = {snapshot_b.snapshot_id: snapshot_b}
    plan = PlanBuilder(context_diff).build()
    assert not plan.missing_intervals
    assert plan.start == to_datetime("2022-01-01")


def test_auto_categorization(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    updated_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 2, ds")))

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff).build()

    assert updated_snapshot.version == updated_snapshot.fingerprint.to_version()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING


def test_auto_categorization_missing_schema_downstream(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, 2, ds")))

    # selects * from `tbl` which is not defined and has an unknown schema
    # therefore we can't be sure what is included in the star select
    downstream_snapshot = make_snapshot(
        SqlModel(name="b", query=parse_one("select * from tbl"), depends_on={'"a"'}),
        nodes={'"a"': snapshot.model},
    )
    downstream_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_downstream_snapshot = make_snapshot(
        downstream_snapshot.model,
        nodes={'"a"': updated_snapshot.model},
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            updated_snapshot.name: (updated_snapshot, snapshot),
            updated_downstream_snapshot.name: (updated_downstream_snapshot, downstream_snapshot),
        },
        snapshots={
            updated_snapshot.snapshot_id: updated_snapshot,
            updated_downstream_snapshot.snapshot_id: updated_downstream_snapshot,
        },
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff).build()

    assert updated_snapshot.version
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING


def test_broken_references(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds FROM a")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={snapshot_a.snapshot_id: snapshot_a.table_info},
        modified_snapshots={snapshot_b.name: (snapshot_b, snapshot_b)},
        snapshots={snapshot_b.snapshot_id: snapshot_b},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    # Make sure the downstream snapshot doesn't have any parents,
    # since its only parent has been removed from the project.
    assert not snapshot_b.parents

    with pytest.raises(
        PlanError,
        match=r"""Removed '"a"' are referenced in '"b"'.*""",
    ):
        PlanBuilder(context_diff).build()


def test_broken_references_external_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(ExternalModel(name="a", kind=dict(name=ModelKindName.EXTERNAL)))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds FROM a")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={snapshot_a.snapshot_id: snapshot_a.table_info},
        modified_snapshots={snapshot_b.name: (snapshot_b, snapshot_b)},
        snapshots={snapshot_b.snapshot_id: snapshot_b},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    # Make sure the downstream snapshot doesn't have any parents,
    # since its only parent has been removed from the project.
    assert not snapshot_b.parents

    # Shouldn't raise
    PlanBuilder(context_diff).build()


def test_effective_from(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a", query=parse_one("select 1, ds FROM b"), start="2023-01-01", dialect="duckdb"
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.add_interval("2023-01-01", "2023-03-01")

    updated_snapshot = make_snapshot(
        SqlModel(
            name="a", query=parse_one("select 2, ds FROM b"), start="2023-01-01", dialect="duckdb"
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(
        PlanError,
        match="Effective date can only be set for a forward-only plan.",
    ):
        PlanBuilder(context_diff).set_effective_from("2023-02-01").build()

    # The snapshot gets categorized as breaking in previous step so we want to reset that back to None
    updated_snapshot.change_category = None
    plan_builder = PlanBuilder(
        context_diff,
        forward_only=True,
        start="2023-01-01",
        end="2023-03-01",
        execution_time="2023-03-02 00:01:00",
        is_dev=True,
        end_bounded=True,
    )
    updated_snapshot.add_interval("2023-01-01", "2023-03-01")

    with pytest.raises(
        PlanError,
        match="Effective date cannot be in the future.",
    ):
        plan_builder.set_effective_from(now() + timedelta(days=1)).build()

    assert plan_builder.set_effective_from(None).build().effective_from is None
    assert updated_snapshot.effective_from is None

    plan_builder.set_effective_from("2023-02-01")
    plan_builder.set_start("2023-02-01")
    assert plan_builder.build().effective_from == "2023-02-01"
    assert updated_snapshot.effective_from == "2023-02-01"

    assert len(plan_builder.build().missing_intervals) == 1
    missing_intervals = plan_builder.build().missing_intervals[0]
    assert missing_intervals.intervals[0][0] == to_timestamp("2023-02-01")
    assert missing_intervals.intervals[-1][-1] == to_timestamp("2023-03-02")

    plan_builder.set_effective_from(None)
    assert plan_builder.build().effective_from is None
    assert updated_snapshot.effective_from is None


def test_effective_from_non_evaluatble_model(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            kind="EMBEDDED",
            query=parse_one("select 1, ds FROM b"),
            start="2023-01-01",
            dialect="duckdb",
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    updated_snapshot = make_snapshot(
        SqlModel(
            name="a",
            kind="EMBEDDED",
            query=parse_one("select 2, ds FROM b"),
            start="2023-01-01",
            dialect="duckdb",
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(
        context_diff,
        forward_only=True,
        start="2023-01-01",
        end="2023-03-01",
        is_dev=True,
    )

    plan_builder.set_effective_from("2023-02-01")
    assert plan_builder.build().effective_from == "2023-02-01"
    assert not updated_snapshot.effective_from


def test_new_environment_no_changes(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    with pytest.raises(
        PlanError, match="Creating a new environment requires a change, but project files match.*"
    ):
        PlanBuilder(context_diff, is_dev=True).build()

    assert PlanBuilder(context_diff).build().environment.promoted_snapshot_ids is None
    assert (
        PlanBuilder(context_diff, is_dev=True, include_unmodified=True)
        .build()
        .environment.promoted_snapshot_ids
        is None
    )


def test_new_environment_with_changes(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 3, ds")))

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot_a.name: (updated_snapshot_a, snapshot_a)},
        snapshots={
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={updated_snapshot_a.snapshot_id: updated_snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    # Modified the existing model.

    assert PlanBuilder(context_diff, is_dev=True).build().environment.promoted_snapshot_ids == [
        updated_snapshot_a.snapshot_id
    ]

    # Updating the existing environment with a previously promoted snapshot.
    context_diff.previously_promoted_snapshot_ids = {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    context_diff.is_new_environment = False
    assert set(
        PlanBuilder(context_diff, is_dev=True).build().environment.promoted_snapshot_ids or []
    ) == {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Adding a new model
    snapshot_c = make_snapshot(SqlModel(name="c", query=parse_one("select 4, ds")))
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)
    context_diff.snapshots = {
        updated_snapshot_a.snapshot_id: updated_snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
        snapshot_c.snapshot_id: snapshot_c,
    }
    context_diff.added = {snapshot_c.snapshot_id}
    context_diff.modified_snapshots = {}
    context_diff.new_snapshots = {snapshot_c.snapshot_id: snapshot_c}

    assert set(
        PlanBuilder(context_diff, is_dev=True).build().environment.promoted_snapshot_ids or []
    ) == {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
        snapshot_c.snapshot_id,
    }


def test_forward_only_models(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            dialect="duckdb",
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 3 as a, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
            dialect="duckdb",
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, is_dev=True).build()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING
    assert updated_snapshot.is_forward_only

    updated_snapshot.change_category = None
    updated_snapshot.version = None
    PlanBuilder(context_diff, is_dev=True, forward_only=True).build()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING
    assert updated_snapshot.is_forward_only

    updated_snapshot.change_category = None
    updated_snapshot.version = None
    PlanBuilder(context_diff, forward_only=True).build()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING
    assert updated_snapshot.is_forward_only


def test_forward_only_models_model_kind_changed(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds"), kind=FullKind()))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 3, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, is_dev=True).build()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING


@pytest.mark.parametrize(
    "partitioned_by, expected_forward_only",
    [
        ([], False),
        ([d.parse_one("ds")], True),
    ],
)
def test_forward_only_models_model_kind_changed_to_incremental_by_time_range(
    make_snapshot,
    partitioned_by: t.List[exp.Expr],
    expected_forward_only: bool,
):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            kind=IncrementalUnmanagedKind(),
            partitioned_by=partitioned_by,
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 3 as a, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, is_dev=True).build()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING
    assert updated_snapshot.is_forward_only == expected_forward_only


def test_indirectly_modified_forward_only_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as a, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 2 as a, ds")))
    updated_snapshot_a.previous_versions = snapshot_a.all_versions

    snapshot_b = make_snapshot(
        SqlModel(
            dialect="duckdb",
            name="b",
            query=parse_one("select a, ds from a"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        ),
        nodes={'"a"': snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    updated_snapshot_b = make_snapshot(snapshot_b.model, nodes={'"a"': updated_snapshot_a.model})
    updated_snapshot_b.previous_versions = snapshot_b.all_versions

    snapshot_c = make_snapshot(
        SqlModel(name="c", query=parse_one("select a, ds from b")), nodes={'"b"': snapshot_b.model}
    )
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_c = make_snapshot(
        snapshot_c.model, nodes={'"b"': updated_snapshot_b.model, '"a"': updated_snapshot_a.model}
    )
    updated_snapshot_c.previous_versions = snapshot_c.all_versions

    snapshot_d = make_snapshot(
        SqlModel(name="d", query=parse_one("select a.a from a, b")),
        nodes={
            '"a"': snapshot_a.model,
            '"b"': snapshot_b.model,
        },
    )
    snapshot_d.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_d = make_snapshot(
        snapshot_d.model, nodes={'"b"': updated_snapshot_b.model, '"a"': updated_snapshot_a.model}
    )
    updated_snapshot_d.previous_versions = snapshot_d.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            updated_snapshot_a.name: (updated_snapshot_a, snapshot_a),
            updated_snapshot_b.name: (updated_snapshot_b, snapshot_b),
            updated_snapshot_c.name: (updated_snapshot_c, snapshot_c),
            updated_snapshot_d.name: (updated_snapshot_d, snapshot_d),
        },
        snapshots={
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
            updated_snapshot_b.snapshot_id: updated_snapshot_b,
            updated_snapshot_c.snapshot_id: updated_snapshot_c,
            updated_snapshot_d.snapshot_id: updated_snapshot_d,
        },
        new_snapshots={
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
            updated_snapshot_b.snapshot_id: updated_snapshot_b,
            updated_snapshot_c.snapshot_id: updated_snapshot_c,
            updated_snapshot_d.snapshot_id: updated_snapshot_d,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff, is_dev=True).build()
    assert plan.indirectly_modified == {
        updated_snapshot_a.snapshot_id: {
            updated_snapshot_b.snapshot_id,
            updated_snapshot_c.snapshot_id,
            updated_snapshot_d.snapshot_id,
        }
    }

    assert plan.directly_modified == {updated_snapshot_a.snapshot_id}

    assert updated_snapshot_a.change_category == SnapshotChangeCategory.BREAKING
    assert updated_snapshot_b.change_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert updated_snapshot_c.change_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert updated_snapshot_d.change_category == SnapshotChangeCategory.INDIRECT_BREAKING

    assert not updated_snapshot_a.is_forward_only
    assert updated_snapshot_b.is_forward_only
    assert not updated_snapshot_c.is_forward_only
    assert not updated_snapshot_d.is_forward_only

    deployability_index = DeployabilityIndex.create(
        {
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
            updated_snapshot_b.snapshot_id: updated_snapshot_b,
            updated_snapshot_c.snapshot_id: updated_snapshot_c,
        }
    )
    assert deployability_index.is_representative(updated_snapshot_a)
    assert not deployability_index.is_representative(updated_snapshot_b)
    assert not deployability_index.is_representative(updated_snapshot_c)


def test_added_model_with_forward_only_parent(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as a, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select a, ds from a")))

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_b.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={snapshot_b.snapshot_id: snapshot_b},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, is_dev=True).build()
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING
    assert not snapshot_b.is_forward_only


def test_added_forward_only_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select a, ds from a")))

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_a.snapshot_id, snapshot_b.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff).build()
    assert snapshot_a.change_category == SnapshotChangeCategory.BREAKING
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING


@time_machine.travel("2026-08-22 15:00:00 UTC")
def test_disable_restatement(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", disable_restatement=True),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff, restate_models=['"a"']).build()
    assert not plan.restatements

    # Effective from doesn't apply to snapshots for which restatements are disabled.
    plan = PlanBuilder(context_diff, forward_only=True, effective_from="2023-01-01").build()
    assert plan.effective_from == "2023-01-01"
    assert snapshot.effective_from is None

    # Restatements should still be supported when in dev.
    plan = PlanBuilder(context_diff, is_dev=True, restate_models=['"a"']).build()
    assert plan.restatements == {
        snapshot.snapshot_id: (to_timestamp(plan.start), to_timestamp(to_date("tomorrow")))
    }

    # We don't want to restate a disable_restatement model if it is unpaused since that would be mean we are violating
    # the model kind property
    snapshot.unpaused_ts = 9999999999
    plan = PlanBuilder(context_diff, is_dev=True, restate_models=['"a"']).build()
    assert plan.restatements == {}


def test_revert_to_previous_value(make_snapshot, mocker: MockerFixture):
    """
    Make sure we can revert to previous snapshots with intervals if it already exists and not modify
    it's existing change category
    """
    old_snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), depends_on=set())
    )
    old_snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select 1, ds FROM a"), depends_on={"a"})
    )
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 2, ds"), depends_on=set())
    )
    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select 1, ds FROM a"), depends_on={"a"})
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    snapshot_b.add_interval("2022-01-01", now())

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a.name: (snapshot_a, old_snapshot_a),
            snapshot_b.name: (snapshot_b, old_snapshot_b),
        },
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(context_diff)
    plan_builder.set_choice(snapshot_a, SnapshotChangeCategory.BREAKING)
    plan_builder.build()
    # Make sure it does not get assigned INDIRECT_BREAKING
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING
    assert snapshot_b.is_forward_only


test_add_restatement_fixtures = [
    (
        "No dependencies single depends on self",
        {
            '"a"': {},
            '"b"': {},
        },
        {'"b"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "6 days ago"),
            '"b"': ("1 week ago", "today"),
        },
    ),
    (
        "Simple dependency with leaf depends on self",
        {
            '"a"': {},
            '"b"': {'"a"'},
        },
        {'"b"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "6 days ago"),
            '"b"': ("1 week ago", "today"),
        },
    ),
    (
        "Simple dependency with root depends on self",
        {
            '"a"': {},
            '"b"': {'"a"'},
        },
        {'"a"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "today"),
            '"b"': ("1 week ago", "today"),
        },
    ),
    (
        "Two unrelated subgraphs with root depends on self",
        {
            '"a"': {},
            '"b"': {},
            '"c"': {'"a"'},
            '"d"': {'"b"'},
        },
        {'"a"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "today"),
            '"b"': ("1 week ago", "6 days ago"),
            '"c"': ("1 week ago", "today"),
            '"d"': ("1 week ago", "6 days ago"),
        },
    ),
    (
        "Simple root depends on self with adjusted execution time",
        {
            '"a"': {},
            '"b"': {'"a"'},
        },
        {'"a"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "3 day ago",
        {
            '"a"': ("1 week ago", "2 days ago"),
            '"b"': ("1 week ago", "2 days ago"),
        },
    ),
    (
        """
        a -> c -> d
        b -> c -> e -> g
        b -> f -> g
        c depends on self
        restate a and b
        """,
        {
            '"a"': {},
            '"b"': {},
            '"c"': {'"a"', '"b"'},
            '"d"': {'"c"'},
            '"e"': {'"c"'},
            '"f"': {'"b"'},
            '"g"': {'"f"', '"e"'},
        },
        {'"c"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "6 days ago"),
            '"b"': ("1 week ago", "6 days ago"),
            '"c"': ("1 week ago", "today"),
            '"d"': ("1 week ago", "today"),
            '"e"': ("1 week ago", "today"),
            '"f"': ("1 week ago", "6 days ago"),
            '"g"': ("1 week ago", "today"),
        },
    ),
    (
        """
        a -> c -> d
        b -> c -> e -> g
        b -> f -> g
        c depends on self
        restate e
        """,
        {
            '"a"': {},
            '"b"': {},
            '"c"': {'"a"', '"b"'},
            '"d"': {'"c"'},
            '"e"': {'"c"'},
            '"f"': {'"b"'},
            '"g"': {'"f"', '"e"'},
        },
        {'"c"'},
        {'"e"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"e"': ("1 week ago", "6 days ago"),
            '"g"': ("1 week ago", "6 days ago"),
        },
    ),
]


@pytest.mark.parametrize(
    "graph,depends_on_self_names,restatement_names,start,end,execution_time,expected",
    [test[1:] for test in test_add_restatement_fixtures],
    ids=[test[0] for test in test_add_restatement_fixtures],
)
def test_add_restatements(
    graph: t.Dict[str, t.Set[str]],
    depends_on_self_names: t.Set[str],
    restatement_names: t.Set[str],
    start: str,
    end: str,
    execution_time: str,
    expected: t.Dict[str, t.Tuple[str, str]],
    make_snapshot,
    mocker,
):
    dag = DAG(graph)
    snapshots: t.Dict[str, Snapshot] = {}
    for snapshot_name in dag:
        depends_on = dag.upstream(snapshot_name)
        snapshot = make_snapshot(
            SqlModel(
                name=snapshot_name,
                kind=IncrementalByTimeRangeKind(time_column="ds"),
                cron="@daily",
                start="1 week ago",
                query=parse_one(
                    f"SELECT 1 FROM {snapshot_name}"
                    if snapshot_name in depends_on_self_names
                    else "SELECT 1"
                ),
                depends_on=depends_on,
            ),
            nodes={
                upstream_snapshot_name: snapshots[upstream_snapshot_name].model
                for upstream_snapshot_name in depends_on
            },
        )
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
        snapshots[snapshot_name] = snapshot

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot for snapshot in snapshots.values()},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(
        context_diff,
        start=to_date(start),
        end=to_date(end),
        execution_time=to_date(execution_time),
        restate_models=restatement_names,
    ).build()

    assert {s_id.name: interval for s_id, interval in plan.restatements.items()} == {
        name: (to_timestamp(to_date(start)), to_timestamp(to_date(end)))
        for name, (start, end) in expected.items()
    }


def test_dev_plan_depends_past(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            # self reference query so it depends_on_self
            query=parse_one("select 1, ds FROM a"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_child = make_snapshot(
        SqlModel(
            name="a_child",
            query=parse_one("select 1, ds FROM a"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
        nodes={'"a"': snapshot.model},
    )
    snapshot_child.categorize_as(SnapshotChangeCategory.BREAKING)
    unrelated_snapshot = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select 1, ds"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )
    unrelated_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.depends_on_self
    assert not snapshot_child.depends_on_self
    assert not unrelated_snapshot.depends_on_self
    assert snapshot_child.model.depends_on == {'"a"'}
    assert snapshot_child.parents == (snapshot.snapshot_id,)
    assert unrelated_snapshot.model.depends_on == set()

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot.snapshot_id, snapshot_child.snapshot_id, unrelated_snapshot.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot.snapshot_id: snapshot,
            snapshot_child.snapshot_id: snapshot_child,
            unrelated_snapshot.snapshot_id: unrelated_snapshot,
        },
        new_snapshots={
            snapshot.snapshot_id: snapshot,
            snapshot_child.snapshot_id: snapshot_child,
            unrelated_snapshot.snapshot_id: unrelated_snapshot,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    dev_plan_start_aligned = PlanBuilder(
        context_diff, start="2023-01-01", end="2023-01-10", is_dev=True
    ).build()
    assert len(dev_plan_start_aligned.new_snapshots) == 3
    assert sorted([x.name for x in dev_plan_start_aligned.new_snapshots]) == [
        '"a"',
        '"a_child"',
        '"b"',
    ]
    assert dev_plan_start_aligned.directly_modified == {
        snapshot.snapshot_id,
        snapshot_child.snapshot_id,
        unrelated_snapshot.snapshot_id,
    }
    assert dev_plan_start_aligned.indirectly_modified == {}

    dev_plan_start_ahead_of_model = PlanBuilder(
        context_diff, start="2023-01-02", end="2023-01-10", is_dev=True
    ).build()
    assert len(dev_plan_start_ahead_of_model.new_snapshots) == 3
    assert not dev_plan_start_ahead_of_model.deployability_index.is_deployable(snapshot)
    assert not dev_plan_start_ahead_of_model.deployability_index.is_deployable(snapshot_child)
    assert dev_plan_start_ahead_of_model.deployability_index.is_deployable(unrelated_snapshot)
    assert dev_plan_start_ahead_of_model.directly_modified == {
        snapshot.snapshot_id,
        snapshot_child.snapshot_id,
        unrelated_snapshot.snapshot_id,
    }
    assert dev_plan_start_ahead_of_model.indirectly_modified == {}


def test_dev_plan_depends_past_non_deployable(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            # self reference query so it depends_on_self
            query=parse_one("select 1, ds FROM a"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    updated_snapshot = make_snapshot(
        SqlModel(
            **{
                **snapshot.model.dict(),
                "query": parse_one("select 1, ds, 2 FROM a"),
            }
        ),
    )

    snapshot_child = make_snapshot(
        SqlModel(
            name="a_child",
            query=parse_one("select 1, ds FROM a"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        ),
        nodes={'"a"': updated_snapshot.model},
    )
    unrelated_snapshot = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select 1, ds"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )

    assert updated_snapshot.depends_on_self
    assert not snapshot_child.depends_on_self
    assert not unrelated_snapshot.depends_on_self
    assert snapshot_child.model.depends_on == {'"a"'}
    assert snapshot_child.parents == (updated_snapshot.snapshot_id,)
    assert unrelated_snapshot.model.depends_on == set()

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_child.snapshot_id, unrelated_snapshot.snapshot_id},
        removed_snapshots={},
        modified_snapshots={snapshot.name: (updated_snapshot, snapshot)},
        snapshots={
            updated_snapshot.snapshot_id: updated_snapshot,
            snapshot_child.snapshot_id: snapshot_child,
            unrelated_snapshot.snapshot_id: unrelated_snapshot,
        },
        new_snapshots={
            updated_snapshot.snapshot_id: snapshot,
            snapshot_child.snapshot_id: snapshot_child,
            unrelated_snapshot.snapshot_id: unrelated_snapshot,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    def new_builder(start, end):
        builder = PlanBuilder(context_diff, start=start, end=end, is_dev=True)
        builder.set_choice(updated_snapshot, SnapshotChangeCategory.BREAKING)
        builder.set_choice(snapshot_child, SnapshotChangeCategory.BREAKING)
        builder.set_choice(unrelated_snapshot, SnapshotChangeCategory.BREAKING)
        return builder

    dev_plan_start_aligned = new_builder("2023-01-01", "2023-01-10").build()
    assert sorted([x.name for x in dev_plan_start_aligned.new_snapshots]) == [
        '"a"',
        '"a_child"',
        '"b"',
    ]


def test_restatement_intervals_after_updating_start(sushi_context: Context):
    plan = sushi_context.plan(no_prompts=True, restate_models=["sushi.waiter_revenue_by_day"])
    snapshot_id = [
        snapshot.snapshot_id
        for snapshot in plan.snapshots
        if snapshot.name == '"memory"."sushi"."waiter_revenue_by_day"'
    ][0]
    restatement_interval = plan.restatements[snapshot_id]
    assert restatement_interval[0] == to_timestamp(plan.start)

    new_start = yesterday_ds()
    plan = sushi_context.plan(
        no_prompts=True, restate_models=["sushi.waiter_revenue_by_day"], start=new_start
    )
    new_restatement_interval = plan.restatements[snapshot_id]
    assert new_restatement_interval[0] == to_timestamp(new_start)
    assert new_restatement_interval != restatement_interval


def test_models_selected_for_backfill(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as one, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select one, ds from a")),
        nodes={'"a"': snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_b.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        new_snapshots={snapshot_b.snapshot_id: snapshot_b},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff).build()
    assert plan.is_selected_for_backfill('"a"')
    assert plan.is_selected_for_backfill('"b"')
    assert plan.models_to_backfill is None
    assert {i.snapshot_id for i in plan.missing_intervals} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    plan = PlanBuilder(context_diff, is_dev=True, backfill_models={'"a"'}).build()
    assert plan.is_selected_for_backfill('"a"')
    assert not plan.is_selected_for_backfill('"b"')
    assert plan.models_to_backfill == {'"a"'}
    assert {i.snapshot_id for i in plan.missing_intervals} == {snapshot_a.snapshot_id}
    assert plan.environment.promoted_snapshot_ids == [snapshot_a.snapshot_id]

    plan = PlanBuilder(context_diff, is_dev=True, backfill_models={'"b"'}).build()
    assert plan.is_selected_for_backfill('"a"')
    assert plan.is_selected_for_backfill('"b"')
    assert plan.models_to_backfill == {'"a"', '"b"'}
    assert {i.snapshot_id for i in plan.missing_intervals} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    assert plan.environment.promoted_snapshot_ids == [snapshot_b.snapshot_id]


def test_categorized_uncategorized(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    new_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 2, ds")))

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={new_snapshot.name: (new_snapshot, snapshot)},
        snapshots={new_snapshot.snapshot_id: new_snapshot},
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(context_diff, auto_categorization_enabled=False)

    plan = plan_builder.build()
    assert plan.uncategorized == [new_snapshot]
    assert not plan.categorized

    plan_builder.set_choice(new_snapshot, SnapshotChangeCategory.NON_BREAKING)

    plan = plan_builder.build()
    assert not plan.uncategorized
    assert plan.categorized == [new_snapshot]


def test_environment_previous_finalized_snapshots(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as one, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    updated_snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 4 as four, ds")))
    updated_snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2 as two, ds")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_c = make_snapshot(SqlModel(name="c", query=parse_one("select 3 as three, ds")))
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_d = make_snapshot(SqlModel(name="d", query=parse_one("select 5 as five, ds")))
    snapshot_d.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=False,
        is_unfinalized_environment=True,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_b.snapshot_id},
        removed_snapshots={snapshot_c.snapshot_id: snapshot_c.table_info},
        modified_snapshots={snapshot_a.name: (updated_snapshot_a, snapshot_a)},
        snapshots={
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
            snapshot_d.snapshot_id: snapshot_d,
        },
        new_snapshots={
            snapshot_b.snapshot_id: snapshot_b,
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=[snapshot_c.table_info, snapshot_d.table_info],
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff).build()
    assert set(plan.environment.previous_finalized_snapshots or []) == {
        snapshot_c.table_info,
        snapshot_d.table_info,
    }

    context_diff.is_unfinalized_environment = False

    plan = PlanBuilder(context_diff).build()
    assert set(plan.environment.previous_finalized_snapshots or []) == {
        snapshot_a.table_info,
        snapshot_c.table_info,
        snapshot_d.table_info,
    }


def test_metadata_change(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            dialect="duckdb",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    # Simulate a direct change.
    updated_snapshot = make_snapshot(
        SqlModel(
            **{
                **snapshot.model.dict(),
                "owner": "new_owner",
            }
        )
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff, is_dev=True).build()

    assert (
        plan.snapshots[updated_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.METADATA
    )


def test_plan_start_when_preview_enabled(make_snapshot, mocker: MockerFixture):
    model_start = "2024-01-01"
    model = SqlModel(
        name="a",
        query=parse_one("select 1, ds"),
        dialect="duckdb",
        kind=dict(
            name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", forward_only=True
        ),
        start=model_start,
    )

    snapshot = make_snapshot(model)
    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot.snapshot_id: snapshot,
        },
        new_snapshots={snapshot.snapshot_id: snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    default_start_for_preview = "2024-06-09"

    plan_builder = PlanBuilder(
        context_diff,
        default_start=default_start_for_preview,
        is_dev=True,
        enable_preview=True,
    )
    assert plan_builder.build().start == default_start_for_preview

    # When a model is modified then the backfill should be a preview.
    snapshot = make_snapshot(model)
    context_diff.added = set()
    context_diff.modified_snapshots = {snapshot.name: (snapshot, snapshot)}

    plan_builder = PlanBuilder(
        context_diff,
        default_start=default_start_for_preview,
        is_dev=True,
        enable_preview=True,
    )
    assert plan_builder.build().start == default_start_for_preview


def test_end_override_per_model(make_snapshot):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    new_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 2, ds")))

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={new_snapshot.name: (new_snapshot, snapshot)},
        snapshots={new_snapshot.snapshot_id: new_snapshot},
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(
        context_diff,
        end_override_per_model={snapshot.name: to_datetime("2023-01-09")},
    )
    assert plan_builder.build().end_override_per_model == {snapshot.name: to_datetime("2023-01-09")}

    # User-provided end should take precedence.
    plan_builder = PlanBuilder(
        context_diff,
        end_override_per_model={snapshot.name: to_datetime("2023-01-09")},
        end="2023-01-10",
        is_dev=True,
    )
    assert plan_builder.build().end_override_per_model is None


def test_unaligned_start_model_with_forward_only_preview(make_snapshot):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True, time_column="ds"
            ),
        )
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    new_snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 2 as a, ds"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, forward_only=True, time_column="ds"
            ),
        )
    )
    new_snapshot_a.previous_versions = snapshot_a.all_versions
    new_snapshot_a.unpaused_ts = 1

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select 1 AS key"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_UNIQUE_KEY, unique_key="key"),
            start="2024-01-01",
            depends_on={"a"},
        ),
        nodes={new_snapshot_a.name: new_snapshot_a.model},
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_b.snapshot_id},
        removed_snapshots={},
        snapshots={new_snapshot_a.snapshot_id: new_snapshot_a, snapshot_b.snapshot_id: snapshot_b},
        new_snapshots={
            new_snapshot_a.snapshot_id: new_snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
        },
        modified_snapshots={snapshot_a.name: (new_snapshot_a, snapshot_a)},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(
        context_diff,
        enable_preview=True,
        is_dev=True,
    )
    plan = plan_builder.build()

    assert set(plan.restatements) == {new_snapshot_a.snapshot_id, snapshot_b.snapshot_id}
    assert not plan.deployability_index.is_deployable(new_snapshot_a)
    assert not plan.deployability_index.is_deployable(snapshot_b)


def _make_forward_only_preview_context_diff(make_snapshot):
    old_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE,
                forward_only=True,
                time_column="ds",
            ),
            start="2025-01-01",
        )
    )
    old_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    new_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 2 as a, ds"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE,
                forward_only=True,
                time_column="ds",
            ),
            start="2025-01-01",
        )
    )
    new_snapshot.previous_versions = old_snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        snapshots={new_snapshot.snapshot_id: new_snapshot},
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        modified_snapshots={old_snapshot.name: (new_snapshot, old_snapshot)},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    return context_diff, new_snapshot


def test_forward_only_preview_start_does_not_override_implicit_backfill_start(make_snapshot):
    context_diff, new_snapshot = _make_forward_only_preview_context_diff(make_snapshot)

    normal_old_snapshot = make_snapshot(
        SqlModel(
            name="normal",
            query=parse_one("select 1, ds"),
            dialect="duckdb",
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
            start="2025-01-01",
        )
    )
    normal_old_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    normal_new_snapshot = make_snapshot(
        SqlModel(
            name="normal",
            query=parse_one("select 2, ds"),
            dialect="duckdb",
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
            start="2025-01-01",
        )
    )
    normal_new_snapshot.previous_versions = normal_old_snapshot.all_versions

    context_diff.modified_snapshots[normal_new_snapshot.name] = (
        normal_new_snapshot,
        normal_old_snapshot,
    )
    context_diff.snapshots[normal_new_snapshot.snapshot_id] = normal_new_snapshot
    context_diff.new_snapshots[normal_new_snapshot.snapshot_id] = normal_new_snapshot

    plan = PlanBuilder(
        context_diff,
        default_start="2025-01-01",
        end="2025-01-04",
        preview_start="2025-01-03",
        skip_backfill=False,
        is_dev=True,
        enable_preview=True,
    ).build()

    assert plan.start == "2025-01-01"
    assert plan.restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2025-01-03"),
            to_timestamp("2025-01-05"),
        )
    }
    missing_intervals = {i.snapshot_id: i.intervals for i in plan.missing_intervals}
    assert missing_intervals[normal_new_snapshot.snapshot_id] == [
        (to_timestamp("2025-01-01"), to_timestamp("2025-01-02")),
        (to_timestamp("2025-01-02"), to_timestamp("2025-01-03")),
        (to_timestamp("2025-01-03"), to_timestamp("2025-01-04")),
        (to_timestamp("2025-01-04"), to_timestamp("2025-01-05")),
    ]


def test_forward_only_preview_start_follows_implicit_plan_start_updates(make_snapshot):
    context_diff, new_snapshot = _make_forward_only_preview_context_diff(make_snapshot)

    plan_builder = PlanBuilder(
        context_diff,
        default_start="2025-01-03",
        end="2025-01-04",
        is_dev=True,
        forward_only=True,
        enable_preview=True,
    )

    assert plan_builder.build().restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2025-01-03"),
            to_timestamp("2025-01-05"),
        )
    }

    assert plan_builder.set_effective_from("2025-01-01").build().restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2025-01-01"),
            to_timestamp("2025-01-05"),
        )
    }

    assert plan_builder.set_start("2025-01-02").build().restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2025-01-02"),
            to_timestamp("2025-01-05"),
        )
    }


@time_machine.travel("2026-06-02 00:00:00 UTC")
def test_forward_only_preview_uses_preview_start(make_snapshot):
    context_diff, new_snapshot = _make_forward_only_preview_context_diff(make_snapshot)

    plan = PlanBuilder(
        context_diff,
        start="2025-01-01",
        preview_start="yesterday",
        preview_min_intervals=1,
        enable_preview=True,
        is_dev=True,
    ).build()

    assert plan.start == "2025-01-01"
    assert plan.restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2026-06-01"),
            to_timestamp("2026-06-02"),
        )
    }


@time_machine.travel("2026-06-02 00:00:00 UTC")
def test_forward_only_preview_min_intervals_expands_preview_start(make_snapshot):
    context_diff, new_snapshot = _make_forward_only_preview_context_diff(make_snapshot)

    plan = PlanBuilder(
        context_diff,
        start="2025-01-01",
        preview_start="now",
        preview_min_intervals=1,
        enable_preview=True,
        is_dev=True,
    ).build()

    assert plan.restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2026-06-01"),
            to_timestamp("2026-06-02"),
        )
    }


@time_machine.travel("2026-06-02 00:00:00 UTC")
def test_forward_only_preview_start_can_exceed_preview_min_intervals(make_snapshot):
    context_diff, new_snapshot = _make_forward_only_preview_context_diff(make_snapshot)

    plan = PlanBuilder(
        context_diff,
        start="2025-01-01",
        preview_start="2025-01-01",
        preview_min_intervals=1,
        enable_preview=True,
        is_dev=True,
    ).build()

    assert plan.restatements == {
        new_snapshot.snapshot_id: (
            to_timestamp("2025-01-01"),
            to_timestamp("2026-06-02"),
        )
    }


def test_restate_production_model_in_dev(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model_a",
            dialect="duckdb",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    prod_snapshot = make_snapshot(
        SqlModel(
            name="test_model_b",
            dialect="duckdb",
            query=parse_one("select 2, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )
    prod_snapshot.unpaused_ts = 1

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=False,
        is_unfinalized_environment=True,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot, prod_snapshot.snapshot_id: prod_snapshot},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    mock_console = mocker.Mock()

    plan = PlanBuilder(
        context_diff,
        is_dev=True,
        restate_models={snapshot.name, prod_snapshot.name},
        console=mock_console,
    ).build()

    assert len(plan.restatements) == 1
    assert prod_snapshot.snapshot_id not in plan.restatements

    mock_console.log_warning.assert_called_once_with(
        "Cannot restate model '\"test_model_b\"' because the current version is used in production. "
        "Run the restatement against the production environment instead to restate this model."
    )


@time_machine.travel("2025-02-23 15:00:00 UTC")
def test_restate_daily_to_monthly(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as one"),
            cron="@daily",
            start="2025-01-01",
        ),
    )

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select one from a"),
            cron="@monthly",
            start="2025-01-01",
        ),
        nodes={'"a"': snapshot_a.model},
    )

    snapshot_c = make_snapshot(
        SqlModel(
            name="c",
            query=parse_one("select one from b"),
            cron="@daily",
            start="2025-01-01",
        ),
        nodes={
            '"a"': snapshot_a.model,
            '"b"': snapshot_b.model,
        },
    )

    snapshot_d = make_snapshot(
        SqlModel(
            name="d",
            query=parse_one("select one from b union all select one from a"),
            cron="@daily",
            start="2025-01-01",
        ),
        nodes={
            '"a"': snapshot_a.model,
            '"b"': snapshot_b.model,
        },
    )
    snapshot_e = make_snapshot(
        SqlModel(
            name="e",
            query=parse_one("select one from b"),
            cron="@daily",
            start="2025-01-01",
        ),
        nodes={
            '"a"': snapshot_a.model,
            '"b"': snapshot_b.model,
        },
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=True,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={
            snapshot_a.snapshot_id: snapshot_a,
            snapshot_b.snapshot_id: snapshot_b,
            snapshot_c.snapshot_id: snapshot_c,
            snapshot_d.snapshot_id: snapshot_d,
            snapshot_e.snapshot_id: snapshot_e,
        },
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(
        context_diff,
        restate_models=[snapshot_a.name, snapshot_e.name],
        start="2025-02-15",
        end="2025-02-20",
    ).build()

    assert plan.restatements == {
        snapshot_a.snapshot_id: (1739577600000, 1740355200000),
        snapshot_b.snapshot_id: (1738368000000, 1740787200000),
        snapshot_c.snapshot_id: (1739577600000, 1740355200000),
        snapshot_d.snapshot_id: (1739577600000, 1740355200000),
        snapshot_e.snapshot_id: (1739577600000, 1740355200000),
    }


def test_plan_environment_statements_diff(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model_a",
            dialect="duckdb",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=False,
        is_unfinalized_environment=True,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        environment_statements=[
            EnvironmentStatements(
                before_all=["CREATE OR REPLACE TABLE table_1 AS SELECT 1", "@test_macro()"],
                after_all=["CREATE OR REPLACE TABLE table_2 AS SELECT 2"],
                python_env={
                    "test_macro": Executable(
                        payload="def test_macro(evaluator):\n    return 'one'"
                    ),
                },
            )
        ],
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
    )

    assert context_diff.has_changes
    assert context_diff.has_environment_statements_changes

    console_output, terminal_console = create_test_console()
    for _, diff in context_diff.environment_statements_diff():
        terminal_console._print(diff)
    output = console_output.getvalue()
    stripped = strip_ansi_codes(output)

    expected_output = (
        "before_all:\n"
        "  + CREATE OR REPLACE TABLE table_1 AS SELECT 1\n"
        "  + @test_macro()\n\n"
        "after_all:\n"
        "  + CREATE OR REPLACE TABLE table_2 AS SELECT 2"
    )
    assert stripped == expected_output
    console_output.close()

    # Validate with python env included
    console_output, terminal_console = create_test_console()
    for _, diff in context_diff.environment_statements_diff(include_python_env=True):
        terminal_console._print(diff)
    output = console_output.getvalue()
    stripped = strip_ansi_codes(output)
    expected_output = (
        "before_all:\n"
        "  + CREATE OR REPLACE TABLE table_1 AS SELECT 1\n"
        "  + @test_macro()\n\n"
        "after_all:\n"
        "  + CREATE OR REPLACE TABLE table_2 AS SELECT 2\n\n"
        "dependencies:\n"
        "@@ -0,0 +1,2 @@\n\n"
        "+def test_macro(evaluator):\n"
        "+    return 'one'"
    )
    assert stripped == expected_output
    console_output.close()


def test_set_choice_for_forward_only_model(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            dialect="duckdb",
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 3 as a, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
            dialect="duckdb",
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={updated_snapshot.name: (updated_snapshot, snapshot)},
        snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        new_snapshots={updated_snapshot.snapshot_id: updated_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder = PlanBuilder(context_diff, is_dev=True)
    plan_builder.set_choice(updated_snapshot, SnapshotChangeCategory.BREAKING)

    plan = plan_builder.build()
    assert (
        plan.snapshots[updated_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert plan.snapshots[updated_snapshot.snapshot_id].is_forward_only


def test_user_provided_flags(sushi_context: Context):
    expected_flags = {
        "run": True,
        "execution_time": "2025-01-01",
    }
    plan_a = sushi_context.plan(no_prompts=True, run=True, execution_time="2025-01-01")
    assert plan_a.user_provided_flags == expected_flags
    evaluatable_plan = plan_a.to_evaluatable()
    assert evaluatable_plan.user_provided_flags == expected_flags

    plan_b = sushi_context.plan()
    assert plan_b.user_provided_flags == {}
    evaluatable_plan_b = plan_b.to_evaluatable()
    assert evaluatable_plan_b.user_provided_flags == {}

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )
    plan_builder = PlanBuilder(
        context_diff,
        forward_only=True,
        user_provided_flags={"forward_only": True},
    ).build()
    assert plan_builder.user_provided_flags == {"forward_only": True}
    plan_builder = PlanBuilder(
        context_diff,
    ).build()
    assert plan_builder.user_provided_flags == None


@time_machine.travel(now())
@pytest.mark.parametrize(
    "input,output",
    [
        # execution_time, start, end
        (
            # no execution time, start or end
            (None, None, None),
            # execution time defaults to now()
            # start defaults to 1 day before execution time
            # end defaults to execution_time
            (now(), yesterday_ds(), now()),
        ),
        (
            # fixed execution time, no start, no end
            ("2020-01-05", None, None),
            # execution time set to 2020-01-05
            # start defaults to 1 day before execution time
            # end defaults to execution time
            ("2020-01-05", "2020-01-04", "2020-01-05"),
        ),
        (
            # fixed execution time, relative start, no end
            ("2020-01-05", "2 days ago", None),
            # execution time set to 2020-01-05
            # start relative to execution time
            # end defaults to execution time
            ("2020-01-05", "2020-01-03", "2020-01-05"),
        ),
        (
            # fixed execution time, relative start, relative end
            ("2020-01-05", "2 days ago", "1 day ago"),
            # execution time set to 2020-01-05
            # start relative to execution time
            # end relative to execution time
            ("2020-01-05", "2020-01-03", "2020-01-04"),
        ),
        (
            # fixed execution time, fixed start, fixed end
            ("2020-01-05", "2020-01-01", "2020-01-05"),
            # fixed dates are all in the valid range
            ("2020-01-05", "2020-01-01", "2020-01-05"),
        ),
        (
            # fixed execution time, fixed start, fixed end
            ("2020-01-05", "2020-01-05", "2020-01-01"),
            # Error because start is after end
            r"Plan end date.*must be after the plan start date",
        ),
        (
            # fixed execution time, relative start, fixed end beyond fixed execution time
            ("2020-01-05", "2 days ago", "2021-01-01"),
            # Error because end is set to 2021-01-01 which is after the execution time
            r"Plan end date.*cannot be in the future",
        ),
    ],
)
def test_plan_dates_relative_to_execution_time(
    input: t.Tuple[t.Optional[str], ...],
    output: t.Union[str, t.Tuple[t.Optional[str], ...]],
    make_snapshot: t.Callable,
):
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), dialect="duckdb")
    )

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added={snapshot_a.snapshot_id},
        removed_snapshots={},
        modified_snapshots={},
        snapshots={},
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    input_execution_time, input_start, input_end = input

    def _build_plan() -> Plan:
        return PlanBuilder(
            context_diff,
            start=input_start,
            end=input_end,
            execution_time=input_execution_time,
            is_dev=True,
        ).build()

    if isinstance(output, str):
        with pytest.raises(PlanError, match=output):
            _build_plan()
    else:
        output_execution_time, output_start, output_end = output

        plan = _build_plan()
        assert to_datetime(plan.start) == to_datetime(output_start)
        assert to_datetime(plan.end) == to_datetime(output_end)
        assert to_datetime(plan.execution_time) == to_datetime(output_execution_time)


def test_plan_builder_additive_change_error_blocks_plan(make_snapshot):
    """Test that additive changes block plan when on_additive_change=ERROR."""
    # Create models with actual schema differences
    # Use explicit column schemas in CTE so columns_to_types can be determined
    old_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    # New model with additional column (additive change)
    new_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'email@test.com'::VARCHAR as email, '2022-01-01'::DATE as ds
            )
            select id, name, email, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    old_snapshot = make_snapshot(old_model)
    new_snapshot = make_snapshot(new_model)

    # Set previous versions to simulate a modification
    new_snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="old_data_hash",
                metadata_hash="old_metadata_hash",
            ),
            version="old_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
            dev_table_suffix="dev",
        ),
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={old_snapshot.name: (new_snapshot, old_snapshot)},
        snapshots={
            old_snapshot.snapshot_id: old_snapshot,
            new_snapshot.snapshot_id: new_snapshot,
        },
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    builder = PlanBuilder(context_diff, forward_only=True)

    # Should raise PlanError for additive changes when on_additive_change=ERROR
    with pytest.raises(PlanError, match="additive change"):
        builder.build()


def test_plan_builder_additive_change_warn_allows_plan(make_snapshot):
    """Test that additive changes allow plan with warning when on_additive_change=WARN."""
    old_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.WARN,
        ),
    )

    new_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'email@test.com'::VARCHAR as email, '2022-01-01'::DATE as ds
            )
            select id, name, email, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.WARN,
        ),
    )

    old_snapshot = make_snapshot(old_model)
    new_snapshot = make_snapshot(new_model)

    # Set previous versions to simulate a modification
    new_snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="old_data_hash",
                metadata_hash="old_metadata_hash",
            ),
            version="old_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
            dev_table_suffix="dev",
        ),
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={old_snapshot.name: (new_snapshot, old_snapshot)},
        snapshots={
            old_snapshot.snapshot_id: old_snapshot,
            new_snapshot.snapshot_id: new_snapshot,
        },
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    builder = PlanBuilder(context_diff, forward_only=True)

    # Should log warning but not fail
    with patch.object(builder._console, "log_additive_change") as mock_log_additive:
        plan = builder.build()
        assert plan is not None
        mock_log_additive.assert_called()  # Should have logged an additive change


def test_plan_builder_additive_change_allow_permits_plan(make_snapshot):
    """Test that additive changes are permitted when on_additive_change=ALLOW."""
    old_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ALLOW,
        ),
    )

    new_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'email@test.com'::VARCHAR as email, '2022-01-01'::DATE as ds
            )
            select id, name, email, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ALLOW,
        ),
    )

    old_snapshot = make_snapshot(old_model)
    new_snapshot = make_snapshot(new_model)

    # Set previous versions to simulate a modification
    new_snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="old_data_hash",
                metadata_hash="old_metadata_hash",
            ),
            version="old_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
            dev_table_suffix="dev",
        ),
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={old_snapshot.name: (new_snapshot, old_snapshot)},
        snapshots={
            old_snapshot.snapshot_id: old_snapshot,
            new_snapshot.snapshot_id: new_snapshot,
        },
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    builder = PlanBuilder(context_diff, forward_only=True)

    # Should build plan without issues
    plan = builder.build()
    assert plan is not None


def test_plan_builder_additive_change_ignore_skips_validation(make_snapshot):
    """Test that additive changes are ignored when on_additive_change=IGNORE."""
    old_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.IGNORE,
        ),
    )

    new_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'email@test.com'::VARCHAR as email, '2022-01-01'::DATE as ds
            )
            select id, name, email, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.IGNORE,
        ),
    )

    old_snapshot = make_snapshot(old_model)
    new_snapshot = make_snapshot(new_model)

    # Set previous versions to simulate a modification
    new_snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="old_data_hash",
                metadata_hash="old_metadata_hash",
            ),
            version="old_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
            dev_table_suffix="dev",
        ),
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={old_snapshot.name: (new_snapshot, old_snapshot)},
        snapshots={
            old_snapshot.snapshot_id: old_snapshot,
            new_snapshot.snapshot_id: new_snapshot,
        },
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    builder = PlanBuilder(context_diff, forward_only=True)

    # Should build plan without any validation
    with patch("sqlmesh.core.plan.builder.logger.warning") as mock_warning:
        plan = builder.build()
        assert plan is not None
        mock_warning.assert_not_called()  # Should not log any warnings


def test_plan_builder_mixed_destructive_and_additive_changes(make_snapshot):
    """Test scenarios with both destructive and additive changes."""
    # Test case: on_destructive_change=IGNORE, on_additive_change=ERROR
    # Should ignore destructive changes but error on additive changes
    old_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'old_value'::VARCHAR as old_col, '2022-01-01'::DATE as ds
            )
            select id, name, old_col, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=OnDestructiveChange.IGNORE,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    new_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'new_value'::VARCHAR as new_col, '2022-01-01'::DATE as ds
            )
            select id, name, new_col, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=OnDestructiveChange.IGNORE,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    old_snapshot = make_snapshot(old_model)
    new_snapshot = make_snapshot(new_model)

    # Set previous versions to simulate a modification
    new_snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="old_data_hash",
                metadata_hash="old_metadata_hash",
            ),
            version="old_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
            dev_table_suffix="dev",
        ),
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={old_snapshot.name: (new_snapshot, old_snapshot)},
        snapshots={
            old_snapshot.snapshot_id: old_snapshot,
            new_snapshot.snapshot_id: new_snapshot,
        },
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    builder = PlanBuilder(context_diff, forward_only=True)

    # Should error on additive change (new_col), but ignore destructive change (old_col removal)
    with pytest.raises(PlanError, match="additive change"):
        builder.build()


def test_plan_builder_allow_additive_models_flag(make_snapshot):
    """Test that --allow-additive-model flag overrides on_additive_change=ERROR."""
    old_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    # New model with additional column (additive change)
    new_model = SqlModel(
        name="test_model",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'email@test.com'::VARCHAR as email, '2022-01-01'::DATE as ds
            )
            select id, name, email, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    old_snapshot = make_snapshot(old_model)
    new_snapshot = make_snapshot(new_model)

    # Set previous versions to simulate a modification
    new_snapshot.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="old_data_hash",
                metadata_hash="old_metadata_hash",
            ),
            version="old_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
            dev_table_suffix="dev",
        ),
    )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={new_snapshot.name: (new_snapshot, old_snapshot)},
        snapshots={new_snapshot.snapshot_id: new_snapshot},
        new_snapshots={new_snapshot.snapshot_id: new_snapshot},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    # First, verify that without the flag, the plan fails with additive change error
    builder = PlanBuilder(context_diff, forward_only=True)
    with pytest.raises(PlanError, match="additive change"):
        builder.build()

    # Now test that the --allow-additive-model flag allows the plan to succeed
    builder_with_flag = PlanBuilder(
        context_diff,
        forward_only=True,
        allow_additive_models={'"test_model"'},
    )

    # Should succeed without raising an exception
    plan = builder_with_flag.build()
    assert plan is not None


def test_plan_builder_allow_additive_models_pattern_matching(make_snapshot):
    """Test that --allow-additive-model flag supports pattern matching like destructive models."""
    # Create two models with additive changes
    old_model_1 = SqlModel(
        name="test.model_1",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    new_model_1 = SqlModel(
        name="test.model_1",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'email@test.com'::VARCHAR as email, '2022-01-01'::DATE as ds
            )
            select id, name, email, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    old_model_2 = SqlModel(
        name="other.model_2",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, '2022-01-01'::DATE as ds
            )
            select id, name, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    new_model_2 = SqlModel(
        name="other.model_2",
        dialect="duckdb",
        query=parse_one("""
            with source as (
                select 1::INT as id, 'test'::VARCHAR as name, 'phone'::VARCHAR as phone, '2022-01-01'::DATE as ds
            )
            select id, name, phone, ds from source
        """),
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
    )

    old_snapshot_1 = make_snapshot(old_model_1)
    new_snapshot_1 = make_snapshot(new_model_1)
    old_snapshot_2 = make_snapshot(old_model_2)
    new_snapshot_2 = make_snapshot(new_model_2)

    # Set previous versions to simulate modifications
    for new_snapshot in [new_snapshot_1, new_snapshot_2]:
        new_snapshot.previous_versions = (
            SnapshotDataVersion(
                fingerprint=SnapshotFingerprint(
                    data_hash="old_data_hash",
                    metadata_hash="old_metadata_hash",
                ),
                version="old_version",
                change_category=SnapshotChangeCategory.FORWARD_ONLY,
                dev_table_suffix="dev",
            ),
        )

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            new_snapshot_1.name: (new_snapshot_1, old_snapshot_1),
            new_snapshot_2.name: (new_snapshot_2, old_snapshot_2),
        },
        snapshots={
            new_snapshot_1.snapshot_id: new_snapshot_1,
            new_snapshot_2.snapshot_id: new_snapshot_2,
        },
        new_snapshots={
            new_snapshot_1.snapshot_id: new_snapshot_1,
            new_snapshot_2.snapshot_id: new_snapshot_2,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    # Test pattern matching: allow only models in "test" schema
    # In real usage, patterns would be expanded by Context.expand_model_selections
    # Here we simulate what the expansion would produce
    builder_with_pattern = PlanBuilder(
        context_diff,
        forward_only=True,
        allow_additive_models={'"test"."model_1"'},  # Only allow test.model_1, not other.model_2
    )

    # Should still fail because other.model_2 is not allowed
    with pytest.raises(PlanError, match="additive change"):
        builder_with_pattern.build()

    # Test allowing both patterns
    builder_with_both = PlanBuilder(
        context_diff,
        forward_only=True,
        allow_additive_models={'"test"."model_1"', '"other"."model_2"'},  # Allow both models
    )

    # Should succeed
    plan = builder_with_both.build()
    assert plan is not None


def test_environment_statements_change_allows_dev_environment_creation(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            dialect="duckdb",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )

    # First context diff of a new 'dev' environment without environment statements
    context_diff_no_statements = ContextDiff(
        environment="dev",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids={snapshot.snapshot_id},
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
        previous_environment_statements=[],
    )

    # Should fail because no changes
    plan_builder = PlanBuilder(
        context_diff_no_statements,
        is_dev=True,
    )

    with pytest.raises(NoChangesPlanError, match="Creating a new environment requires a change"):
        plan_builder.build()

    # Now create context diff with environment statements
    environment_statements = [
        EnvironmentStatements(
            before_all=["CREATE TABLE IF NOT EXISTS test_table (id INT)"],
            after_all=[],
            python_env={},
            jinja_macros=None,
        )
    ]

    context_diff_with_statements = ContextDiff(
        environment="dev",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot.snapshot_id: snapshot},
        new_snapshots={},
        previous_plan_id=None,
        previously_promoted_snapshot_ids={snapshot.snapshot_id},
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=environment_statements,
        previous_environment_statements=[],
    )

    # Should succeed because there are environment statements changes
    plan_builder_with_statements = PlanBuilder(
        context_diff_with_statements,
        is_dev=True,
    )

    # Test that allows creating a dev environment without other changes
    plan = plan_builder_with_statements.build()
    assert plan is not None
    assert plan.context_diff.has_environment_statements_changes
    assert plan.context_diff.environment_statements == environment_statements


def test_plan_ignore_cron_flag(make_snapshot):
    snapshot_a = make_snapshot(
        SqlModel(
            name="test_model",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            cron="@daily",  # Daily cron schedule
            start="2023-01-01",
            query=parse_one("SELECT 1 as id, ds FROM VALUES ('2023-01-01') t(ds)"),
            allow_partials=True,
        )
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=False)

    context_diff = ContextDiff(
        environment="dev",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={},
        snapshots={snapshot_a.snapshot_id: snapshot_a},
        new_snapshots={snapshot_a.snapshot_id: snapshot_a},
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan_builder_ignore_cron = PlanBuilder(
        context_diff,
        start="2023-01-01",
        execution_time="2023-01-05 12:00:00",
        is_dev=True,
        include_unmodified=True,
        ignore_cron=True,
        end_bounded=False,
    )

    plan = plan_builder_ignore_cron.build()
    assert plan.ignore_cron is True
    assert plan.to_evaluatable().ignore_cron is True

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot_a.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-05 12:00:00")),
            ],
        )
    ]


def test_indirect_change_to_materialized_view_is_breaking(make_snapshot):
    snapshot_a_old = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as col_a"),
            kind=ViewKind(materialized=True),
        )
    )
    snapshot_a_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b_old = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select col_a from a"),
            kind=ViewKind(materialized=True),
        ),
        nodes={'"a"': snapshot_a_old.model},
    )
    snapshot_b_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a_new = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as col_a, 2 as col_b"),
            kind=ViewKind(materialized=True),
        )
    )

    snapshot_a_new.previous_versions = snapshot_a_old.all_versions

    snapshot_b_new = make_snapshot(
        snapshot_b_old.model,
        nodes={'"a"': snapshot_a_new.model},
    )
    snapshot_b_new.previous_versions = snapshot_b_old.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a_new.name: (snapshot_a_new, snapshot_a_old),
            snapshot_b_new.name: (snapshot_b_new, snapshot_b_old),
        },
        snapshots={
            snapshot_a_new.snapshot_id: snapshot_a_new,
            snapshot_b_new.snapshot_id: snapshot_b_new,
        },
        new_snapshots={
            snapshot_a_new.snapshot_id: snapshot_a_new,
            snapshot_b_new.snapshot_id: snapshot_b_new,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, forward_only=False).build()

    assert snapshot_b_new.change_category == SnapshotChangeCategory.INDIRECT_BREAKING


def test_forward_only_indirect_change_to_materialized_view(make_snapshot):
    snapshot_a_old = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as col_a"),
        )
    )
    snapshot_a_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b_old = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select col_a from a"),
            kind=ViewKind(materialized=True),
        ),
        nodes={'"a"': snapshot_a_old.model},
    )
    snapshot_b_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a_new = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as col_a, 2 as col_b"),
        )
    )

    snapshot_a_new.previous_versions = snapshot_a_old.all_versions

    snapshot_b_new = make_snapshot(
        snapshot_b_old.model,
        nodes={'"a"': snapshot_a_new.model},
    )
    snapshot_b_new.previous_versions = snapshot_b_old.all_versions

    context_diff = ContextDiff(
        environment="test_environment",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snapshot_a_new.name: (snapshot_a_new, snapshot_a_old),
            snapshot_b_new.name: (snapshot_b_new, snapshot_b_old),
        },
        snapshots={
            snapshot_a_new.snapshot_id: snapshot_a_new,
            snapshot_b_new.snapshot_id: snapshot_b_new,
        },
        new_snapshots={
            snapshot_a_new.snapshot_id: snapshot_a_new,
            snapshot_b_new.snapshot_id: snapshot_b_new,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    PlanBuilder(context_diff, forward_only=True).build()

    # Forward-only indirect changes to MVs should not always be classified as indirect breaking.
    # Instead, we want to preserve the standard categorization.
    assert snapshot_b_new.change_category == SnapshotChangeCategory.INDIRECT_NON_BREAKING


def _hydrate_model_schemas(*models: Model) -> None:
    dag: DAG[str] = DAG()
    model_map: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    for m in models:
        model_map[m.fqn] = m
        dag.add(m.fqn, m.depends_on)
    with TemporaryDirectory() as tmp_dir:
        update_model_schemas(dag, model_map, Path(tmp_dir))


def _build_snapshots_and_diff(
    make_snapshot: t.Callable,
    old_models: t.Sequence[Model],
    new_models: t.Sequence[Model],
    *,
    environment: str = "dev",
    is_new_environment: bool = True,
    new_snapshot_names: t.Optional[t.Set[str]] = None,
) -> t.Tuple[t.Dict[str, Snapshot], t.Dict[str, Snapshot], ContextDiff]:
    _hydrate_model_schemas(*old_models)
    _hydrate_model_schemas(*new_models)

    old_nodes = {m.fqn: m for m in old_models}
    new_nodes = {m.fqn: m for m in new_models}

    old_snapshots: t.Dict[str, Snapshot] = {}
    for m in old_models:
        snap = make_snapshot(m, nodes=old_nodes)
        snap.categorize_as(SnapshotChangeCategory.BREAKING)
        old_snapshots[m.fqn] = snap

    new_snapshots: t.Dict[str, Snapshot] = {}
    for m in new_models:
        snap = make_snapshot(m, nodes=new_nodes)
        if m.fqn in old_snapshots:
            snap.previous_versions = old_snapshots[m.fqn].all_versions
        new_snapshots[m.fqn] = snap

    modified_snapshots = {
        fqn: (new_snap, old_snapshots[fqn])
        for fqn, new_snap in new_snapshots.items()
        if fqn in old_snapshots and new_snap.fingerprint != old_snapshots[fqn].fingerprint
    }
    snapshots_by_id = {snap.snapshot_id: snap for snap in new_snapshots.values()}
    if new_snapshot_names is None:
        new_snapshots_by_id = {
            snap.snapshot_id: snap
            for fqn, snap in new_snapshots.items()
            if fqn in modified_snapshots
        }
    else:
        new_snapshots_by_id = {
            snap.snapshot_id: snap
            for fqn, snap in new_snapshots.items()
            if fqn in new_snapshot_names
        }

    context_diff = ContextDiff(
        environment=environment,
        is_new_environment=is_new_environment,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots=modified_snapshots,
        snapshots=snapshots_by_id,
        new_snapshots=new_snapshots_by_id,
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )
    return old_snapshots, new_snapshots, context_diff


def test_indirectly_modified_forward_only_model_destructive_change(
    make_snapshot, mocker: MockerFixture
):
    """Gap S2 (destructive): Upstream FULL model incompatibly alters a column type or drops a
    nested struct field while an indirectly_modified downstream forward-only model selects it."""
    up_old = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=FullKind(),
        query=parse_one(
            "SELECT 1::INT AS id, '1'::TEXT AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )
    up_new = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=FullKind(),
        query=parse_one(
            "SELECT 1::INT AS id, 1::INT AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )

    def _make_time_range_diff(on_destructive: OnDestructiveChange) -> ContextDiff:
        down_old = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=True,
                on_destructive_change=on_destructive,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        down_new = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=True,
                on_destructive_change=on_destructive,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        _, _, diff = _build_snapshots_and_diff(
            make_snapshot, [up_old.copy(), down_old], [up_new.copy(), down_new]
        )
        assert diff.indirectly_modified('"db"."down"')
        return diff

    # 1. ERROR: logs destructive change with error=True and raises PlanError
    console = TerminalConsole()
    log_destructive_spy = mocker.spy(console, "log_destructive_change")
    diff_error = _make_time_range_diff(OnDestructiveChange.ERROR)
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_error, console=console).build()
    assert log_destructive_spy.call_count == 1
    assert log_destructive_spy.call_args[0][0] == '"db"."down"'
    assert log_destructive_spy.call_args[1]["error"] is True

    # 2. allow_destructive_models overrides ERROR without logging or raising
    log_destructive_spy.reset_mock()
    diff_allowed = _make_time_range_diff(OnDestructiveChange.ERROR)
    assert PlanBuilder(
        diff_allowed,
        allow_destructive_models={'"db"."down"'},
        console=console,
    ).build()
    assert log_destructive_spy.call_count == 0

    # 3. WARN: logs destructive change with error=False without raising
    log_destructive_spy.reset_mock()
    diff_warn = _make_time_range_diff(OnDestructiveChange.WARN)
    assert PlanBuilder(diff_warn, console=console).build()
    assert log_destructive_spy.call_count == 1
    assert log_destructive_spy.call_args[0][0] == '"db"."down"'
    assert log_destructive_spy.call_args[1]["error"] is False

    # 4. ALLOW & IGNORE: succeed silently without logging
    for permissive_mode in (OnDestructiveChange.ALLOW, OnDestructiveChange.IGNORE):
        log_destructive_spy.reset_mock()
        diff_permissive = _make_time_range_diff(permissive_mode)
        assert PlanBuilder(diff_permissive, console=console).build()
        assert log_destructive_spy.call_count == 0

    # 5. INCREMENTAL_BY_PARTITION (forward_only=True by default)
    part_old = SqlModel(
        name="db.part_down",
        dialect="postgres",
        kind=IncrementalByPartitionKind(),
        partitioned_by=["ds"],
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    part_new = SqlModel(
        name="db.part_down",
        dialect="postgres",
        kind=IncrementalByPartitionKind(),
        partitioned_by=["ds"],
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    _, _, diff_part = _build_snapshots_and_diff(
        make_snapshot, [up_old.copy(), part_old], [up_new.copy(), part_new]
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_part).build()

    # 6. SCD_TYPE_2_BY_TIME (forward_only=True by default)
    scd_old = SqlModel(
        name="db.scd_down",
        dialect="postgres",
        kind=SCDType2ByTimeKind(unique_key=["id"], updated_at_name="ds"),
        query=parse_one("SELECT id, val, ds FROM db.up", read="postgres"),
    )
    scd_new = SqlModel(
        name="db.scd_down",
        dialect="postgres",
        kind=SCDType2ByTimeKind(unique_key=["id"], updated_at_name="ds"),
        query=parse_one("SELECT id, val, ds FROM db.up", read="postgres"),
    )
    _, _, diff_scd = _build_snapshots_and_diff(
        make_snapshot, [up_old.copy(), scd_old], [up_new.copy(), scd_new]
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_scd).build()

    # 7. STRUCT nested field drop on indirectly_modified forward-only downstream model (databricks)
    struct_up_old = SqlModel(
        name="db.struct_up",
        dialect="databricks",
        kind=FullKind(),
        query=parse_one(
            "SELECT CAST(NAMED_STRUCT('a', 1, 'b', 'x') AS STRUCT<a: INT, b: STRING>) AS info, "
            "CAST('2022-01-01' AS DATE) AS ds",
            read="databricks",
        ),
    )
    struct_up_new = SqlModel(
        name="db.struct_up",
        dialect="databricks",
        kind=FullKind(),
        query=parse_one(
            "SELECT CAST(NAMED_STRUCT('a', 1) AS STRUCT<a: INT>) AS info, "
            "CAST('2022-01-01' AS DATE) AS ds",
            read="databricks",
        ),
    )
    struct_down_old = SqlModel(
        name="db.struct_down",
        dialect="databricks",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        query=parse_one("SELECT info, ds FROM db.struct_up", read="databricks"),
    )
    struct_down_new = SqlModel(
        name="db.struct_down",
        dialect="databricks",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        query=parse_one("SELECT info, ds FROM db.struct_up", read="databricks"),
    )
    _, _, diff_struct_drop = _build_snapshots_and_diff(
        make_snapshot, [struct_up_old, struct_down_old], [struct_up_new, struct_down_new]
    )
    assert diff_struct_drop.indirectly_modified('"db"."struct_down"')
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_struct_drop).build()


def test_indirectly_modified_forward_only_model_additive_change(
    make_snapshot, mocker: MockerFixture
):
    """Gap S2 (additive): Upstream FULL model triggers a compatible type widening or nested struct
    field addition on an indirectly_modified downstream forward-only model."""
    up_old = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=FullKind(),
        query=parse_one(
            "SELECT 'x'::VARCHAR(10) AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )
    up_new = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=FullKind(),
        query=parse_one(
            "SELECT 'x'::VARCHAR(50) AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )

    def _make_additive_diff(on_additive: OnAdditiveChange) -> ContextDiff:
        down_old = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=True,
                on_additive_change=on_additive,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        down_new = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=True,
                on_additive_change=on_additive,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        _, _, diff = _build_snapshots_and_diff(
            make_snapshot, [up_old.copy(), down_old], [up_new.copy(), down_new]
        )
        assert diff.indirectly_modified('"db"."down"')
        return diff

    # 1. ERROR: logs additive change with error=True and raises PlanError
    console = TerminalConsole()
    log_additive_spy = mocker.spy(console, "log_additive_change")
    diff_error = _make_additive_diff(OnAdditiveChange.ERROR)
    with pytest.raises(
        PlanError, match=r"Plan requires an additive change to a forward-only model\."
    ):
        PlanBuilder(diff_error, console=console).build()
    assert log_additive_spy.call_count == 1
    assert log_additive_spy.call_args[0][0] == '"db"."down"'
    assert log_additive_spy.call_args[1]["error"] is True

    # 2. allow_additive_models overrides ERROR without logging or raising
    log_additive_spy.reset_mock()
    diff_allowed = _make_additive_diff(OnAdditiveChange.ERROR)
    assert PlanBuilder(
        diff_allowed,
        allow_additive_models={'"db"."down"'},
        console=console,
    ).build()
    assert log_additive_spy.call_count == 0

    # 3. WARN: logs additive change with error=False without raising
    log_additive_spy.reset_mock()
    diff_warn = _make_additive_diff(OnAdditiveChange.WARN)
    assert PlanBuilder(diff_warn, console=console).build()
    assert log_additive_spy.call_count == 1
    assert log_additive_spy.call_args[0][0] == '"db"."down"'
    assert log_additive_spy.call_args[1]["error"] is False

    # 4. ALLOW & IGNORE: succeed silently without logging
    for permissive_mode in (OnAdditiveChange.ALLOW, OnAdditiveChange.IGNORE):
        log_additive_spy.reset_mock()
        diff_permissive = _make_additive_diff(permissive_mode)
        assert PlanBuilder(diff_permissive, console=console).build()
        assert log_additive_spy.call_count == 0

    # 5. STRUCT nested field addition on indirectly_modified forward-only model (databricks)
    struct_up_old = SqlModel(
        name="db.struct_up",
        dialect="databricks",
        kind=FullKind(),
        query=parse_one(
            "SELECT CAST(NAMED_STRUCT('a', 1) AS STRUCT<a: INT>) AS info, "
            "CAST('2022-01-01' AS DATE) AS ds",
            read="databricks",
        ),
    )
    struct_up_new = SqlModel(
        name="db.struct_up",
        dialect="databricks",
        kind=FullKind(),
        query=parse_one(
            "SELECT CAST(NAMED_STRUCT('a', 1, 'b', 'x') AS STRUCT<a: INT, b: STRING>) AS info, "
            "CAST('2022-01-01' AS DATE) AS ds",
            read="databricks",
        ),
    )
    struct_down_old = SqlModel(
        name="db.struct_down",
        dialect="databricks",
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
        query=parse_one("SELECT info, ds FROM db.struct_up", read="databricks"),
    )
    struct_down_new = SqlModel(
        name="db.struct_down",
        dialect="databricks",
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_additive_change=OnAdditiveChange.ERROR,
        ),
        query=parse_one("SELECT info, ds FROM db.struct_up", read="databricks"),
    )
    _, _, diff_struct_add = _build_snapshots_and_diff(
        make_snapshot, [struct_up_old, struct_down_old], [struct_up_new, struct_down_new]
    )
    assert diff_struct_add.indirectly_modified('"db"."struct_down"')
    with pytest.raises(
        PlanError, match=r"Plan requires an additive change to a forward-only model\."
    ):
        PlanBuilder(diff_struct_add).build()


def test_indirect_downstream_inherits_forward_only_and_view_embedded_handling(
    make_snapshot, mocker: MockerFixture
):
    """Gap S3 + #2576 non-regression + VIEW/EMBEDDED intermediate & leaf handling."""
    # 1. Scenario S3: upstream forward_only=True (on_destructive_change=ALLOW) alters val TEXT->INT;
    # downstream has model.forward_only=False (default on_destructive_change=ERROR) and inherits
    # forward_only=True from all_upstream_forward_only == {True}, so PlanBuilder MUST raise PlanError.
    up_old = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=OnDestructiveChange.ALLOW,
        ),
        query=parse_one("SELECT '1'::TEXT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    up_new = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=OnDestructiveChange.ALLOW,
        ),
        query=parse_one("SELECT 1::INT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    down_old = SqlModel(
        name="db.down",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    down_new = SqlModel(
        name="db.down",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    _, new_snaps_s3, diff_s3 = _build_snapshots_and_diff(
        make_snapshot, [up_old, down_old], [up_new, down_new]
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_s3, forward_only=False).build()
    assert new_snaps_s3['"db"."down"'].is_forward_only

    # 2. #2576 non-regression: upstream drops unused_col, downstream only selects keep_col and ds
    # (downstream's own schema did NOT change) -> succeeds with zero errors/warnings.
    up_drop_old = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=OnDestructiveChange.ALLOW,
        ),
        query=parse_one(
            "SELECT 1::INT AS keep_col, 'drop_me'::TEXT AS unused_col, '2022-01-01'::DATE AS ds",
            read="postgres",
        ),
    )
    up_drop_new = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=OnDestructiveChange.ALLOW,
        ),
        query=parse_one(
            "SELECT 1::INT AS keep_col, '2022-01-01'::DATE AS ds",
            read="postgres",
        ),
    )
    down_keep_old = SqlModel(
        name="db.down",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT keep_col, ds FROM db.up", read="postgres"),
    )
    down_keep_new = SqlModel(
        name="db.down",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT keep_col, ds FROM db.up", read="postgres"),
    )
    _, new_snaps_keep, diff_keep = _build_snapshots_and_diff(
        make_snapshot, [up_drop_old, down_keep_old], [up_drop_new, down_keep_new]
    )
    console = TerminalConsole()
    log_destructive_spy = mocker.spy(console, "log_destructive_change")
    assert PlanBuilder(diff_keep, forward_only=False, console=console).build()
    assert new_snaps_keep['"db"."down"'].is_forward_only
    assert log_destructive_spy.call_count == 0

    # 3. VIEW leaf does NOT raise PlanError (views recreate via CREATE OR REPLACE VIEW)
    view_leaf_old = SqlModel(
        name="db.v_leaf",
        dialect="postgres",
        kind=ViewKind(),
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    view_leaf_new = SqlModel(
        name="db.v_leaf",
        dialect="postgres",
        kind=ViewKind(),
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    _, _, diff_view_leaf = _build_snapshots_and_diff(
        make_snapshot, [up_old.copy(), view_leaf_old], [up_new.copy(), view_leaf_new]
    )
    assert PlanBuilder(diff_view_leaf, forward_only=True, console=console).build()
    assert log_destructive_spy.call_count == 0

    # 4. Materialized forward_only=True model downstream of EMBEDDED -> VIEW DOES raise PlanError
    emb_old = SqlModel(
        name="db.emb",
        dialect="postgres",
        kind=EmbeddedKind(),
        query=parse_one("SELECT '1'::TEXT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    emb_new = SqlModel(
        name="db.emb",
        dialect="postgres",
        kind=EmbeddedKind(),
        query=parse_one("SELECT 1::INT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    mid_view_old = SqlModel(
        name="db.mid_view",
        dialect="postgres",
        kind=ViewKind(),
        query=parse_one("SELECT val, ds FROM db.emb", read="postgres"),
    )
    mid_view_new = SqlModel(
        name="db.mid_view",
        dialect="postgres",
        kind=ViewKind(),
        query=parse_one("SELECT val, ds FROM db.emb", read="postgres"),
    )
    leaf_fo_old = SqlModel(
        name="db.leaf_fo",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        query=parse_one("SELECT val, ds FROM db.mid_view", read="postgres"),
    )
    leaf_fo_new = SqlModel(
        name="db.leaf_fo",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        query=parse_one("SELECT val, ds FROM db.mid_view", read="postgres"),
    )
    _, _, diff_chain = _build_snapshots_and_diff(
        make_snapshot,
        [emb_old, mid_view_old, leaf_fo_old],
        [emb_new, mid_view_new, leaf_fo_new],
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_chain).build()


def test_forward_only_plan_indirect_downstream_destructive_change(make_snapshot):
    """Gap S4: PlanBuilder(..., forward_only=True) where upstream FULL model alters column type and
    indirectly_modified downstream INCREMENTAL_BY_TIME_RANGE(forward_only=False) selects it."""
    up_old = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=FullKind(),
        query=parse_one("SELECT '1'::TEXT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    up_new = SqlModel(
        name="db.up",
        dialect="postgres",
        kind=FullKind(),
        query=parse_one("SELECT 1::INT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    down_old = SqlModel(
        name="db.down",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    down_new = SqlModel(
        name="db.down",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
    )
    _, _, diff = _build_snapshots_and_diff(make_snapshot, [up_old, down_old], [up_new, down_new])

    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff, forward_only=True).build()

    _, _, diff_allowed = _build_snapshots_and_diff(
        make_snapshot, [up_old.copy(), down_old.copy()], [up_new.copy(), down_new.copy()]
    )
    assert PlanBuilder(
        diff_allowed,
        forward_only=True,
        allow_destructive_models={'"db"."down"'},
    ).build()


def test_partially_unknown_columns_to_types_schema_checks(make_snapshot, mocker: MockerFixture):
    """Gap S5: Partially UNKNOWN-typed columns_to_types still detects dropped columns, added
    columns, and incompatible known-type alterations without false-positiving on shared UNKNOWNs."""

    def _make_direct_diff(
        old_sql: str,
        new_sql: str,
        on_destructive: OnDestructiveChange = OnDestructiveChange.ERROR,
        on_additive: OnAdditiveChange = OnAdditiveChange.ALLOW,
    ) -> ContextDiff:
        kind = IncrementalByTimeRangeKind(
            time_column="ds",
            forward_only=True,
            on_destructive_change=on_destructive,
            on_additive_change=on_additive,
        )
        m_old = SqlModel(
            name="db.m",
            dialect="duckdb",
            kind=kind,
            query=parse_one(old_sql, read="duckdb"),
        )
        m_new = SqlModel(
            name="db.m",
            dialect="duckdb",
            kind=kind,
            query=parse_one(new_sql, read="duckdb"),
        )
        _, _, diff = _build_snapshots_and_diff(make_snapshot, [m_old], [m_new])
        return diff

    base_sql = (
        "SELECT 'a'::TEXT AS typed_col, 'drop_me'::TEXT AS dropped_col, "
        "some_udf(x) AS unk_col, '2022-01-01'::DATE AS ds FROM raw_tbl"
    )

    # 1. Dropping a typed column alongside an UNKNOWN column raises PlanError
    diff_drop_typed = _make_direct_diff(
        base_sql,
        "SELECT 'a'::TEXT AS typed_col, some_udf(x) AS unk_col, '2022-01-01'::DATE AS ds FROM raw_tbl",
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_drop_typed).build()

    # 2. Dropping the UNKNOWN column itself raises PlanError
    diff_drop_unk = _make_direct_diff(
        base_sql,
        "SELECT 'a'::TEXT AS typed_col, 'drop_me'::TEXT AS dropped_col, '2022-01-01'::DATE AS ds FROM raw_tbl",
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_drop_unk).build()

    # 3. Adding a known-typed OR UNKNOWN-typed column with on_additive_change=ERROR raises PlanError
    for added_expr in ("1::INT AS added_col", "other_udf(x) AS added_col"):
        diff_add = _make_direct_diff(
            base_sql,
            f"SELECT 'a'::TEXT AS typed_col, 'drop_me'::TEXT AS dropped_col, "
            f"some_udf(x) AS unk_col, {added_expr}, '2022-01-01'::DATE AS ds FROM raw_tbl",
            on_additive=OnAdditiveChange.ERROR,
        )
        with pytest.raises(
            PlanError, match=r"Plan requires an additive change to a forward-only model\."
        ):
            PlanBuilder(diff_add).build()

    # 4. Incompatibly changing typed_col ('a'::TEXT -> 1::INT) while unk_col stays UNKNOWN raises PlanError
    diff_retype = _make_direct_diff(
        base_sql,
        "SELECT 1::INT AS typed_col, 'drop_me'::TEXT AS dropped_col, "
        "some_udf(x) AS unk_col, '2022-01-01'::DATE AS ds FROM raw_tbl",
    )
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_retype).build()

    # 5. Shared column keeping UNKNOWN (or INT -> UNKNOWN) with no drops/incompatible changes does NOT false-positive
    console = TerminalConsole()
    log_destructive_spy = mocker.spy(console, "log_destructive_change")
    diff_shared_unk = _make_direct_diff(
        "SELECT 1::INT AS col_a, some_udf(x) AS unk_col, '2022-01-01'::DATE AS ds FROM raw_tbl",
        "SELECT col_a, other_udf(x) AS unk_col, '2022-01-01'::DATE AS ds FROM raw_tbl",
    )
    assert PlanBuilder(diff_shared_unk, console=console).build()
    assert log_destructive_spy.call_count == 0


def test_forward_only_change_to_unannotated_star_does_not_false_positive(
    make_snapshot, mocker: MockerFixture
):
    """Bug C2: Changing a forward-only model from known columns_to_types to unexpanded SELECT *
    (columns_to_types is None) must NOT coerce None -> {} and falsely claim all columns dropped."""
    kind = IncrementalByTimeRangeKind(
        time_column="ds",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.ERROR,
    )
    m_old = SqlModel(
        name="db.m",
        dialect="duckdb",
        kind=kind,
        query=parse_one("SELECT 1::INT AS a, '2022-01-01'::DATE AS ds", read="duckdb"),
    )
    m_new = SqlModel(
        name="db.m",
        dialect="duckdb",
        kind=kind,
        query=parse_one("SELECT * FROM unregistered_raw_tbl", read="duckdb"),
    )
    assert m_old.columns_to_types is not None
    assert m_new.columns_to_types is None

    _, _, diff = _build_snapshots_and_diff(make_snapshot, [m_old], [m_new])
    console = TerminalConsole()
    log_destructive_spy = mocker.spy(console, "log_destructive_change")
    assert PlanBuilder(diff, console=console).build()
    assert log_destructive_spy.call_count == 0


def test_interactive_categorization_indirect_non_breaking_schema_checks(make_snapshot):
    """Gaps S7 & S8 + --no-auto-categorization:
    1. First build() while upstream is uncategorized must NOT prematurely raise PlanError.
    2. set_choice(up, BREAKING) -> downstream INDIRECT_BREAKING (full rebuild) -> succeeds.
    3. set_choice(up, NON_BREAKING) -> downstream INDIRECT_NON_BREAKING (is_no_rebuild) -> raises PlanError.
    """

    def _make_s7_diff() -> t.Tuple[Snapshot, Snapshot, ContextDiff]:
        up_old = SqlModel(
            name="db.up",
            dialect="postgres",
            kind=FullKind(),
            query=parse_one("SELECT '1'::TEXT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
        )
        up_new = SqlModel(
            name="db.up",
            dialect="postgres",
            kind=FullKind(),
            query=parse_one("SELECT 1::INT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
        )
        down_old = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=False,
                on_destructive_change=OnDestructiveChange.ERROR,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        down_new = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=False,
                on_destructive_change=OnDestructiveChange.ERROR,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        _, new_snaps, diff = _build_snapshots_and_diff(
            make_snapshot, [up_old, down_old], [up_new, down_new]
        )
        return new_snaps['"db"."up"'], new_snaps['"db"."down"'], diff

    # S7 (destructive): uncategorized -> BREAKING succeeds -> NON_BREAKING raises PlanError
    up_snap_b, down_snap_b, diff_s7_breaking = _make_s7_diff()
    builder_s7_breaking = PlanBuilder(diff_s7_breaking, auto_categorization_enabled=False)
    assert builder_s7_breaking.build()
    assert not up_snap_b.categorized
    assert builder_s7_breaking.set_choice(up_snap_b, SnapshotChangeCategory.BREAKING).build()
    assert down_snap_b.change_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert not down_snap_b.is_no_rebuild

    up_snap_nb, down_snap_nb, diff_s7_nb = _make_s7_diff()
    builder_s7_nb = PlanBuilder(diff_s7_nb, auto_categorization_enabled=False)
    assert builder_s7_nb.build()
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        builder_s7_nb.set_choice(up_snap_nb, SnapshotChangeCategory.NON_BREAKING).build()
    assert down_snap_nb.is_indirect_non_breaking
    assert down_snap_nb.is_no_rebuild

    # S8 (additive): uncategorized -> BREAKING succeeds -> NON_BREAKING raises PlanError
    def _make_s8_diff() -> t.Tuple[Snapshot, Snapshot, ContextDiff]:
        up_old = SqlModel(
            name="db.up",
            dialect="postgres",
            kind=FullKind(),
            query=parse_one(
                "SELECT 'x'::VARCHAR(10) AS val, '2022-01-01'::DATE AS ds", read="postgres"
            ),
        )
        up_new = SqlModel(
            name="db.up",
            dialect="postgres",
            kind=FullKind(),
            query=parse_one(
                "SELECT 'x'::VARCHAR(50) AS val, '2022-01-01'::DATE AS ds", read="postgres"
            ),
        )
        down_old = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=False,
                on_additive_change=OnAdditiveChange.ERROR,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        down_new = SqlModel(
            name="db.down",
            dialect="postgres",
            kind=IncrementalByTimeRangeKind(
                time_column="ds",
                forward_only=False,
                on_additive_change=OnAdditiveChange.ERROR,
            ),
            query=parse_one("SELECT val, ds FROM db.up", read="postgres"),
        )
        _, new_snaps, diff = _build_snapshots_and_diff(
            make_snapshot, [up_old, down_old], [up_new, down_new]
        )
        return new_snaps['"db"."up"'], new_snaps['"db"."down"'], diff

    up_s8_b, down_s8_b, diff_s8_b = _make_s8_diff()
    builder_s8_b = PlanBuilder(diff_s8_b, auto_categorization_enabled=False)
    assert builder_s8_b.build()
    assert builder_s8_b.set_choice(up_s8_b, SnapshotChangeCategory.BREAKING).build()
    assert down_s8_b.change_category == SnapshotChangeCategory.INDIRECT_BREAKING

    up_s8_nb, down_s8_nb, diff_s8_nb = _make_s8_diff()
    builder_s8_nb = PlanBuilder(diff_s8_nb, auto_categorization_enabled=False)
    assert builder_s8_nb.build()
    with pytest.raises(
        PlanError, match=r"Plan requires an additive change to a forward-only model\."
    ):
        builder_s8_nb.set_choice(up_s8_nb, SnapshotChangeCategory.NON_BREAKING).build()
    assert down_s8_nb.is_indirect_non_breaking
    assert down_s8_nb.is_no_rebuild


def test_precategorized_forward_only_snapshot_checked_on_prod_plan(make_snapshot):
    """Gap S9: A snapshot pre-categorized in dev via categorize_as(BREAKING, forward_only=True)
    and already persisted in state (new_snapshots={}) must still be checked on a prod plan."""
    m_old = SqlModel(
        name="db.m",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT '1'::TEXT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    m_new = SqlModel(
        name="db.m",
        dialect="postgres",
        kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=False),
        query=parse_one("SELECT 1::INT AS val, '2022-01-01'::DATE AS ds", read="postgres"),
    )
    _, new_snaps, prod_diff = _build_snapshots_and_diff(
        make_snapshot,
        [m_old],
        [m_new],
        environment="prod",
        is_new_environment=False,
        new_snapshot_names=set(),
    )
    m_snap = new_snaps['"db"."m"']
    m_snap.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    assert m_snap.is_forward_only
    assert not prod_diff.new_snapshots

    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(prod_diff, forward_only=False).build()

    assert PlanBuilder(
        prod_diff,
        forward_only=False,
        allow_destructive_models={'"db"."m"'},
    ).build()


def test_breaking_kind_change_in_forward_only_plan_skips_destructive_check(make_snapshot):
    """Bug B1: Breaking kind change (FullKind -> IncrementalByTimeRangeKind) while dropping a
    column in PlanBuilder(..., forward_only=True) forces forward_only=False (full rebuild) and
    must succeed without raising PlanError."""
    m_old = SqlModel(
        name="db.m",
        dialect="duckdb",
        kind=FullKind(),
        query=parse_one(
            "SELECT 1::INT AS id, 'drop_me'::TEXT AS b, '2022-01-01'::DATE AS ds",
            read="duckdb",
        ),
    )
    m_new = SqlModel(
        name="db.m",
        dialect="duckdb",
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        query=parse_one("SELECT 1::INT AS id, '2022-01-01'::DATE AS ds", read="duckdb"),
    )
    _, new_snaps, diff = _build_snapshots_and_diff(make_snapshot, [m_old], [m_new])

    plan = PlanBuilder(diff, forward_only=True).build()
    assert plan is not None
    assert not new_snaps['"db"."m"'].is_forward_only
    assert new_snaps['"db"."m"'].change_category == SnapshotChangeCategory.BREAKING


def test_schema_warnings_logged_at_most_once_per_plan_builder(make_snapshot, mocker: MockerFixture):
    """Idempotent schema warning logging: repeated build() calls after set_start / set_end on the
    same PlanBuilder instance must log each warning at most once per (snapshot_id, change_kind)."""
    kind = IncrementalByTimeRangeKind(
        time_column="ds",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.WARN,
        on_additive_change=OnAdditiveChange.WARN,
    )
    m_old = SqlModel(
        name="db.m",
        dialect="duckdb",
        kind=kind,
        query=parse_one(
            "SELECT 1::INT AS id, 'drop_me'::TEXT AS old_col, '2022-01-01'::DATE AS ds",
            read="duckdb",
        ),
    )
    m_new = SqlModel(
        name="db.m",
        dialect="duckdb",
        kind=kind,
        query=parse_one(
            "SELECT 1::INT AS id, 'added'::TEXT AS new_col, '2022-01-01'::DATE AS ds",
            read="duckdb",
        ),
    )
    _, _, diff = _build_snapshots_and_diff(make_snapshot, [m_old], [m_new])

    console = TerminalConsole()
    log_destructive_spy = mocker.spy(console, "log_destructive_change")
    log_additive_spy = mocker.spy(console, "log_additive_change")

    plan_builder = PlanBuilder(diff, is_dev=True, console=console)
    plan_builder.build()
    plan_builder.set_start("2022-01-02")
    plan_builder.build()
    plan_builder.set_end("2022-01-03")
    plan_builder.build()

    assert log_destructive_spy.call_count == 1
    assert log_additive_spy.call_count == 1


def test_plan_builder_schema_differ_overrides(make_snapshot):
    """Gap S10 (unit): PlanBuilder(..., schema_differ_overrides=...) applies both default (None)
    and per-gateway overrides matching snapshot.model_gateway."""

    def _make_widening_diff(gateway: t.Optional[str] = None) -> ContextDiff:
        m_old = SqlModel(
            name="db.m",
            dialect="postgres",
            gateway=gateway,
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
            query=parse_one(
                "SELECT 'x'::VARCHAR(10) AS val, '2022-01-01'::DATE AS ds", read="postgres"
            ),
        )
        m_new = SqlModel(
            name="db.m",
            dialect="postgres",
            gateway=gateway,
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
            query=parse_one(
                "SELECT 'x'::VARCHAR(50) AS val, '2022-01-01'::DATE AS ds", read="postgres"
            ),
        )
        _, _, diff = _build_snapshots_and_diff(make_snapshot, [m_old], [m_new])
        return diff

    # Without overrides, VARCHAR(10) -> VARCHAR(50) in postgres is compatible (additive, allowed)
    assert PlanBuilder(_make_widening_diff()).build()

    # With default (None) override treat_alter_data_type_as_destructive=True -> raises PlanError
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(
            _make_widening_diff(),
            schema_differ_overrides={None: {"treat_alter_data_type_as_destructive": True}},
        ).build()

    # Per-gateway override: gw_a unaffected, gw_b raises PlanError
    gateway_overrides: t.Dict[t.Optional[str], t.Dict[str, t.Any]] = {
        "gw_b": {"treat_alter_data_type_as_destructive": True}
    }
    assert PlanBuilder(
        _make_widening_diff(gateway="gw_a"),
        schema_differ_overrides=gateway_overrides,
    ).build()

    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(
            _make_widening_diff(gateway="gw_b"),
            schema_differ_overrides=gateway_overrides,
        ).build()

    # Default gateway (None) overrides must NOT bleed onto an explicitly named secondary gateway
    assert PlanBuilder(
        _make_widening_diff(gateway="gw_other"),
        schema_differ_overrides={
            None: {"treat_alter_data_type_as_destructive": True},
            "gw_a": {"treat_alter_data_type_as_destructive": True},
            "gw_other": {},
        },
    ).build()


def test_context_plan_builder_plumbs_schema_differ_overrides_without_connecting(
    tmp_path: Path, mocker: MockerFixture
):
    """Gap S10 (Context plumbing): Context.plan_builder reads ConnectionConfig.schema_differ_overrides
    for both the default gateway and named multi-gateway configs without instantiating uninitialized
    secondary EngineAdapters."""
    # 1. Default gateway DuckDBConnectionConfig with schema_differ_overrides catches DECIMAL widening
    proj_default = tmp_path / "proj_default"
    (proj_default / "models").mkdir(parents=True)
    (proj_default / "models" / "m.sql").write_text(
        """
        MODEL (
            name db.m,
            kind INCREMENTAL_BY_TIME_RANGE (time_column ds, forward_only true),
            start '2023-01-01'
        );
        SELECT 1::DECIMAL(10, 2) AS val, '2023-01-01'::DATE AS ds;
        """
    )
    cfg_default = Config(
        gateways={
            "default": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    schema_differ_overrides={"treat_alter_data_type_as_destructive": True}
                )
            )
        },
        default_gateway="default",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    ctx_default = Context(paths=[proj_default], config=cfg_default)
    ctx_default.plan("prod", execution_time="2023-01-02", auto_apply=True, no_prompts=True)

    (proj_default / "models" / "m.sql").write_text(
        """
        MODEL (
            name db.m,
            kind INCREMENTAL_BY_TIME_RANGE (time_column ds, forward_only true),
            start '2023-01-01'
        );
        SELECT 1::DECIMAL(12, 2) AS val, '2023-01-01'::DATE AS ds;
        """
    )
    ctx_default.load()
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        ctx_default.plan_builder("dev", execution_time="2023-01-02").build()

    # 2. Multi-gateway config: secondary Postgres gateway with unreachable host is NOT connected
    # when Context.plan_builder collects per-gateway schema_differ_overrides
    proj_multi = tmp_path / "proj_multi"
    (proj_multi / "models").mkdir(parents=True)
    (proj_multi / "models" / "a.sql").write_text(
        """
        MODEL (name db.a, kind FULL);
        SELECT 1::INT AS id;
        """
    )
    pg_conn = PostgresConnectionConfig(
        host="unreachable.invalid",
        port=5432,
        user="u",
        password="p",
        database="d",
        schema_differ_overrides={"treat_alter_data_type_as_destructive": True},
    )
    cfg_multi = Config(
        gateways={
            "gw_a": GatewayConfig(connection=DuckDBConnectionConfig()),
            "gw_b": GatewayConfig(connection=pg_conn),
        },
        default_gateway="gw_a",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    pg_adapter_spy = mocker.spy(PostgresConnectionConfig, "create_engine_adapter")
    plan_builder_init_spy = mocker.spy(PlanBuilder, "__init__")
    ctx_multi = Context(paths=[proj_multi], config=cfg_multi)
    pg_adapter_spy.reset_mock()

    pb = ctx_multi.plan_builder("dev", execution_time="2023-01-02")
    assert pb.build()
    assert pg_adapter_spy.call_count == 0
    passed_overrides = plan_builder_init_spy.call_args.kwargs.get("schema_differ_overrides") or {}
    assert passed_overrides.get("gw_b") == {"treat_alter_data_type_as_destructive": True}


def test_plan_builder_partially_unknown_nested_struct_and_positional_add(
    make_snapshot, mocker: MockerFixture
):
    # 1. Partially-unknown STRUCT field drop in forward-only model must raise PlanError
    kind_err = IncrementalByTimeRangeKind(time_column="ds", forward_only=True)
    m_old_struct = SqlModel(
        name="db.m",
        dialect="databricks",
        kind=kind_err,
        query=parse_one(
            "SELECT STRUCT(1 AS a, 'x' AS b, NULL AS u) AS info, CAST('2023-01-01' AS DATE) AS ds",
            read="databricks",
        ),
    )
    m_new_struct = SqlModel(
        name="db.m",
        dialect="databricks",
        kind=kind_err,
        query=parse_one(
            "SELECT STRUCT(1 AS a, NULL AS u) AS info, CAST('2023-01-01' AS DATE) AS ds",
            read="databricks",
        ),
    )
    _, _, diff_struct = _build_snapshots_and_diff(make_snapshot, [m_old_struct], [m_new_struct])
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_struct).build()

    # 2. Positional column addition in the middle preserves AFTER clause on Databricks
    kind_warn = IncrementalByTimeRangeKind(
        time_column="ds",
        forward_only=True,
        on_additive_change=OnAdditiveChange.WARN,
    )
    m_old_pos = SqlModel(
        name="db.m",
        dialect="databricks",
        kind=kind_warn,
        query=parse_one("SELECT 1 AS a, CAST('2023-01-01' AS DATE) AS ds", read="databricks"),
    )
    m_new_pos = SqlModel(
        name="db.m",
        dialect="databricks",
        kind=kind_warn,
        query=parse_one(
            "SELECT 1 AS a, 2 AS b_mid, CAST('2023-01-01' AS DATE) AS ds", read="databricks"
        ),
    )
    _, _, diff_pos = _build_snapshots_and_diff(make_snapshot, [m_old_pos], [m_new_pos])
    console = TerminalConsole()
    log_additive_spy = mocker.spy(console, "log_additive_change")
    assert PlanBuilder(diff_pos, console=console).build()
    assert log_additive_spy.call_count == 1
    alter_ops = log_additive_spy.call_args.args[1]
    assert [op.expression.sql("databricks") for op in alter_ops] == [
        "ALTER TABLE `db`.`m` ADD COLUMN b_mid INT AFTER a"
    ]


def test_context_plan_builder_gateway_default_connection_fallback_and_precedence(
    tmp_path: Path, mocker: MockerFixture
):
    from sqlmesh.core.config import (
        Config,
        DuckDBConnectionConfig,
        GatewayConfig,
        ModelDefaultsConfig,
        PostgresConnectionConfig,
    )
    from sqlmesh.core.context import Context

    proj = tmp_path / "proj_gw_fallback"
    (proj / "models").mkdir(parents=True)
    (proj / "models" / "a.sql").write_text(
        """
        MODEL (name db.a, kind FULL);
        SELECT 1::INT AS id;
        """
    )

    pg_default_conn = PostgresConnectionConfig(
        host="unreachable.invalid",
        port=5432,
        user="u",
        password="p",
        database="d",
        schema_differ_overrides={"treat_alter_data_type_as_destructive": True},
    )
    cfg = Config(
        default_connection=pg_default_conn,
        gateways={
            "gw_duck": GatewayConfig(connection=DuckDBConnectionConfig()),
            # connection=None should inherit default_connection's schema_differ_overrides
            "gw_state_only": GatewayConfig(
                connection=None, state_connection=DuckDBConnectionConfig()
            ),
        },
        default_gateway="gw_duck",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )

    pg_adapter_spy = mocker.spy(PostgresConnectionConfig, "create_engine_adapter")
    plan_builder_init_spy = mocker.spy(PlanBuilder, "__init__")
    ctx = Context(paths=[proj], config=cfg)
    pg_adapter_spy.reset_mock()

    pb = ctx.plan_builder("dev", execution_time="2023-01-02")
    assert pb.build()
    assert pg_adapter_spy.call_count == 0
    passed_overrides = plan_builder_init_spy.call_args.kwargs.get("schema_differ_overrides") or {}
    assert passed_overrides.get("gw_duck") == {}
    assert passed_overrides.get(None) == {}
    assert passed_overrides.get("gw_state_only") == {"treat_alter_data_type_as_destructive": True}


def test_treat_alter_data_type_as_destructive_with_ignore_policies(
    make_snapshot, mocker: MockerFixture
):
    overrides: t.Dict[t.Optional[str], t.Dict[str, t.Any]] = {
        None: {"treat_alter_data_type_as_destructive": True}
    }

    # 1. on_destructive_change=IGNORE + on_additive_change=ERROR must NOT log or raise
    kind_ignore_dest = IncrementalByTimeRangeKind(
        time_column="ds",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.ERROR,
    )
    m_old_1 = SqlModel(
        name="db.m",
        dialect="postgres",
        kind=kind_ignore_dest,
        query=parse_one(
            "SELECT 'x'::VARCHAR(10) AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )
    m_new_1 = SqlModel(
        name="db.m",
        dialect="postgres",
        kind=kind_ignore_dest,
        query=parse_one(
            "SELECT 'x'::VARCHAR(50) AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )
    _, _, diff_ignore_dest = _build_snapshots_and_diff(make_snapshot, [m_old_1], [m_new_1])
    console = TerminalConsole()
    log_dest_spy = mocker.spy(console, "log_destructive_change")
    log_add_spy = mocker.spy(console, "log_additive_change")
    assert PlanBuilder(diff_ignore_dest, console=console, schema_differ_overrides=overrides).build()
    assert log_dest_spy.call_count == 0
    assert log_add_spy.call_count == 0

    # 2. on_destructive_change=ERROR + on_additive_change=IGNORE MUST still raise PlanError
    kind_ignore_add = IncrementalByTimeRangeKind(
        time_column="ds",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.ERROR,
        on_additive_change=OnAdditiveChange.IGNORE,
    )
    m_old_2 = SqlModel(
        name="db.m",
        dialect="postgres",
        kind=kind_ignore_add,
        query=parse_one(
            "SELECT 'x'::VARCHAR(10) AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )
    m_new_2 = SqlModel(
        name="db.m",
        dialect="postgres",
        kind=kind_ignore_add,
        query=parse_one(
            "SELECT 'x'::VARCHAR(50) AS val, '2022-01-01'::DATE AS ds", read="postgres"
        ),
    )
    _, _, diff_ignore_add = _build_snapshots_and_diff(make_snapshot, [m_old_2], [m_new_2])
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        PlanBuilder(diff_ignore_add, schema_differ_overrides=overrides).build()
