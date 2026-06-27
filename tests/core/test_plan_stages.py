import pytest
import typing as t
from sqlglot import parse_one
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.config import EnvironmentSuffixTarget
from sqlmesh.core.config.common import VirtualEnvironmentMode
from sqlmesh.core.model import SqlModel, ModelKindName
from sqlmesh.core.plan.common import SnapshotIntervalClearRequest
from sqlmesh.core.plan.definition import EvaluatablePlan
from sqlmesh.core.plan.stages import (
    build_plan_stages,
    AfterAllStage,
    AuditOnlyRunStage,
    PhysicalLayerUpdateStage,
    PhysicalLayerSchemaCreationStage,
    CreateSnapshotRecordsStage,
    BeforeAllStage,
    BackfillStage,
    EnvironmentRecordUpdateStage,
    VirtualLayerUpdateStage,
    RestatementStage,
    MigrateSchemasStage,
    FinalizeEnvironmentStage,
    UnpauseStage,
)
from sqlmesh.core.plan.explainer import ExplainableRestatementStage
from sqlmesh.core.snapshot.definition import (
    SnapshotChangeCategory,
    DeployabilityIndex,
    Snapshot,
    SnapshotId,
    SnapshotIdLike,
)
from sqlmesh.core.state_sync import StateReader
from sqlmesh.core.environment import Environment, EnvironmentStatements
from sqlmesh.utils.date import to_timestamp


@pytest.fixture
def snapshot_a(make_snapshot) -> Snapshot:
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    return snapshot


@pytest.fixture
def snapshot_b(make_snapshot, snapshot_a: Snapshot) -> Snapshot:
    snapshot = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select 2, ds from a"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        ),
        nodes={'"a"': snapshot_a.model},
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    return snapshot


@pytest.fixture
def snapshot_c(make_snapshot, snapshot_a: Snapshot) -> Snapshot:
    snapshot = make_snapshot(
        SqlModel(
            name="c",
            query=parse_one("select * from a"),
            kind=dict(name=ModelKindName.VIEW),
        ),
        nodes={'"a"': snapshot_a.model},
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    return snapshot


def test_build_plan_stages_basic(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Create environment
    environment = Environment(
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_a, snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 7

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[0]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[1]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    assert physical_stage.deployability_index == DeployabilityIndex.all_deployable()

    # Verify BackfillStage
    backfill_stage = stages[2]
    assert isinstance(backfill_stage, BackfillStage)
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    expected_interval = (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))
    assert len(backfill_stage.snapshot_to_intervals) == 2
    assert backfill_stage.snapshot_to_intervals[snapshot_a] == [expected_interval]
    assert backfill_stage.snapshot_to_intervals[snapshot_b] == [expected_interval]

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[3], EnvironmentRecordUpdateStage)
    assert stages[3].no_gaps_snapshot_names == {snapshot_a.name, snapshot_b.name}

    # Verify UnpauseStage
    assert isinstance(stages[4], UnpauseStage)
    assert {s.name for s in stages[4].promoted_snapshots} == {snapshot_a.name, snapshot_b.name}

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[5]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 2
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {snapshot_a.name, snapshot_b.name}

    state_reader.refresh_snapshot_intervals.assert_called_once()

    assert isinstance(stages[6], FinalizeEnvironmentStage)


def test_build_plan_stages_with_before_all_and_after_all(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Create environment
    environment = Environment(
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
    )

    environment_statements = [
        EnvironmentStatements(
            before_all=["BEFORE ALL A", "BEFORE ALL B"],
            after_all=["AFTER ALL A", "AFTER ALL B"],
            python_env={},
            jinja_macros=None,
        )
    ]

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_a, snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=environment_statements,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 9

    # Verify BeforeAllStage
    before_all_stage = stages[0]
    assert isinstance(before_all_stage, BeforeAllStage)
    assert before_all_stage.statements == environment_statements

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[1]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[2]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    assert physical_stage.deployability_index == DeployabilityIndex.all_deployable()

    # Verify BackfillStage
    backfill_stage = stages[3]
    assert isinstance(backfill_stage, BackfillStage)
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    expected_interval = (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))
    assert len(backfill_stage.snapshot_to_intervals) == 2
    assert backfill_stage.snapshot_to_intervals[snapshot_a] == [expected_interval]
    assert backfill_stage.snapshot_to_intervals[snapshot_b] == [expected_interval]

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[4], EnvironmentRecordUpdateStage)
    assert stages[4].no_gaps_snapshot_names == {snapshot_a.name, snapshot_b.name}

    # Verify UnpauseStage
    assert isinstance(stages[5], UnpauseStage)
    assert {s.name for s in stages[5].promoted_snapshots} == {snapshot_a.name, snapshot_b.name}

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[6]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 2
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {'"a"', '"b"'}

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[7], FinalizeEnvironmentStage)

    # Verify AfterAllStage
    after_all_stage = stages[8]
    assert isinstance(after_all_stage, AfterAllStage)
    assert after_all_stage.statements == environment_statements


