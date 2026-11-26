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


@pytest.mark.parametrize("base_view_unpaused", [False, True])
def test_build_plan_stages_migrates_views_downstream_of_migrated_view(
    snapshot_a: Snapshot, make_snapshot, mocker: MockerFixture, base_view_unpaused: bool
) -> None:
    base_view = make_snapshot(
        SqlModel(name="base_view", query=parse_one("select * from a"), kind=ModelKindName.VIEW),
        nodes={'"a"': snapshot_a.model},
    )
    base_view.categorize_as(SnapshotChangeCategory.BREAKING)
    if base_view_unpaused:
        base_view.unpaused_ts = 1
    public_view = make_snapshot(
        SqlModel(
            name="public_view", query=parse_one("select * from base_view"), kind=ModelKindName.VIEW
        ),
        nodes={'"a"': snapshot_a.model, '"base_view"': base_view.model},
    )
    public_view.categorize_as(SnapshotChangeCategory.BREAKING)
    forward_only = make_snapshot(
        SqlModel(
            name="forward_only",
            query=parse_one("select 1, ds from base_view"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", forward_only=True
            ),
        ),
        nodes={'"a"': snapshot_a.model, '"base_view"': base_view.model},
    )
    forward_only.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshots = [snapshot_a, base_view, public_view, forward_only]

    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None

    environment = Environment(
        snapshots=[s.table_info for s in snapshots],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
        promoted_snapshot_ids=[s.snapshot_id for s in snapshots],
    )
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=snapshots,
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
        directly_modified_snapshots=[s.snapshot_id for s in snapshots],
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

    migrate_stage = next(s for s in stages if isinstance(s, MigrateSchemasStage))
    expected = [snapshot_a, forward_only] if base_view_unpaused else snapshots
    assert {s.snapshot_id for s in migrate_stage.snapshots} == {s.snapshot_id for s in expected}


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


def _make_mv(
    make_snapshot,
    name: str = "db.mv",
    query: str = "SELECT 1 AS a",
    nodes: t.Optional[t.Dict[str, t.Any]] = None,
    category: SnapshotChangeCategory = SnapshotChangeCategory.BREAKING,
    forward_only: bool = False,
    previous_versions: t.Optional[t.Tuple[t.Any, ...]] = None,
    unpaused: bool = False,
    **model_kwargs: t.Any,
) -> Snapshot:
    snapshot = make_snapshot(
        SqlModel(
            name=name,
            query=parse_one(query),
            kind=dict(name=ModelKindName.VIEW, materialized=True),
            **model_kwargs,
        ),
        nodes=nodes,
    )
    if previous_versions is not None:
        snapshot.previous_versions = previous_versions
    snapshot.categorize_as(category, forward_only=forward_only)
    if unpaused:
        snapshot.unpaused_ts = to_timestamp("2023-01-02")
    return snapshot


def _make_prod_env(
    snapshots: t.List[Snapshot],
    plan_id: str = "prod_v1",
    finalized: bool = True,
    previous_finalized_snapshots: t.Optional[t.List[Snapshot]] = None,
    expiration_ts: t.Optional[int] = None,
) -> Environment:
    return Environment(
        name="prod",
        snapshots=[s.table_info for s in snapshots],
        previous_finalized_snapshots=(
            [s.table_info for s in previous_finalized_snapshots]
            if previous_finalized_snapshots is not None
            else None
        ),
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id=plan_id,
        finalized_ts=to_timestamp("2023-01-02") if finalized else None,
        expiration_ts=expiration_ts,
    )


def _build_prod_plan_for_mv(
    snapshots: t.List[Snapshot],
    new_snapshots: t.List[Snapshot],
    existing_environment: t.Optional[Environment],
    stored_snapshots: t.Dict[SnapshotId, Snapshot],
    mocker: MockerFixture,
) -> t.List[t.Any]:
    state_reader = mocker.Mock(spec=StateReader)
    state_reader.get_snapshots.side_effect = lambda ids: {
        s_id: stored_snapshots[s_id]
        for s_id in (x.snapshot_id for x in ids)
        if s_id in stored_snapshots
    }
    state_reader.get_environment.return_value = existing_environment

    existing_name_versions = set()
    if existing_environment:
        for s in existing_environment.snapshots:
            existing_name_versions.add((s.name, s.version))
        for s in existing_environment.previous_finalized_snapshots or []:
            existing_name_versions.add((s.name, s.version))

    def _refresh_intervals(snaps: t.Iterable[Snapshot]) -> t.List[Snapshot]:
        refreshed = list(snaps)
        for s in refreshed:
            if s.is_model and not s.intervals and (s.name, s.version) in existing_name_versions:
                s.add_interval("2023-01-01", "2023-01-02")
        return refreshed

    state_reader.refresh_snapshot_intervals.side_effect = _refresh_intervals

    environment = Environment(
        name="prod",
        snapshots=[s.table_info for s in snapshots],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="test_plan",
        previous_plan_id=existing_environment.plan_id if existing_environment else None,
        promoted_snapshot_ids=None,
    )
    plan = EvaluatablePlan(
        start="2023-01-01",
        end="2023-01-02",
        new_snapshots=new_snapshots,
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
        metadata_updated_snapshots=[s.snapshot_id for s in new_snapshots if s.is_metadata],
        removed_snapshots=[],
        requires_backfill=False,
        models_to_backfill=None,
        execution_time="2023-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
    )
    return build_plan_stages(plan, state_reader, None)


