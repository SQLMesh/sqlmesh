import typing as t
import pytest
from unittest.mock import MagicMock
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import SqlModel, IncrementalByTimeRangeKind
from sqlmesh.core.plan import EvaluatablePlan, stages as plan_stages
from sqlmesh.core.plan.evaluator import BuiltInPlanEvaluator
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import ConflictingPlanError


def _make_evaluatable_plan(
    environment_name: str = "prod",
    restatements: t.Optional[dict] = None,
    restate_all_snapshots: bool = True,
    previous_finalized_snapshots: t.Optional[list] = None,
) -> EvaluatablePlan:
    env = Environment(
        name=environment_name,
        snapshots=[],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="plan_1",
        previous_finalized_snapshots=previous_finalized_snapshots,
    )
    plan = MagicMock(spec=EvaluatablePlan)
    plan.environment = env
    plan.is_dev = False
    plan.is_prod = True
    plan.restate_all_snapshots = restate_all_snapshots
    plan.restatements = restatements or {}
    plan.disabled_restatement_models = set()
    plan.execution_time = to_timestamp("2023-01-02")
    return plan


def test_restatement_stage_expected_previous_snapshots_allows_promoted_model(make_snapshot):
    # Model b was modified and promoted (b_v1 -> b_v2) in the current plan while a extended start
    model_b_old = SqlModel(
        name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_new = SqlModel(
        name="b", query="SELECT 2 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_dev = SqlModel(
        name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )

    snap_b_old = make_snapshot(model_b_old)
    snap_b_old.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_new = make_snapshot(model_b_new)
    snap_b_new.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_new.previous_versions = snap_b_old.all_versions
    snap_b_dev = make_snapshot(model_b_dev)
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    assert snap_b_old.version != snap_b_new.version

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b_new.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_finalized_snapshots=[snap_b_old.table_info],
    )

    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b_new.name: snap_b_new},
        expected_previous_snapshots={snap_b_old.name: snap_b_old.table_info},
    )

    # In prod, the active snapshot is still b_v1 prior to promotion
    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_old.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="prev_plan",
    )
    dev_env = Environment(
        name="dev",
        snapshots=[snap_b_dev.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="dev_plan",
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_env if name == "prod" else dev_env
    )
    state_sync.get_environments_summary.return_value = [dev_env.summary]

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    # Should not raise ConflictingPlanError because prod version matches expected_previous_snapshots
    evaluator.visit_restatement_stage(stage, evaluatable_plan)


def test_restatement_stage_expected_previous_snapshots_detects_concurrent_deploy(make_snapshot):
    # Model b was promoted (b_v1 -> b_v2), but someone concurrently deployed b_v3 to prod
    model_b_v1 = SqlModel(
        name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_v2 = SqlModel(
        name="b", query="SELECT 2 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_v3 = SqlModel(
        name="b", query="SELECT 3 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_dev = SqlModel(
        name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )

    snap_b_v1 = make_snapshot(model_b_v1)
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2 = make_snapshot(model_b_v2)
    snap_b_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v3 = make_snapshot(model_b_v3)
    snap_b_v3.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(model_b_dev)
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b_v2.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_finalized_snapshots=[snap_b_v1.table_info],
    )

    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b_v2.name: snap_b_v2},
        expected_previous_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )

    # Prod has b_v3 deployed concurrently!
    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_v3.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="concurrent_plan",
    )
    dev_env = Environment(
        name="dev",
        snapshots=[snap_b_dev.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="dev_plan",
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_env if name == "prod" else dev_env
    )
    state_sync.get_environments_summary.return_value = [dev_env.summary]

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    with pytest.raises(ConflictingPlanError, match="Another plan.*deployed new versions"):
        evaluator.visit_restatement_stage(stage, evaluatable_plan)


def test_restatement_stage_expected_previous_snapshots_new_model_not_in_prod(make_snapshot):
    # Model b did not exist in prod when plan was created
    model_b = SqlModel(
        name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_dev = SqlModel(
        name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )

    snap_b = make_snapshot(model_b)
    snap_b.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(model_b_dev)
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_finalized_snapshots=[],
    )

    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b.name: snap_b},
        expected_previous_snapshots={},  # empty because b is new
    )

    # Prod does not have b yet
    deployed_env = Environment(
        name="prod",
        snapshots=[],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="prev_plan",
    )
    dev_env = Environment(
        name="dev",
        snapshots=[snap_b_dev.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="dev_plan",
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_env if name == "prod" else dev_env
    )
    state_sync.get_environments_summary.return_value = [dev_env.summary]

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    # Adding a brand-new model does not raise a conflict
    evaluator.visit_restatement_stage(stage, evaluatable_plan)


def test_restatement_stage_expected_previous_snapshots_new_model_conflicts_if_deployed(
    make_snapshot,
):
    # Model b did not exist when plan was created, but someone deployed another version of b to prod
    model_b_v1 = SqlModel(
        name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_v2 = SqlModel(
        name="b", query="SELECT 2 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    model_b_dev = SqlModel(
        name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )

    snap_b_v1 = make_snapshot(model_b_v1)
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2 = make_snapshot(model_b_v2)
    snap_b_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(model_b_dev)
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b_v1.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_finalized_snapshots=[],
    )

    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b_v1.name: snap_b_v1},
        expected_previous_snapshots={},
    )

    # Prod now has b_v2 deployed
    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_v2.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="other_plan",
    )
    dev_env = Environment(
        name="dev",
        snapshots=[snap_b_dev.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="dev_plan",
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_env if name == "prod" else dev_env
    )
    state_sync.get_environments_summary.return_value = [dev_env.summary]

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    with pytest.raises(ConflictingPlanError, match="Another plan.*deployed new versions"):
        evaluator.visit_restatement_stage(stage, evaluatable_plan)


def test_restatement_stage_populates_expected_previous_snapshots_in_builder(make_snapshot):
    model_b = SqlModel(
        name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
    )
    snap_b = make_snapshot(model_b)
    snap_b.categorize_as(SnapshotChangeCategory.BREAKING)

    env = Environment(
        name="prod",
        snapshots=[snap_b.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="plan_1",
        previous_finalized_snapshots=[snap_b.table_info],
    )
    plan = MagicMock(spec=EvaluatablePlan)
    plan.environment = env
    plan.is_dev = False
    plan.restate_all_snapshots = True
    plan.restatements = {snap_b.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))}

    state_reader = MagicMock()
    builder = plan_stages.PlanStagesBuilder(state_reader, default_catalog=None)
    stage = builder._get_restatement_stage(plan, {snap_b.name: snap_b})

    assert stage is not None
    assert stage.expected_previous_snapshots is not None
    assert snap_b.name in stage.expected_previous_snapshots
    assert stage.expected_previous_snapshots[snap_b.name].version == snap_b.version