def test_build_plan_stages_select_models(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Create environment
    environment = Environment(
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_a, snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill={snapshot_a.name},
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 7

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[0]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[1]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 1
    assert {s.snapshot_id for s in physical_stage.snapshots} == {snapshot_a.snapshot_id}
    assert physical_stage.deployability_index == DeployabilityIndex.all_deployable()

    # Verify BackfillStage
    backfill_stage = stages[2]
    assert isinstance(backfill_stage, BackfillStage)
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    expected_interval = (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))
    assert len(backfill_stage.snapshot_to_intervals) == 1
    assert backfill_stage.snapshot_to_intervals[snapshot_a] == [expected_interval]

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[3], EnvironmentRecordUpdateStage)
    assert stages[3].no_gaps_snapshot_names == {snapshot_a.name}

    # Verify UnpauseStage
    assert isinstance(stages[4], UnpauseStage)
    assert {s.name for s in stages[4].promoted_snapshots} == {snapshot_a.name}

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[5]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 1
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {'"a"'}

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[6], FinalizeEnvironmentStage)


@pytest.mark.parametrize("skip_backfill,empty_backfill", [(True, False), (False, True)])
def test_build_plan_stages_basic_no_backfill(
    snapshot_a: Snapshot,
    snapshot_b: Snapshot,
    mocker: MockerFixture,
    skip_backfill: bool,
    empty_backfill: bool,
) -> None:
    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Create environment
    environment = Environment(
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_a, snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=skip_backfill,
        empty_backfill=empty_backfill,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 8

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[0]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[1]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Verify PhysicalLayerUpdateStage
    physical_stage = stages[2]
    assert isinstance(physical_stage, PhysicalLayerUpdateStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Verify BackfillStage
    backfill_stage = stages[3]
    assert isinstance(backfill_stage, BackfillStage)
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    assert backfill_stage.snapshot_to_intervals == {}

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[4], EnvironmentRecordUpdateStage)
    assert stages[4].no_gaps_snapshot_names == {snapshot_a.name, snapshot_b.name}

    # Verify UnpauseStage
    assert isinstance(stages[5], UnpauseStage)
    assert {s.name for s in stages[5].promoted_snapshots} == {snapshot_a.name, snapshot_b.name}

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[6]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 2
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {'"a"', '"b"'}

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[7], FinalizeEnvironmentStage)


def test_build_plan_stages_restatement_prod_only(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    """
    Scenario:
        - Prod restatement triggered in a project with no dev environments

    Expected Outcome:
        - Plan still contains a RestatementStage in case a dev environment was
          created during restatement
    """

    # Mock state reader to return existing snapshots and environment
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    state_reader.get_snapshots_by_names.return_value = {
        snapshot_a.id_and_version,
        snapshot_b.id_and_version,
    }

    existing_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )

    state_reader.get_environment.return_value = existing_environment
    state_reader.get_environments_summary.return_value = [existing_environment.summary]

    environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
    )

    # Create evaluatable plan with restatements
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[],  # No new snapshots
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={
            '"a"': (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
            '"b"': (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        },
        restate_all_snapshots=True,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],  # No changes
        indirectly_modified_snapshots={},  # No changes
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 5

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[0]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Verify BackfillStage
    backfill_stage = stages[1]
    assert isinstance(backfill_stage, BackfillStage)
    assert len(backfill_stage.snapshot_to_intervals) == 2
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    expected_backfill_interval = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
    for intervals in backfill_stage.snapshot_to_intervals.values():
        assert intervals == expected_backfill_interval

    # Verify RestatementStage exists but is empty
    restatement_stage = stages[2]
    assert isinstance(restatement_stage, RestatementStage)
    restatement_stage = ExplainableRestatementStage.from_restatement_stage(
        restatement_stage, state_reader, plan
    )
    assert not restatement_stage.snapshot_intervals_to_clear

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[3], EnvironmentRecordUpdateStage)

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[4], FinalizeEnvironmentStage)