@pytest.mark.registry_isolation
@pytest.mark.parametrize(
    "change_type",
    ["tags", "owner", "sql_comment", "metadata_only_macro"],
)
def test_build_plan_stages_mv_metadata_only_skips_migrate(
    make_snapshot, mocker: MockerFixture, change_type: str
) -> None:
    from sqlmesh.core.dialect import parse as parse_sql
    from sqlmesh.core.macros import macro
    from sqlmesh.core.model import load_sql_based_model

    if change_type == "metadata_only_macro":
        try:
            macro.registry().pop("my_meta_macro", None)

            @macro(metadata_only=True)
            def my_meta_macro(evaluator):  # type: ignore
                return "1"

            mv_v1 = make_snapshot(
                load_sql_based_model(
                    parse_sql(
                        "MODEL (name db.mv, kind VIEW (materialized true)); "
                        "SELECT @my_meta_macro() AS a"
                    ),
                    macros=macro.get_registry(),
                )
            )
            mv_v1.categorize_as(SnapshotChangeCategory.BREAKING)
            mv_v1.unpaused_ts = to_timestamp("2023-01-02")

            macro.registry().pop("my_meta_macro", None)

            @macro(metadata_only=True)
            def my_meta_macro(evaluator):  # type: ignore
                _unused = 1
                return "1"

            mv_v2 = make_snapshot(
                load_sql_based_model(
                    parse_sql(
                        "MODEL (name db.mv, kind VIEW (materialized true)); "
                        "SELECT @my_meta_macro() AS a"
                    ),
                    macros=macro.get_registry(),
                )
            )
            assert mv_v2.snapshot_id != mv_v1.snapshot_id
        finally:
            macro.registry().pop("my_meta_macro", None)
    else:
        base_sql = "MODEL (name db.mv, kind VIEW (materialized true)); SELECT 1 AS a"
        mv_v1 = make_snapshot(load_sql_based_model(parse_sql(base_sql)))
        mv_v1.categorize_as(SnapshotChangeCategory.BREAKING)
        mv_v1.unpaused_ts = to_timestamp("2023-01-02")

        if change_type == "tags":
            updated_sql = (
                "MODEL (name db.mv, kind VIEW (materialized true), tags ['analytics']); "
                "SELECT 1 AS a"
            )
        elif change_type == "owner":
            updated_sql = (
                "MODEL (name db.mv, kind VIEW (materialized true), owner alice); SELECT 1 AS a"
            )
        else:
            updated_sql = (
                "MODEL (name db.mv, kind VIEW (materialized true)); "
                "SELECT 1 AS a -- column doc comment"
            )
        mv_v2 = make_snapshot(load_sql_based_model(parse_sql(updated_sql)))

    mv_v2.previous_versions = mv_v1.all_versions
    mv_v2.categorize_as(SnapshotChangeCategory.METADATA)
    assert mv_v2.requires_schema_migration_in_prod

    stages = _build_prod_plan_for_mv(
        snapshots=[mv_v2],
        new_snapshots=[mv_v2],
        existing_environment=_make_prod_env([mv_v1], plan_id="prev_plan"),
        stored_snapshots={mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )

    assert not any(isinstance(s, MigrateSchemasStage) for s in stages)


@pytest.mark.parametrize(
    "case,expect_migrate",
    [
        ("unfinalized_forward_only_retry", True),
        ("unfinalized_forward_only_revert_to_metadata", True),
        ("unfinalized_metadata_retry", False),
        ("never_finalized", True),
        ("expired", True),
    ],
)
def test_build_plan_stages_mv_unfinalized_or_expired_env(
    make_snapshot, mocker: MockerFixture, case: str, expect_migrate: bool
) -> None:
    mv_v1 = _make_mv(make_snapshot, unpaused=case != "never_finalized")

    if case == "unfinalized_forward_only_retry":
        mv_v2 = _make_mv(
            make_snapshot,
            query="SELECT 2 AS a",
            category=SnapshotChangeCategory.NON_BREAKING,
            forward_only=True,
            previous_versions=mv_v1.all_versions,
        )
        existing_env = _make_prod_env(
            [mv_v2],
            plan_id="failed_plan",
            finalized=False,
            previous_finalized_snapshots=[mv_v1],
        )
        candidate = mv_v2
        new_snapshots: t.List[Snapshot] = []
        stored = {mv_v1.snapshot_id: mv_v1, mv_v2.snapshot_id: mv_v2}
    elif case == "unfinalized_forward_only_revert_to_metadata":
        # Plan 1 ran MigrateSchemasStage with forward-only SQL `SELECT 2` and then failed before
        # finalization. Plan 2 reverts to `SELECT 1` + metadata edit; it must migrate so the live
        # warehouse MV is not left on Plan 1's SQL.
        mv_failed_fo = _make_mv(
            make_snapshot,
            query="SELECT 2 AS a",
            category=SnapshotChangeCategory.NON_BREAKING,
            forward_only=True,
            previous_versions=mv_v1.all_versions,
        )
        mv_reverted = make_snapshot(mv_v1.model.copy(update={"owner": "alice"}))
        mv_reverted.previous_versions = mv_v1.all_versions
        mv_reverted.categorize_as(SnapshotChangeCategory.METADATA)
        existing_env = _make_prod_env(
            [mv_failed_fo],
            plan_id="failed_plan",
            finalized=False,
            previous_finalized_snapshots=[mv_v1],
        )
        candidate = mv_reverted
        new_snapshots = [mv_reverted]
        stored = {mv_v1.snapshot_id: mv_v1, mv_failed_fo.snapshot_id: mv_failed_fo}
    else:
        mv_v2 = make_snapshot(mv_v1.model.copy(update={"owner": "alice"}))
        mv_v2.previous_versions = mv_v1.all_versions
        mv_v2.categorize_as(SnapshotChangeCategory.METADATA)
        candidate = mv_v2
        if case == "unfinalized_metadata_retry":
            existing_env = _make_prod_env(
                [mv_v2],
                plan_id="failed_plan",
                finalized=False,
                previous_finalized_snapshots=[mv_v1],
            )
            new_snapshots = []
            stored = {mv_v1.snapshot_id: mv_v1, mv_v2.snapshot_id: mv_v2}
        elif case == "never_finalized":
            existing_env = _make_prod_env(
                [mv_v1],
                plan_id="failed_initial",
                finalized=False,
                previous_finalized_snapshots=None,
            )
            new_snapshots = [mv_v2]
            stored = {mv_v1.snapshot_id: mv_v1}
        else:
            existing_env = _make_prod_env(
                [mv_v1],
                plan_id="expired_plan",
                finalized=True,
                expiration_ts=1,
            )
            new_snapshots = [mv_v2]
            stored = {mv_v1.snapshot_id: mv_v1}

    stages = _build_prod_plan_for_mv(
        snapshots=[candidate],
        new_snapshots=new_snapshots,
        existing_environment=existing_env,
        stored_snapshots=stored,
        mocker=mocker,
    )

    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    if expect_migrate:
        assert len(migrate_stages) == 1
        assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [candidate.snapshot_id]
        assert migrate_stages[0].grants_only_snapshot_ids == set()
        assert migrate_stages[0].create_only_snapshot_ids == set()
    else:
        assert len(migrate_stages) == 0


def test_build_plan_stages_mv_unfinalized_grants_revert_syncs_grants(
    make_snapshot, mocker: MockerFixture
) -> None:
    from sqlmesh.core.model.meta import GrantsTargetLayer

    mv_v1 = _make_mv(
        make_snapshot,
        unpaused=True,
        grants={"SELECT": ["role_a"]},
        grants_target_layer=GrantsTargetLayer.ALL,
    )
    mv_failed_grants = make_snapshot(mv_v1.model.copy(update={"grants": {"SELECT": ["role_b"]}}))
    mv_failed_grants.previous_versions = mv_v1.all_versions
    mv_failed_grants.categorize_as(SnapshotChangeCategory.METADATA)

    mv_reverted = make_snapshot(mv_v1.model.copy(update={"owner": "alice"}))
    mv_reverted.previous_versions = mv_v1.all_versions
    mv_reverted.categorize_as(SnapshotChangeCategory.METADATA)

    existing_env = _make_prod_env(
        [mv_failed_grants],
        plan_id="failed_grants_plan",
        finalized=False,
        previous_finalized_snapshots=[mv_v1],
    )
    stages = _build_prod_plan_for_mv(
        snapshots=[mv_reverted],
        new_snapshots=[mv_reverted],
        existing_environment=existing_env,
        stored_snapshots={mv_v1.snapshot_id: mv_v1, mv_failed_grants.snapshot_id: mv_failed_grants},
        mocker=mocker,
    )
    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [mv_reverted.snapshot_id]
    assert migrate_stages[0].grants_only_snapshot_ids == {mv_reverted.snapshot_id}
    assert migrate_stages[0].create_only_snapshot_ids == set()


def test_build_plan_stages_mv_metadata_only_with_backfill_routes_to_create_only(
    make_snapshot, mocker: MockerFixture
) -> None:
    mv_v1 = _make_mv(make_snapshot, unpaused=True)
    mv_v2 = make_snapshot(mv_v1.model.copy(update={"owner": "alice"}))
    mv_v2.previous_versions = mv_v1.all_versions
    mv_v2.categorize_as(SnapshotChangeCategory.METADATA)

    # An unrelated model requires backfill, so PhysicalLayerUpdateStage is skipped.
    # The metadata-only MV must be routed into create_only_snapshot_ids so it is created if missing
    # without being recreated when it already exists.
    other_model = make_snapshot(
        SqlModel(
            name="db.other",
            query=parse_one("SELECT 1 AS x"),
            kind=dict(name=ModelKindName.FULL),
            start="2023-01-01",
        )
    )
    other_model.categorize_as(SnapshotChangeCategory.BREAKING)

    stages = _build_prod_plan_for_mv(
        snapshots=[mv_v2, other_model],
        new_snapshots=[mv_v2, other_model],
        existing_environment=_make_prod_env([mv_v1]),
        stored_snapshots={mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )
    assert not any(isinstance(s, PhysicalLayerUpdateStage) for s in stages)
    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [mv_v2.snapshot_id]
    assert migrate_stages[0].grants_only_snapshot_ids == set()
    assert migrate_stages[0].create_only_snapshot_ids == {mv_v2.snapshot_id}


def test_build_plan_stages_mv_stacked_dev_forward_only_plus_metadata_migrates(
    make_snapshot, mocker: MockerFixture
) -> None:
    mv_v1 = _make_mv(make_snapshot, unpaused=True)

    # Dev step 1: forward-only SQL change (never deployed to prod)
    mv_v2 = _make_mv(
        make_snapshot,
        query="SELECT 2 AS a",
        category=SnapshotChangeCategory.NON_BREAKING,
        forward_only=True,
        previous_versions=mv_v1.all_versions,
    )

    # Dev step 2: metadata-only change stacked on top of v2
    mv_v3 = make_snapshot(mv_v2.model.copy(update={"owner": "bob"}))
    mv_v3.previous_versions = mv_v2.all_versions
    mv_v3.categorize_as(SnapshotChangeCategory.METADATA)

    stages = _build_prod_plan_for_mv(
        snapshots=[mv_v3],
        new_snapshots=[],
        existing_environment=_make_prod_env([mv_v1]),
        stored_snapshots={mv_v1.snapshot_id: mv_v1, mv_v3.snapshot_id: mv_v3},
        mocker=mocker,
    )

    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [mv_v3.snapshot_id]
    assert migrate_stages[0].grants_only_snapshot_ids == set()
    assert migrate_stages[0].create_only_snapshot_ids == set()


@pytest.mark.parametrize(
    "mode",
    ["physical_version", "vde_dev_only"],
)
def test_build_plan_stages_mv_physical_version_and_vde_dev_only(
    make_snapshot, mocker: MockerFixture, mode: str
) -> None:
    kwargs: t.Dict[str, t.Any] = (
        {"physical_version": "v1"}
        if mode == "physical_version"
        else {"virtual_environment_mode": VirtualEnvironmentMode.DEV_ONLY}
    )
    mv_v1 = _make_mv(make_snapshot, unpaused=True, **kwargs)
    existing_prod = _make_prod_env([mv_v1])

    # Metadata-only change: must skip MigrateSchemasStage
    mv_meta = make_snapshot(mv_v1.model.copy(update={"owner": "alice"}))
    mv_meta.previous_versions = mv_v1.all_versions
    mv_meta.categorize_as(SnapshotChangeCategory.METADATA)

    stages_meta = _build_prod_plan_for_mv(
        snapshots=[mv_meta],
        new_snapshots=[mv_meta],
        existing_environment=existing_prod,
        stored_snapshots={mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )
    assert not any(isinstance(s, MigrateSchemasStage) for s in stages_meta)

    # Data change (or physical_version bump): must still emit MigrateSchemasStage
    changed_kwargs: t.Dict[str, t.Any] = (
        {"physical_version": "v2"}
        if mode == "physical_version"
        else {"virtual_environment_mode": VirtualEnvironmentMode.DEV_ONLY}
    )
    mv_changed = _make_mv(
        make_snapshot,
        query="SELECT 1 AS a" if mode == "physical_version" else "SELECT 2 AS a",
        previous_versions=mv_v1.all_versions,
        **changed_kwargs,
    )

    stages_changed = _build_prod_plan_for_mv(
        snapshots=[mv_changed],
        new_snapshots=[mv_changed],
        existing_environment=existing_prod,
        stored_snapshots={mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )
    migrate_stages = [s for s in stages_changed if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [mv_changed.snapshot_id]
    assert migrate_stages[0].grants_only_snapshot_ids == set()


@pytest.mark.parametrize(
    "old_grants,old_layer,new_grants,new_layer,vde_mode,expect_grants_only",
    [
        # Add physical grants
        (None, "physical", {"select": ["u1"]}, "physical", VirtualEnvironmentMode.FULL, True),
        # Add grants targeting ALL layers
        (None, "all", {"select": ["u1"]}, "all", VirtualEnvironmentMode.FULL, True),
        # Update physical grants
        (
            {"select": ["u1"]},
            "physical",
            {"select": ["u2"]},
            "physical",
            VirtualEnvironmentMode.FULL,
            True,
        ),
        # Revoke all physical grants ({} != None)
        ({"select": ["u1"]}, "physical", {}, "physical", VirtualEnvironmentMode.FULL, True),
        # Switch grants_target_layer from virtual -> physical
        (
            {"select": ["u1"]},
            "virtual",
            {"select": ["u1"]},
            "physical",
            VirtualEnvironmentMode.FULL,
            True,
        ),
        # VDE=DEV_ONLY with virtual layer still targets physical in prod
        (
            None,
            "virtual",
            {"select": ["u1"]},
            "virtual",
            VirtualEnvironmentMode.DEV_ONLY,
            True,
        ),
        # Unchanged physical grants (only owner changed) -> skip MigrateSchemasStage
        (
            {"select": ["u1"]},
            "physical",
            {"select": ["u1"]},
            "physical",
            VirtualEnvironmentMode.FULL,
            False,
        ),
        # Virtual-only grants change under FULL VDE -> handled in virtual layer only
        (
            None,
            "virtual",
            {"select": ["u1"]},
            "virtual",
            VirtualEnvironmentMode.FULL,
            False,
        ),
    ],
)
def test_build_plan_stages_mv_grants_only_sync(
    make_snapshot,
    mocker: MockerFixture,
    old_grants: t.Optional[t.Dict[str, t.List[str]]],
    old_layer: str,
    new_grants: t.Optional[t.Dict[str, t.List[str]]],
    new_layer: str,
    vde_mode: VirtualEnvironmentMode,
    expect_grants_only: bool,
) -> None:
    from sqlmesh.core.model.meta import GrantsTargetLayer

    v1_kwargs: t.Dict[str, t.Any] = {
        "grants_target_layer": GrantsTargetLayer(old_layer),
        "virtual_environment_mode": vde_mode,
    }
    if old_grants is not None:
        v1_kwargs["grants"] = old_grants

    mv_v1 = _make_mv(make_snapshot, unpaused=True, **v1_kwargs)

    v2_kwargs: t.Dict[str, t.Any] = {
        "grants_target_layer": GrantsTargetLayer(new_layer),
        "virtual_environment_mode": vde_mode,
        "owner": "alice",
    }
    if new_grants is not None:
        v2_kwargs["grants"] = new_grants

    mv_v2 = _make_mv(
        make_snapshot,
        category=SnapshotChangeCategory.METADATA,
        previous_versions=mv_v1.all_versions,
        **v2_kwargs,
    )

    stages = _build_prod_plan_for_mv(
        snapshots=[mv_v2],
        new_snapshots=[mv_v2],
        existing_environment=_make_prod_env([mv_v1]),
        stored_snapshots={mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )

    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    if expect_grants_only:
        assert len(migrate_stages) == 1
        assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [mv_v2.snapshot_id]
        assert migrate_stages[0].grants_only_snapshot_ids == {mv_v2.snapshot_id}
        assert migrate_stages[0].create_only_snapshot_ids == set()
    else:
        assert len(migrate_stages) == 0


def test_build_plan_stages_mv_upstream_subdag_create_only(
    make_snapshot, mocker: MockerFixture
) -> None:
    mv_upstream = _make_mv(
        make_snapshot,
        name="db.mv_upstream",
        query="SELECT 1 AS a, '2023-01-01' AS ds",
    )
    # Sibling downstream MV that should NOT be cascaded into MigrateSchemasStage
    mv_sibling = _make_mv(
        make_snapshot,
        name="db.mv_sibling",
        query="SELECT a FROM db.mv_upstream",
        nodes={mv_upstream.name: mv_upstream.model},
    )
    downstream_fo = make_snapshot(
        SqlModel(
            name="db.downstream_fo",
            query=parse_one("SELECT a, ds FROM db.mv_upstream"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", forward_only=True
            ),
        ),
        nodes={mv_upstream.name: mv_upstream.model},
    )
    downstream_fo.categorize_as(SnapshotChangeCategory.BREAKING)

    stages = _build_prod_plan_for_mv(
        snapshots=[mv_upstream, mv_sibling, downstream_fo],
        new_snapshots=[mv_upstream, mv_sibling, downstream_fo],
        existing_environment=None,
        stored_snapshots={},
        mocker=mocker,
    )

    migrate_stage = next(s for s in stages if isinstance(s, MigrateSchemasStage))
    assert {s.snapshot_id for s in migrate_stage.snapshots} == {
        mv_upstream.snapshot_id,
        downstream_fo.snapshot_id,
    }
    assert migrate_stage.create_only_snapshot_ids == {mv_upstream.snapshot_id}
    assert migrate_stage.grants_only_snapshot_ids == set()


def test_build_plan_stages_mv_cascade_override_from_upstream_regular_view(
    make_snapshot, mocker: MockerFixture
) -> None:
    from sqlmesh.core.model.meta import GrantsTargetLayer

    v_up_v1 = make_snapshot(
        SqlModel(
            name="db.v_up",
            query=parse_one("SELECT 1 AS a, '2023-01-01' AS ds"),
            kind=dict(name=ModelKindName.VIEW),
        )
    )
    v_up_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    v_up_v1.unpaused_ts = to_timestamp("2023-01-02")

    mv_meta_v1 = _make_mv(
        make_snapshot,
        name="db.mv_meta",
        query="SELECT a FROM db.v_up",
        nodes={v_up_v1.name: v_up_v1.model},
        unpaused=True,
    )
    mv_grants_v1 = _make_mv(
        make_snapshot,
        name="db.mv_grants",
        query="SELECT a FROM db.v_up",
        nodes={v_up_v1.name: v_up_v1.model},
        unpaused=True,
        grants_target_layer=GrantsTargetLayer.PHYSICAL,
    )
    mv_create_only_v1 = _make_mv(
        make_snapshot,
        name="db.mv_create_only",
        query="SELECT a, ds FROM db.v_up",
        nodes={v_up_v1.name: v_up_v1.model},
        unpaused=True,
    )
    tbl_fo_v1 = make_snapshot(
        SqlModel(
            name="db.tbl_fo",
            query=parse_one("SELECT a, ds FROM db.mv_create_only"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", forward_only=True
            ),
        ),
        nodes={v_up_v1.name: v_up_v1.model, mv_create_only_v1.name: mv_create_only_v1.model},
    )
    tbl_fo_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    tbl_fo_v1.unpaused_ts = to_timestamp("2023-01-02")

    # Upstream regular VIEW gets a metadata-only change -> still migrated (not materialized)
    v_up_v2 = make_snapshot(v_up_v1.model.copy(update={"owner": "alice"}))
    v_up_v2.previous_versions = v_up_v1.all_versions
    v_up_v2.categorize_as(SnapshotChangeCategory.METADATA)

    # Downstream MVs would otherwise be skipped / grants_only / create_only
    mv_meta_v2 = _make_mv(
        make_snapshot,
        name="db.mv_meta",
        query="SELECT a FROM db.v_up",
        nodes={v_up_v2.name: v_up_v2.model},
        category=SnapshotChangeCategory.METADATA,
        previous_versions=mv_meta_v1.all_versions,
    )
    mv_grants_v2 = _make_mv(
        make_snapshot,
        name="db.mv_grants",
        query="SELECT a FROM db.v_up",
        nodes={v_up_v2.name: v_up_v2.model},
        category=SnapshotChangeCategory.METADATA,
        previous_versions=mv_grants_v1.all_versions,
        grants={"select": ["u1"]},
        grants_target_layer=GrantsTargetLayer.PHYSICAL,
    )
    mv_create_only_v2 = _make_mv(
        make_snapshot,
        name="db.mv_create_only",
        query="SELECT a, ds FROM db.v_up",
        nodes={v_up_v2.name: v_up_v2.model},
        category=SnapshotChangeCategory.METADATA,
        previous_versions=mv_create_only_v1.all_versions,
    )
    tbl_fo_v2 = make_snapshot(
        SqlModel(
            name="db.tbl_fo",
            query=parse_one("SELECT a, a AS b, ds FROM db.mv_create_only"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", forward_only=True
            ),
        ),
        nodes={v_up_v2.name: v_up_v2.model, mv_create_only_v2.name: mv_create_only_v2.model},
    )
    tbl_fo_v2.previous_versions = tbl_fo_v1.all_versions
    tbl_fo_v2.categorize_as(SnapshotChangeCategory.NON_BREAKING, forward_only=True)

    existing_prod = _make_prod_env(
        [v_up_v1, mv_meta_v1, mv_grants_v1, mv_create_only_v1, tbl_fo_v1]
    )
    new_snaps = [v_up_v2, mv_meta_v2, mv_grants_v2, mv_create_only_v2, tbl_fo_v2]
    stored = {
        s.snapshot_id: s for s in (v_up_v1, mv_meta_v1, mv_grants_v1, mv_create_only_v1, tbl_fo_v1)
    }

    stages = _build_prod_plan_for_mv(
        snapshots=new_snaps,
        new_snapshots=new_snaps,
        existing_environment=existing_prod,
        stored_snapshots=stored,
        mocker=mocker,
    )

    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    migrate_stage = migrate_stages[0]
    assert {s.snapshot_id for s in migrate_stage.snapshots} == {s.snapshot_id for s in new_snaps}
    assert migrate_stage.grants_only_snapshot_ids == set()
    assert migrate_stage.create_only_snapshot_ids == set()


def test_build_plan_stages_mv_upstream_metadata_only_propagates_without_recreate(
    make_snapshot, mocker: MockerFixture
) -> None:
    src_v1 = make_snapshot(
        SqlModel(
            name="db.src",
            query=parse_one("SELECT 1 AS a"),
            kind=dict(name=ModelKindName.FULL),
        )
    )
    src_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    src_v1.unpaused_ts = to_timestamp("2023-01-02")

    mv_v1 = _make_mv(
        make_snapshot,
        query="SELECT a FROM db.src",
        nodes={src_v1.name: src_v1.model},
        unpaused=True,
    )

    src_v2 = make_snapshot(src_v1.model.copy(update={"owner": "alice", "tags": ["core"]}))
    src_v2.previous_versions = src_v1.all_versions
    src_v2.categorize_as(SnapshotChangeCategory.METADATA)

    mv_v2 = _make_mv(
        make_snapshot,
        query="SELECT a FROM db.src",
        nodes={src_v2.name: src_v2.model},
        category=SnapshotChangeCategory.METADATA,
        previous_versions=mv_v1.all_versions,
    )
    assert mv_v2.requires_schema_migration_in_prod

    stages = _build_prod_plan_for_mv(
        snapshots=[src_v2, mv_v2],
        new_snapshots=[src_v2, mv_v2],
        existing_environment=_make_prod_env([src_v1, mv_v1]),
        stored_snapshots={src_v1.snapshot_id: src_v1, mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )

    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [src_v2.snapshot_id]
    assert mv_v2.snapshot_id not in {s.snapshot_id for s in migrate_stages[0].snapshots}


@pytest.mark.parametrize(
    "ddl_update",
    [
        {"physical_properties": {"storage_tier": parse_one("'hot'")}},
        {"clustered_by": ["a"]},
        {"partitioned_by": ["ds"]},
    ],
)
def test_build_plan_stages_mv_non_sql_physical_ddl_change_migrates(
    make_snapshot, mocker: MockerFixture, ddl_update: t.Dict[str, t.Any]
) -> None:
    mv_v1 = _make_mv(
        make_snapshot,
        query="SELECT 1 AS a, '2023-01-01' AS ds",
        unpaused=True,
    )
    mv_v2 = _make_mv(
        make_snapshot,
        query="SELECT 1 AS a, '2023-01-01' AS ds",
        category=SnapshotChangeCategory.NON_BREAKING,
        forward_only=True,
        previous_versions=mv_v1.all_versions,
        **ddl_update,
    )
    assert mv_v2.table_name(is_deployable=True) == mv_v1.table_name(is_deployable=True)

    stages = _build_prod_plan_for_mv(
        snapshots=[mv_v2],
        new_snapshots=[mv_v2],
        existing_environment=_make_prod_env([mv_v1]),
        stored_snapshots={mv_v1.snapshot_id: mv_v1},
        mocker=mocker,
    )

    migrate_stages = [s for s in stages if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [mv_v2.snapshot_id]
    assert migrate_stages[0].grants_only_snapshot_ids == set()
    assert migrate_stages[0].create_only_snapshot_ids == set()


def test_build_plan_stages_mv_transitive_embedded_parent(
    make_snapshot, mocker: MockerFixture
) -> None:
    base_v1 = make_snapshot(
        SqlModel(
            name="db.base_tbl",
            query=parse_one("SELECT 1 AS a"),
            kind=dict(name=ModelKindName.FULL),
        )
    )
    base_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    base_v1.version = "v1"
    base_v1.unpaused_ts = to_timestamp("2023-01-02")

    embedded_v1 = make_snapshot(
        SqlModel(
            name="db.embedded",
            query=parse_one("SELECT a FROM db.base_tbl"),
            kind=dict(name=ModelKindName.EMBEDDED),
        ),
        nodes={base_v1.name: base_v1.model},
    )
    embedded_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    embedded_v1.unpaused_ts = to_timestamp("2023-01-02")

    mv_v1 = _make_mv(
        make_snapshot,
        query="SELECT a FROM db.embedded",
        nodes={base_v1.name: base_v1.model, embedded_v1.name: embedded_v1.model},
        unpaused=True,
    )

    existing_prod = _make_prod_env([base_v1, embedded_v1, mv_v1])

    # Case (a): Base table unchanged, MV metadata-only change -> MV is NOT recreated
    mv_meta = make_snapshot(
        mv_v1.model.copy(update={"owner": "alice"}),
        nodes={base_v1.name: base_v1.model, embedded_v1.name: embedded_v1.model},
    )
    mv_meta.previous_versions = mv_v1.all_versions
    mv_meta.categorize_as(SnapshotChangeCategory.METADATA)

    stages_unchanged = _build_prod_plan_for_mv(
        snapshots=[base_v1, embedded_v1, mv_meta],
        new_snapshots=[mv_meta],
        existing_environment=existing_prod,
        stored_snapshots={
            base_v1.snapshot_id: base_v1,
            embedded_v1.snapshot_id: embedded_v1,
            mv_v1.snapshot_id: mv_v1,
        },
        mocker=mocker,
    )
    assert not any(isinstance(s, MigrateSchemasStage) for s in stages_unchanged)

    # Case (b): Base table deployable table_name changed (same data_hash, different version)
    # -> transitive check through EMBEDDED detects table_name change and migrates MV
    base_v2 = make_snapshot(base_v1.model.copy(update={"owner": "bob"}))
    base_v2.previous_versions = base_v1.all_versions
    base_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    base_v2.version = "v2"
    assert base_v2.fingerprint.data_hash == base_v1.fingerprint.data_hash
    assert base_v2.table_name(is_deployable=True) != base_v1.table_name(is_deployable=True)

    embedded_v2 = make_snapshot(
        embedded_v1.model,
        nodes={base_v2.name: base_v2.model},
    )
    embedded_v2.previous_versions = embedded_v1.all_versions
    embedded_v2.categorize_as(SnapshotChangeCategory.METADATA)

    mv_v2 = _make_mv(
        make_snapshot,
        query="SELECT a FROM db.embedded",
        nodes={base_v2.name: base_v2.model, embedded_v2.name: embedded_v2.model},
        category=SnapshotChangeCategory.METADATA,
        previous_versions=mv_v1.all_versions,
    )
    assert mv_v2.fingerprint.parent_data_hash == mv_v1.fingerprint.parent_data_hash
    assert mv_v2.table_name(is_deployable=True) == mv_v1.table_name(is_deployable=True)

    stages_changed = _build_prod_plan_for_mv(
        snapshots=[base_v2, embedded_v2, mv_v2],
        new_snapshots=[base_v2, embedded_v2, mv_v2],
        existing_environment=existing_prod,
        stored_snapshots={
            base_v1.snapshot_id: base_v1,
            embedded_v1.snapshot_id: embedded_v1,
            mv_v1.snapshot_id: mv_v1,
        },
        mocker=mocker,
    )
    migrate_stages = [s for s in stages_changed if isinstance(s, MigrateSchemasStage)]
    assert len(migrate_stages) == 1
    assert [s.snapshot_id for s in migrate_stages[0].snapshots] == [
        base_v2.snapshot_id,
        mv_v2.snapshot_id,
    ]
    assert migrate_stages[0].grants_only_snapshot_ids == set()
    assert migrate_stages[0].create_only_snapshot_ids == set()


def test_plan_explainer_migrate_schemas_stage_grants_and_create_only(
    make_snapshot,
) -> None:
    from rich.console import Console as RichConsole
    from sqlmesh.core.environment import EnvironmentNamingInfo
    from sqlmesh.core.plan.explainer import RichExplainerConsole

    mv_grants = _make_mv(make_snapshot, name="db.mv_grants")
    mv_create_only = _make_mv(make_snapshot, name="db.mv_create_only")
    tbl_migrated = make_snapshot(
        SqlModel(
            name="db.tbl_migrated",
            query=parse_one("SELECT 1 AS a, '2023-01-01' AS ds"),
            kind=dict(
                name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", forward_only=True
            ),
        )
    )
    tbl_migrated.categorize_as(SnapshotChangeCategory.BREAKING)

    explainer = RichExplainerConsole(
        environment_naming_info=EnvironmentNamingInfo(name="prod"),
        dialect="duckdb",
        default_catalog=None,
        console=RichConsole(record=True),
    )

    # 1. Grants-only stage: header already says "Sync grants", child omits redundant suffix
    grants_stage = MigrateSchemasStage(
        snapshots=[mv_grants],
        all_snapshots={mv_grants.snapshot_id: mv_grants},
        deployability_index=DeployabilityIndex.all_deployable(),
        grants_only_snapshot_ids={mv_grants.snapshot_id},
    )
    grants_tree = explainer.visit_migrate_schemas_stage(grants_stage)
    assert grants_tree is not None
    assert "Sync grants" in str(grants_tree.label)
    assert len(grants_tree.children) == 1
    assert "mv_grants" in str(grants_tree.children[0].label)
    assert "(sync grants)" not in str(grants_tree.children[0].label)

    # 2. Mixed stage: regular migration + grants-only + create-only (create-only hidden)
    mixed_stage = MigrateSchemasStage(
        snapshots=[mv_create_only, mv_grants, tbl_migrated],
        all_snapshots={s.snapshot_id: s for s in (mv_create_only, mv_grants, tbl_migrated)},
        deployability_index=DeployabilityIndex.all_deployable(),
        grants_only_snapshot_ids={mv_grants.snapshot_id},
        create_only_snapshot_ids={mv_create_only.snapshot_id},
    )
    mixed_tree = explainer.visit_migrate_schemas_stage(mixed_stage)
    assert mixed_tree is not None
    assert "Update schemas" in str(mixed_tree.label)
    child_labels = [str(c.label) for c in mixed_tree.children]
    assert len(child_labels) == 2
    assert any("mv_grants" in lbl and "(sync grants)" in lbl for lbl in child_labels)
    assert any("tbl_migrated" in lbl and "(sync grants)" not in lbl for lbl in child_labels)
    assert not any("mv_create_only" in lbl for lbl in child_labels)

    # 3. Create-only stage returns None
    create_only_stage = MigrateSchemasStage(
        snapshots=[mv_create_only],
        all_snapshots={mv_create_only.snapshot_id: mv_create_only},
        deployability_index=DeployabilityIndex.all_deployable(),
        create_only_snapshot_ids={mv_create_only.snapshot_id},
    )
    assert explainer.visit_migrate_schemas_stage(create_only_stage) is None