def test_build_plan_stages_restatement_prod_identifies_dev_intervals(
    snapshot_a: Snapshot,
    snapshot_b: Snapshot,
    make_snapshot: t.Callable[..., Snapshot],
    mocker: MockerFixture,
) -> None:
    """
    Scenario:
        - Prod restatement triggered in a project with a dev environment
        - The dev environment contains a different physical version of the affected model

    Expected Outcome:
        - Plan contains a RestatementStage that highlights the affected dev version
    """
    # Dev version of snapshot_a, same name but different version
    snapshot_a_dev = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, changed, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )
    snapshot_a_dev.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot_a_dev.snapshot_id != snapshot_a.snapshot_id
    assert snapshot_a_dev.table_info != snapshot_a.table_info

    # Mock state reader to return existing snapshots and environment
    state_reader = mocker.Mock(spec=StateReader)
    snapshots_in_state = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
        snapshot_a_dev.snapshot_id: snapshot_a_dev,
    }

    def _get_snapshots(snapshot_ids: t.Iterable[SnapshotIdLike]):
        return {
            k: v
            for k, v in snapshots_in_state.items()
            if k in {s.snapshot_id for s in snapshot_ids}
        }

    state_reader.get_snapshots.side_effect = _get_snapshots
    state_reader.get_snapshots_by_names.return_value = set()

    existing_prod_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )

    # dev has new version of snapshot_a but same version of snapshot_b
    existing_dev_environment = Environment(
        name="dev",
        snapshots=[snapshot_a_dev.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a_dev.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )

    state_reader.get_environment.side_effect = lambda name: (
        existing_dev_environment if name == "dev" else existing_prod_environment
    )
    state_reader.get_environments_summary.return_value = [
        existing_prod_environment.summary,
        existing_dev_environment.summary,
    ]

    environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
    )

    # Create evaluatable plan with restatements
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[],  # No new snapshots
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={
            '"a"': (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
            '"b"': (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        },
        restate_all_snapshots=True,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],  # No changes
        indirectly_modified_snapshots={},  # No changes
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 5

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[0]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Verify BackfillStage
    backfill_stage = stages[1]
    assert isinstance(backfill_stage, BackfillStage)
    assert len(backfill_stage.snapshot_to_intervals) == 2
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    expected_backfill_interval = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
    for intervals in backfill_stage.snapshot_to_intervals.values():
        assert intervals == expected_backfill_interval

    # Verify RestatementStage
    restatement_stage = stages[2]
    assert isinstance(restatement_stage, RestatementStage)
    restatement_stage = ExplainableRestatementStage.from_restatement_stage(
        restatement_stage, state_reader, plan
    )

    # note: we only clear the intervals from state for "a" in dev, we leave prod alone
    assert restatement_stage.snapshot_intervals_to_clear
    assert len(restatement_stage.snapshot_intervals_to_clear) == 1
    snapshot_name, clear_requests = list(restatement_stage.snapshot_intervals_to_clear.items())[0]
    assert snapshot_name == '"a"'
    assert len(clear_requests) == 1
    clear_request = clear_requests[0]
    assert isinstance(clear_request, SnapshotIntervalClearRequest)
    assert clear_request.snapshot_id == snapshot_a_dev.snapshot_id
    assert clear_request.snapshot == snapshot_a_dev.id_and_version
    assert clear_request.interval == (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[3], EnvironmentRecordUpdateStage)

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[4], FinalizeEnvironmentStage)


def test_build_plan_stages_restatement_dev_does_not_clear_intervals(
    snapshot_a: Snapshot,
    snapshot_b: Snapshot,
    make_snapshot: t.Callable[..., Snapshot],
    mocker: MockerFixture,
) -> None:
    """
    Scenario:
        - Restatement triggered against the dev environment

    Expected Outcome:
        - BackfillStage only touches models in that dev environment
        - Plan does not contain a RestatementStage because making changes in dev doesnt mean we need
          to clear intervals from other environments
    """
    # Dev version of snapshot_a, same name but different version
    snapshot_a_dev = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, changed, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )
    snapshot_a_dev.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot_a_dev.snapshot_id != snapshot_a.snapshot_id
    assert snapshot_a_dev.table_info != snapshot_a.table_info

    # Mock state reader to return existing snapshots and environment
    state_reader = mocker.Mock(spec=StateReader)
    snapshots_in_state = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
        snapshot_a_dev.snapshot_id: snapshot_a_dev,
    }
    state_reader.get_snapshots.side_effect = lambda snapshot_info_like: {
        k: v
        for k, v in snapshots_in_state.items()
        if k in [sil.snapshot_id for sil in snapshot_info_like]
    }

    # prod has snapshot_a, snapshot_b
    existing_prod_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_prod_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )

    # dev has new version of snapshot_a
    existing_dev_environment = Environment(
        name="dev",
        snapshots=[snapshot_a_dev.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_dev_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a_dev.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )

    state_reader.get_environment.side_effect = lambda name: (
        existing_dev_environment if name == "dev" else existing_prod_environment
    )
    state_reader.get_environments_summary.return_value = [
        existing_prod_environment.summary,
        existing_dev_environment.summary,
    ]

    environment = Environment(
        name="dev",
        snapshots=[snapshot_a_dev.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_dev_plan",
        promoted_snapshot_ids=[snapshot_a_dev.snapshot_id],
    )

    # Create evaluatable plan with restatements
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[],  # No new snapshots
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={
            '"a"': (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        },
        restate_all_snapshots=False,
        is_dev=True,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],  # No changes
        indirectly_modified_snapshots={},  # No changes
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 5

    # Verify no RestatementStage
    assert not any(s for s in stages if isinstance(s, RestatementStage))

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[0]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 1
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        snapshot_a_dev.snapshot_id,
    }

    # Verify BackfillStage
    backfill_stage = stages[1]
    assert isinstance(backfill_stage, BackfillStage)
    assert len(backfill_stage.snapshot_to_intervals) == 1
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()
    backfill_snapshot, backfill_intervals = list(backfill_stage.snapshot_to_intervals.items())[0]
    assert backfill_snapshot.snapshot_id == snapshot_a_dev.snapshot_id
    assert backfill_intervals == [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[2], EnvironmentRecordUpdateStage)

    # Verify VirtualLayerUpdateStage (all non-prod plans get this regardless)
    assert isinstance(stages[3], VirtualLayerUpdateStage)

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[4], FinalizeEnvironmentStage)


def test_build_plan_stages_forward_only(
    snapshot_a: Snapshot, snapshot_b: Snapshot, make_snapshot, mocker: MockerFixture
) -> None:
    # Categorize snapshot_a as forward-only
    new_snapshot_a = make_snapshot(
        snapshot_a.model.copy(update={"stamp": "new_version"}),
    )
    new_snapshot_a.previous_versions = snapshot_a.all_versions
    new_snapshot_a.categorize_as(SnapshotChangeCategory.NON_BREAKING, forward_only=True)

    new_snapshot_b = make_snapshot(
        snapshot_b.model.copy(),
        nodes={'"a"': new_snapshot_a.model},
    )
    new_snapshot_b.previous_versions = snapshot_b.all_versions
    new_snapshot_b.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING, forward_only=True)

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    existing_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    # Create environment
    environment = Environment(
        name="prod",
        snapshots=[new_snapshot_a.table_info, new_snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[new_snapshot_a.snapshot_id, new_snapshot_b.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[new_snapshot_a, new_snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[new_snapshot_a.snapshot_id],
        indirectly_modified_snapshots={
            new_snapshot_a.name: [new_snapshot_b.snapshot_id],
        },
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 8

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[0]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[1]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }
    assert physical_stage.deployability_index == DeployabilityIndex.all_deployable()

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[2], EnvironmentRecordUpdateStage)
    assert stages[2].no_gaps_snapshot_names == set()

    # Verify MigrateSchemasStage
    migrate_stage = stages[3]
    assert isinstance(migrate_stage, MigrateSchemasStage)
    assert len(migrate_stage.snapshots) == 2
    assert {s.snapshot_id for s in migrate_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }

    # Verify UnpauseStage
    assert isinstance(stages[4], UnpauseStage)
    assert {s.name for s in stages[4].promoted_snapshots} == {
        new_snapshot_a.name,
        new_snapshot_b.name,
    }

    # Verify BackfillStage
    backfill_stage = stages[5]
    assert isinstance(backfill_stage, BackfillStage)
    assert len(backfill_stage.snapshot_to_intervals) == 2
    expected_interval = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
    for intervals in backfill_stage.snapshot_to_intervals.values():
        assert intervals == expected_interval
    assert backfill_stage.deployability_index == DeployabilityIndex.all_deployable()

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[6]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 2
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {'"a"', '"b"'}

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[7], FinalizeEnvironmentStage)


def test_build_plan_stages_forward_only_dev(
    snapshot_a: Snapshot, snapshot_b: Snapshot, make_snapshot, mocker: MockerFixture
) -> None:
    # Categorize snapshot_a as forward-only
    new_snapshot_a = make_snapshot(
        snapshot_a.model.copy(update={"stamp": "new_version"}),
    )
    new_snapshot_a.previous_versions = snapshot_a.all_versions
    new_snapshot_a.categorize_as(SnapshotChangeCategory.NON_BREAKING, forward_only=True)

    new_snapshot_b = make_snapshot(
        snapshot_b.model.copy(),
        nodes={'"a"': new_snapshot_a.model},
    )
    new_snapshot_b.previous_versions = snapshot_b.all_versions
    new_snapshot_b.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING, forward_only=True)

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Create environment
    environment = Environment(
        name="dev",
        snapshots=[new_snapshot_a.table_info, new_snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[new_snapshot_a.snapshot_id, new_snapshot_b.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[new_snapshot_a, new_snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=True,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[new_snapshot_a.snapshot_id],
        indirectly_modified_snapshots={
            new_snapshot_a.name: [new_snapshot_b.snapshot_id],
        },
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 6

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[0]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[1]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }
    assert physical_stage.deployability_index == DeployabilityIndex.create(
        [new_snapshot_a, new_snapshot_b]
    )

    # Verify BackfillStage
    backfill_stage = stages[2]
    assert isinstance(backfill_stage, BackfillStage)
    assert len(backfill_stage.snapshot_to_intervals) == 2
    expected_interval = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
    for intervals in backfill_stage.snapshot_to_intervals.values():
        assert intervals == expected_interval
    assert backfill_stage.deployability_index == DeployabilityIndex.create(
        [new_snapshot_a, new_snapshot_b]
    )

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[3], EnvironmentRecordUpdateStage)

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[4]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 2
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {'"a"', '"b"'}

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[5], FinalizeEnvironmentStage)


def test_build_plan_stages_audit_only(
    snapshot_a: Snapshot, snapshot_b: Snapshot, make_snapshot, mocker: MockerFixture
) -> None:
    # Categorize snapshot_a as forward-only
    new_snapshot_a = make_snapshot(
        snapshot_a.model.copy(update={"audits": [("not_null", {})]}),
    )
    new_snapshot_a.previous_versions = snapshot_a.all_versions
    new_snapshot_a.categorize_as(SnapshotChangeCategory.METADATA)
    new_snapshot_a.add_interval("2023-01-01", "2023-01-02")

    new_snapshot_b = make_snapshot(
        snapshot_b.model.copy(),
        nodes={'"a"': new_snapshot_a.model},
    )
    new_snapshot_b.previous_versions = snapshot_b.all_versions
    new_snapshot_b.categorize_as(SnapshotChangeCategory.METADATA)
    new_snapshot_b.add_interval("2023-01-01", "2023-01-02")

    def _get_snapshots(snapshot_ids: t.List[SnapshotId]) -> t.Dict[SnapshotId, Snapshot]:
        if snapshot_a.snapshot_id in snapshot_ids and snapshot_b.snapshot_id in snapshot_ids:
            return {
                snapshot_a.snapshot_id: snapshot_a,
                snapshot_b.snapshot_id: snapshot_b,
            }
        return {}

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.side_effect = _get_snapshots
    state_reader.get_environment.return_value = None

    # Create environment
    environment = Environment(
        name="dev",
        snapshots=[new_snapshot_a.table_info, new_snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[new_snapshot_a.snapshot_id, new_snapshot_b.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[new_snapshot_a, new_snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=True,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[new_snapshot_a.snapshot_id],
        indirectly_modified_snapshots={
            new_snapshot_a.name: [new_snapshot_b.snapshot_id],
        },
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 8

    # Verify CreateSnapshotRecordsStage
    create_snapshot_records_stage = stages[0]
    assert isinstance(create_snapshot_records_stage, CreateSnapshotRecordsStage)
    assert len(create_snapshot_records_stage.snapshots) == 2
    assert {s.snapshot_id for s in create_snapshot_records_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }

    # Verify PhysicalLayerSchemaCreationStage
    physical_stage = stages[1]
    assert isinstance(physical_stage, PhysicalLayerSchemaCreationStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }
    assert physical_stage.deployability_index == DeployabilityIndex.create(
        [new_snapshot_a, new_snapshot_b]
    )

    # Verify PhysicalLayerUpdateStage
    physical_stage = stages[2]
    assert isinstance(physical_stage, PhysicalLayerUpdateStage)
    assert len(physical_stage.snapshots) == 2
    assert {s.snapshot_id for s in physical_stage.snapshots} == {
        new_snapshot_a.snapshot_id,
        new_snapshot_b.snapshot_id,
    }
    assert physical_stage.deployability_index == DeployabilityIndex.create(
        [new_snapshot_a, new_snapshot_b]
    )

    # Verify AuditOnlyRunStage
    audit_only_stage = stages[3]
    assert isinstance(audit_only_stage, AuditOnlyRunStage)
    assert len(audit_only_stage.snapshots) == 1
    assert audit_only_stage.snapshots[0].snapshot_id == new_snapshot_a.snapshot_id

    # Verify BackfillStage
    backfill_stage = stages[4]
    assert isinstance(backfill_stage, BackfillStage)
    assert len(backfill_stage.snapshot_to_intervals) == 0

    # Verify EnvironmentRecordUpdateStage
    assert isinstance(stages[5], EnvironmentRecordUpdateStage)

    # Verify VirtualLayerUpdateStage
    virtual_stage = stages[6]
    assert isinstance(virtual_stage, VirtualLayerUpdateStage)
    assert len(virtual_stage.promoted_snapshots) == 2
    assert len(virtual_stage.demoted_snapshots) == 0
    assert {s.name for s in virtual_stage.promoted_snapshots} == {'"a"', '"b"'}

    # Verify FinalizeEnvironmentStage
    assert isinstance(stages[7], FinalizeEnvironmentStage)


def test_build_plan_stages_forward_only_ensure_finalized_snapshots(
    snapshot_a: Snapshot, snapshot_b: Snapshot, make_snapshot, mocker: MockerFixture
) -> None:
    # Categorize snapshot_a as forward-only
    new_snapshot_a = make_snapshot(
        snapshot_a.model.copy(update={"stamp": "new_version"}),
    )
    new_snapshot_a.previous_versions = snapshot_a.all_versions
    new_snapshot_a.categorize_as(SnapshotChangeCategory.NON_BREAKING, forward_only=True)

    new_snapshot_b = make_snapshot(
        snapshot_b.model.copy(),
        nodes={'"a"': new_snapshot_a.model},
    )
    new_snapshot_b.previous_versions = snapshot_b.all_versions
    new_snapshot_b.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING, forward_only=True)

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    existing_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    # Create environment
    environment = Environment(
        name="prod",
        snapshots=[new_snapshot_a.table_info, new_snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[new_snapshot_a.snapshot_id, new_snapshot_b.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[new_snapshot_a, new_snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=True,
        ignore_cron=False,
        directly_modified_snapshots=[new_snapshot_a.snapshot_id],
        indirectly_modified_snapshots={
            new_snapshot_a.name: [new_snapshot_b.snapshot_id],
        },
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    assert len(stages) == 8
    assert isinstance(stages[0], CreateSnapshotRecordsStage)
    assert isinstance(stages[1], PhysicalLayerSchemaCreationStage)
    assert isinstance(stages[2], EnvironmentRecordUpdateStage)
    assert isinstance(stages[3], MigrateSchemasStage)
    assert isinstance(stages[4], BackfillStage)
    assert isinstance(stages[5], UnpauseStage)
    assert isinstance(stages[6], VirtualLayerUpdateStage)
    assert isinstance(stages[7], FinalizeEnvironmentStage)


def test_build_plan_stages_removed_model(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    existing_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    # Create environment
    environment = Environment(
        snapshots=[snapshot_a.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[snapshot_a.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[snapshot_b.snapshot_id],
        requires_backfill=False,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 5

    assert isinstance(stages[0], PhysicalLayerSchemaCreationStage)
    assert isinstance(stages[1], BackfillStage)
    assert isinstance(stages[2], EnvironmentRecordUpdateStage)
    assert isinstance(stages[3], VirtualLayerUpdateStage)
    assert isinstance(stages[4], FinalizeEnvironmentStage)

    virtual_layer_update_stage = stages[3]
    assert virtual_layer_update_stage.promoted_snapshots == set()
    assert virtual_layer_update_stage.demoted_snapshots == {snapshot_b.table_info}
    assert (
        virtual_layer_update_stage.demoted_environment_naming_info
        == existing_environment.naming_info
    )


def test_build_plan_stages_environment_suffix_target_changed(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    existing_environment = Environment(
        name="dev",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    # Create environment
    environment = Environment(
        name="dev",
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
        suffix_target=EnvironmentSuffixTarget.TABLE,
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=True,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=False,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 5

    assert isinstance(stages[0], PhysicalLayerSchemaCreationStage)
    assert isinstance(stages[1], BackfillStage)
    assert isinstance(stages[2], EnvironmentRecordUpdateStage)
    assert isinstance(stages[3], VirtualLayerUpdateStage)
    assert isinstance(stages[4], FinalizeEnvironmentStage)

    virtual_layer_update_stage = stages[3]
    assert virtual_layer_update_stage.promoted_snapshots == {
        snapshot_a.table_info,
        snapshot_b.table_info,
    }
    assert virtual_layer_update_stage.demoted_snapshots == {
        snapshot_a.table_info,
        snapshot_b.table_info,
    }
    assert (
        virtual_layer_update_stage.demoted_environment_naming_info
        == existing_environment.naming_info
    )


def test_build_plan_stages_indirect_non_breaking_view_migration(
    snapshot_a: Snapshot, snapshot_c: Snapshot, make_snapshot, mocker: MockerFixture
) -> None:
    # Categorize snapshot_a as forward-only
    new_snapshot_a = make_snapshot(
        snapshot_a.model.copy(update={"stamp": "new_version"}),
    )
    new_snapshot_a.previous_versions = snapshot_a.all_versions
    new_snapshot_a.categorize_as(SnapshotChangeCategory.NON_BREAKING)

    new_snapshot_c = make_snapshot(
        snapshot_c.model.copy(),
        nodes={'"a"': new_snapshot_a.model},
    )
    new_snapshot_c.previous_versions = snapshot_c.all_versions
    new_snapshot_c.change_category = SnapshotChangeCategory.INDIRECT_NON_BREAKING
    new_snapshot_c.version = new_snapshot_c.previous_version.data_version.version

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    existing_environment = Environment(
        name="prod",
        snapshots=[snapshot_a.table_info, snapshot_c.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_c.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    # Create environment
    environment = Environment(
        name="prod",
        snapshots=[new_snapshot_a.table_info, new_snapshot_c.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[new_snapshot_a.snapshot_id, new_snapshot_c.snapshot_id],
    )

    # Create evaluatable plan
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[new_snapshot_a, new_snapshot_c],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[new_snapshot_a.snapshot_id],
        indirectly_modified_snapshots={
            new_snapshot_a.name: [new_snapshot_c.snapshot_id],
        },
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    # Build plan stages
    stages = build_plan_stages(plan, state_reader, None)

    # Verify stages
    assert len(stages) == 9

    assert isinstance(stages[0], CreateSnapshotRecordsStage)
    assert isinstance(stages[1], PhysicalLayerSchemaCreationStage)
    assert isinstance(stages[2], BackfillStage)
    assert isinstance(stages[3], EnvironmentRecordUpdateStage)
    assert isinstance(stages[4], MigrateSchemasStage)
    assert isinstance(stages[5], UnpauseStage)
    assert isinstance(stages[6], BackfillStage)
    assert isinstance(stages[7], VirtualLayerUpdateStage)
    assert isinstance(stages[8], FinalizeEnvironmentStage)


def test_build_plan_stages_virtual_environment_mode_filtering(
    make_snapshot, mocker: MockerFixture
) -> None:
    # Create snapshots with different virtual environment modes
    snapshot_full = make_snapshot(
        SqlModel(
            name="full_model",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
            virtual_environment_mode=VirtualEnvironmentMode.FULL,
        )
    )
    snapshot_full.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_dev_only = make_snapshot(
        SqlModel(
            name="dev_only_model",
            query=parse_one("select 2, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
            virtual_environment_mode=VirtualEnvironmentMode.DEV_ONLY,
        )
    )
    snapshot_dev_only.categorize_as(SnapshotChangeCategory.BREAKING)

    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Test 1: Dev environment - both snapshots should be included
    environment_dev = Environment(
        name="dev",
        snapshots=[snapshot_full.table_info, snapshot_dev_only.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_full.snapshot_id, snapshot_dev_only.snapshot_id],
    )

    plan_dev = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_full, snapshot_dev_only],
        environment=environment_dev,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=True,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_full.snapshot_id, snapshot_dev_only.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    stages_dev = build_plan_stages(plan_dev, state_reader, None)

    # Find VirtualLayerUpdateStage
    virtual_stage_dev = next(
        stage for stage in stages_dev if isinstance(stage, VirtualLayerUpdateStage)
    )

    # In dev environment, both snapshots should be promoted regardless of virtual_environment_mode
    assert {s.name for s in virtual_stage_dev.promoted_snapshots} == {
        '"full_model"',
        '"dev_only_model"',
    }
    assert len(virtual_stage_dev.demoted_snapshots) == 0

    # Test 2: Production environment - only FULL mode snapshots should be included
    environment_prod = Environment(
        name="prod",
        snapshots=[snapshot_full.table_info, snapshot_dev_only.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_full.snapshot_id, snapshot_dev_only.snapshot_id],
    )

    plan_prod = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_full, snapshot_dev_only],
        environment=environment_prod,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_full.snapshot_id, snapshot_dev_only.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    stages_prod = build_plan_stages(plan_prod, state_reader, None)

    # Find VirtualLayerUpdateStage
    virtual_stage_prod = next(
        stage for stage in stages_prod if isinstance(stage, VirtualLayerUpdateStage)
    )

    # In production environment, only FULL mode snapshots should be promoted
    assert {s.name for s in virtual_stage_prod.promoted_snapshots} == {'"full_model"'}
    assert len(virtual_stage_prod.demoted_snapshots) == 0

    # Test 3: Production environment with demoted snapshots
    existing_environment = Environment(
        name="prod",
        snapshots=[snapshot_full.table_info, snapshot_dev_only.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_full.snapshot_id, snapshot_dev_only.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    # Remove both snapshots from the new environment
    environment_prod_demote = Environment(
        name="prod",
        snapshots=[],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[],
    )

    plan_prod_demote = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[],
        environment=environment_prod_demote,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[snapshot_full.snapshot_id, snapshot_dev_only.snapshot_id],
        requires_backfill=False,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    stages_prod_demote = build_plan_stages(plan_prod_demote, state_reader, None)

    # Find VirtualLayerUpdateStage
    virtual_stage_prod_demote = next(
        stage for stage in stages_prod_demote if isinstance(stage, VirtualLayerUpdateStage)
    )

    # In production environment, only FULL mode snapshots should be demoted
    assert len(virtual_stage_prod_demote.promoted_snapshots) == 0
    assert {s.name for s in virtual_stage_prod_demote.demoted_snapshots} == {'"full_model"'}
    assert (
        virtual_stage_prod_demote.demoted_environment_naming_info
        == existing_environment.naming_info
    )


def test_build_plan_stages_virtual_environment_mode_no_updates(
    snapshot_a: Snapshot, make_snapshot, mocker: MockerFixture
) -> None:
    # Create snapshot with DEV_ONLY mode
    snapshot_dev_only = make_snapshot(
        SqlModel(
            name="dev_only_model",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
            virtual_environment_mode=VirtualEnvironmentMode.DEV_ONLY,
        )
    )
    snapshot_dev_only.categorize_as(SnapshotChangeCategory.BREAKING)

    # Mock state reader
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    # Production environment with only DEV_ONLY snapshots
    environment = Environment(
        name="prod",
        snapshots=[snapshot_dev_only.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_dev_only.snapshot_id],
    )

    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_dev_only],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[snapshot_dev_only.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    stages = build_plan_stages(plan, state_reader, None)

    # No VirtualLayerUpdateStage should be created since all snapshots are filtered out
    virtual_stages = [stage for stage in stages if isinstance(stage, VirtualLayerUpdateStage)]
    assert len(virtual_stages) == 0


def test_adjust_intervals_new_forward_only_dev_intervals(
    make_snapshot, mocker: MockerFixture
) -> None:
    forward_only_snapshot = make_snapshot(
        SqlModel(
            name="forward_only_model",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )
    forward_only_snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only=True)
    forward_only_snapshot.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]

    forward_only_snapshot.dev_intervals = []

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.refresh_snapshot_intervals = mocker.Mock()
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    environment = Environment(
        snapshots=[forward_only_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[forward_only_snapshot.snapshot_id],
    )

    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[forward_only_snapshot],  # This snapshot should have dev_intervals set
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=True,  # Dev environment
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    assert forward_only_snapshot.dev_intervals == []

    build_plan_stages(plan, state_reader, None)

    assert forward_only_snapshot.dev_intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))
    ]
    assert forward_only_snapshot.dev_intervals is not forward_only_snapshot.intervals

    state_reader.refresh_snapshot_intervals.assert_called_once()


def test_adjust_intervals_restatement_removal(
    snapshot_a: Snapshot, snapshot_b: Snapshot, mocker: MockerFixture
) -> None:
    snapshot_a.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-04"))]
    snapshot_b.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]

    original_a_intervals = snapshot_a.intervals.copy()
    original_b_intervals = snapshot_b.intervals.copy()

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.refresh_snapshot_intervals = mocker.Mock()
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None
    state_reader.get_environments_summary.return_value = []

    environment = Environment(
        snapshots=[snapshot_a.table_info, snapshot_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[snapshot_a.snapshot_id, snapshot_b.snapshot_id],
    )

    restatements = {
        snapshot_a.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        snapshot_b.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    }

    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[snapshot_a, snapshot_b],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements=restatements,
        restate_all_snapshots=True,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    stages = build_plan_stages(plan, state_reader, None)

    assert snapshot_a.intervals != original_a_intervals
    assert snapshot_b.intervals != original_b_intervals

    state_reader.refresh_snapshot_intervals.assert_called_once()

    restatement_stages = [stage for stage in stages if isinstance(stage, RestatementStage)]
    assert len(restatement_stages) == 1

    backfill_stages = [stage for stage in stages if isinstance(stage, BackfillStage)]
    assert len(backfill_stages) == 1
    (snapshot, intervals) = next(iter(backfill_stages[0].snapshot_to_intervals.items()))
    assert snapshot.intervals == [(to_timestamp("2023-01-02"), to_timestamp("2023-01-04"))]
    assert intervals == [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]


def test_adjust_intervals_should_force_rebuild(make_snapshot, mocker: MockerFixture) -> None:
    old_snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds"),
        )
    )
    old_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    old_snapshot.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]

    new_snapshot = make_snapshot(
        SqlModel(
            name="test_model",
            query=parse_one("select 1, ds"),
            kind=dict(name=ModelKindName.FULL),
        )
    )
    new_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    new_snapshot.version = old_snapshot.version
    new_snapshot.intervals = [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.refresh_snapshot_intervals = mocker.Mock()
    state_reader.get_snapshots.side_effect = [{}, {old_snapshot.snapshot_id: old_snapshot}, {}, {}]

    existing_environment = Environment(
        name="prod",
        snapshots=[old_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="previous_plan",
        promoted_snapshot_ids=[old_snapshot.snapshot_id],
        finalized_ts=to_timestamp("2023-01-02"),
    )
    state_reader.get_environment.return_value = existing_environment

    environment = Environment(
        snapshots=[new_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id="previous_plan",
        promoted_snapshot_ids=[new_snapshot.snapshot_id],
    )

    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=[new_snapshot],
        environment=environment,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=False,
        ensure_finalized_snapshots=False,
        ignore_cron=False,
        directly_modified_snapshots=[new_snapshot.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )

    stages = build_plan_stages(plan, state_reader, None)

    state_reader.refresh_snapshot_intervals.assert_called_once()
    state_reader.get_environment.assert_called()

    assert not new_snapshot.intervals
    backfill_stages = [stage for stage in stages if isinstance(stage, BackfillStage)]
    assert len(backfill_stages) == 1
    (snapshot, intervals) = next(iter(backfill_stages[0].snapshot_to_intervals.items()))
    assert not snapshot.intervals
    assert intervals == [(to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))]
