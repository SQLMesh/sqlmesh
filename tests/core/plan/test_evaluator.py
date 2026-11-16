import json
import typing as t
import pytest
from unittest.mock import MagicMock
from sqlglot import parse_one
from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import FullKind, SqlModel, IncrementalByTimeRangeKind
from sqlmesh.core.plan import EvaluatablePlan, PlanBuilder, stages as plan_stages
from sqlmesh.core.plan.common import identify_restatement_intervals_across_snapshot_versions
from sqlmesh.core.plan.evaluator import BuiltInPlanEvaluator
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import ConflictingPlanError


def _make_evaluatable_plan(
    environment_name: str = "prod",
    restatements: t.Optional[dict] = None,
    restate_all_snapshots: bool = True,
    previous_finalized_snapshots: t.Optional[list] = None,
    ensure_finalized_snapshots: bool = False,
    previous_plan_id: t.Optional[str] = None,
    previous_environment_snapshots: t.Optional[dict] = None,
) -> EvaluatablePlan:
    env = Environment(
        name=environment_name,
        snapshots=[],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="plan_2",
        previous_plan_id=previous_plan_id,
        previous_finalized_snapshots=previous_finalized_snapshots,
    )
    plan = MagicMock(spec=EvaluatablePlan)
    plan.environment = env
    plan.is_dev = False
    plan.is_prod = True
    plan.ensure_finalized_snapshots = ensure_finalized_snapshots
    plan.previous_environment_snapshots = previous_environment_snapshots
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


@pytest.mark.parametrize("ensure_finalized_snapshots", [False, True])
def test_restatement_stage_unfinalized_prod_recovery(
    make_snapshot, ensure_finalized_snapshots: bool
):
    model_a_old = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-02",
    )
    model_a_new = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-01",
    )
    model_b_v1 = SqlModel(
        name="b",
        query=parse_one("SELECT 1 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_v2 = SqlModel(
        name="b",
        query=parse_one("SELECT 2 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_dev = SqlModel(
        name="b",
        query=parse_one("SELECT 99 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )

    snap_a_old = make_snapshot(model_a_old)
    snap_a_old.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_a_old.add_interval("2023-01-02", "2023-01-03")

    snap_a_new = make_snapshot(model_a_new)
    snap_a_new.previous_versions = snap_a_old.all_versions

    snap_b_v1 = make_snapshot(model_b_v1, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v1.add_interval("2020-01-01", "2023-01-03")

    snap_b_v2 = make_snapshot(model_b_v2, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2.add_interval("2020-01-01", "2023-01-03")

    baseline_b = snap_b_v1 if ensure_finalized_snapshots else snap_b_v2
    snap_b_next = make_snapshot(baseline_b.model, nodes={snap_a_new.name: snap_a_new.model})
    snap_b_next.previous_versions = baseline_b.all_versions

    snap_b_dev = make_snapshot(model_b_dev, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev.add_interval("2020-01-01", "2023-01-03")

    # prod is unfinalized: plan_1 promoted b_v2 and failed before FinalizeEnvironmentStage,
    # leaving previous_finalized_snapshots = [a_old, b_v1] and finalized_ts = None.
    deployed_env = Environment(
        name="prod",
        snapshots=[snap_a_old.table_info, snap_b_v2.table_info],
        previous_finalized_snapshots=[snap_a_old.table_info, snap_b_v1.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="plan_1",
        finalized_ts=None,
    )
    dev_env = Environment(
        name="dev",
        snapshots=[snap_a_old.table_info, snap_b_dev.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="dev_plan",
        finalized_ts=100,
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
        modified_snapshots={
            snap_a_new.name: (snap_a_new, snap_a_old),
            snap_b_next.name: (snap_b_next, baseline_b),
        },
        snapshots={
            snap_a_new.snapshot_id: snap_a_new,
            snap_b_next.snapshot_id: snap_b_next,
        },
        new_snapshots={
            snap_a_new.snapshot_id: snap_a_new,
            snap_b_next.snapshot_id: snap_b_next,
        },
        previous_plan_id="plan_1",
        previously_promoted_snapshot_ids={snap_a_old.snapshot_id, snap_b_v2.snapshot_id},
        previous_finalized_snapshots=[snap_a_old.table_info, snap_b_v1.table_info],
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(
        context_diff,
        execution_time="2023-01-04 12:00:00 UTC",
        ensure_finalized_snapshots=ensure_finalized_snapshots,
    ).build()

    # Environment.previous_finalized_snapshots must keep its exact state-sync semantics ([b_v1]).
    assert plan.environment.previous_finalized_snapshots is not None
    prev_finalized_by_name = {s.name: s for s in plan.environment.previous_finalized_snapshots}
    assert prev_finalized_by_name[snap_b_v1.name].version == snap_b_v1.version

    evaluatable_plan = plan.to_evaluatable()
    assert evaluatable_plan.previous_environment_snapshots is not None
    assert (
        evaluatable_plan.previous_environment_snapshots[baseline_b.name].version
        == baseline_b.version
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_env if name == "prod" else dev_env
    )
    state_sync.get_environments_summary.return_value = [deployed_env.summary, dev_env.summary]
    state_sync.get_snapshots_by_names.return_value = []
    state_sync.get_snapshots.return_value = {}

    builder = plan_stages.PlanStagesBuilder(state_sync, default_catalog=None)
    stage = builder._get_restatement_stage(
        evaluatable_plan,
        {snap_a_new.name: snap_a_new, snap_b_next.name: snap_b_next},
        deployed_env,
    )
    assert stage is not None
    assert stage.expected_previous_snapshots is not None
    assert stage.expected_previous_snapshots[baseline_b.name].version == baseline_b.version

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    # Recovery must not self-deadlock with a false ConflictingPlanError.
    evaluator.visit_restatement_stage(stage, evaluatable_plan)
    state_sync.remove_intervals.assert_called_once()


def test_restatement_stage_in_flight_plan_finalizes_concurrently_under_ensure_finalized(
    make_snapshot,
):
    # At t=0, prod was unfinalized under plan_1 (snapshots=[b_v2], previous_finalized_snapshots=[b_v1]).
    # plan_2 was created with ensure_finalized_snapshots=True (baseline b_v1, previous_plan_id="plan_1").
    # Before visit_restatement_stage runs at t=2, plan_1 finishes FinalizeEnvironmentStage (finalized_ts set).
    snap_b_v1 = make_snapshot(
        SqlModel(
            name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2 = make_snapshot(
        SqlModel(
            name="b", query="SELECT 2 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(
        SqlModel(
            name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b_v1.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_finalized_snapshots=[snap_b_v1.table_info],
        ensure_finalized_snapshots=True,
        previous_plan_id="plan_1",
        previous_environment_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )
    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b_v1.name: snap_b_v1},
        expected_previous_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )

    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_v2.table_info],
        previous_finalized_snapshots=[snap_b_v1.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="plan_1",
        finalized_ts=200,  # plan_1 finalized concurrently!
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
    state_sync.get_snapshots_by_names.return_value = []

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    with pytest.raises(ConflictingPlanError, match="Another plan.*deployed new versions"):
        evaluator.visit_restatement_stage(stage, evaluatable_plan)
    state_sync.remove_intervals.assert_not_called()


@pytest.mark.parametrize("ensure_finalized_snapshots", [False, True])
@pytest.mark.parametrize("plan_3_finalized_ts", [None, 300])
def test_restatement_stage_concurrent_plan_3_detected_even_when_unfinalized(
    make_snapshot,
    ensure_finalized_snapshots: bool,
    plan_3_finalized_ts: t.Optional[int],
):
    snap_b_v1 = make_snapshot(
        SqlModel(
            name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v3 = make_snapshot(
        SqlModel(
            name="b", query="SELECT 3 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_v3.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(
        SqlModel(
            name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b_v1.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_finalized_snapshots=[snap_b_v1.table_info],
        ensure_finalized_snapshots=ensure_finalized_snapshots,
        previous_plan_id="plan_1",
        previous_environment_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )
    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b_v1.name: snap_b_v1},
        expected_previous_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )

    # Concurrent plan_3 promoted b_v3 (either finalized or crashed unfinalized with previous_finalized_snapshots=[b_v1]).
    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_v3.table_info],
        previous_finalized_snapshots=[snap_b_v1.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="plan_3",
        finalized_ts=plan_3_finalized_ts,
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
    state_sync.get_snapshots_by_names.return_value = []

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )

    with pytest.raises(ConflictingPlanError, match="Another plan.*deployed new versions"):
        evaluator.visit_restatement_stage(stage, evaluatable_plan)
    state_sync.remove_intervals.assert_not_called()


def test_restatement_stage_new_environment_with_create_from_dev(make_snapshot):
    # First-ever prod plan initialized with create_from="dev" (where dev already had a and b).
    model_a_old = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-02",
    )
    model_a_new = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-01",
    )
    model_b = SqlModel(
        name="b",
        query=parse_one("SELECT 1 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_dev_other = SqlModel(
        name="b",
        query=parse_one("SELECT 99 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )

    snap_a_old = make_snapshot(model_a_old)
    snap_a_old.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_a_old.add_interval("2023-01-02", "2023-01-03")
    snap_a_new = make_snapshot(model_a_new)
    snap_a_new.previous_versions = snap_a_old.all_versions

    snap_b_old = make_snapshot(model_b, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_old.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_old.add_interval("2020-01-01", "2023-01-03")
    snap_b_new = make_snapshot(model_b, nodes={snap_a_new.name: snap_a_new.model})
    snap_b_new.previous_versions = snap_b_old.all_versions

    snap_b_dev_other = make_snapshot(model_b_dev_other, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_dev_other.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev_other.add_interval("2020-01-01", "2023-01-03")

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="dev",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snap_a_new.name: (snap_a_new, snap_a_old),
            snap_b_new.name: (snap_b_new, snap_b_old),
        },
        snapshots={
            snap_a_new.snapshot_id: snap_a_new,
            snap_b_new.snapshot_id: snap_b_new,
        },
        new_snapshots={
            snap_a_new.snapshot_id: snap_a_new,
            snap_b_new.snapshot_id: snap_b_new,
        },
        previous_plan_id=None,
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=[snap_a_old.table_info, snap_b_old.table_info],
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(context_diff, execution_time="2023-01-04 12:00:00 UTC").build()
    evaluatable_plan = plan.to_evaluatable()
    # Target environment (prod) did not exist at t=0, even though create_from="dev" had snapshots.
    assert evaluatable_plan.previous_environment_snapshots == {}

    dev_env = Environment(
        name="dev_other",
        snapshots=[snap_b_dev_other.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="dev_plan",
    )

    # 1. Succeeds when prod is still absent (None) or expired at t=2.
    expired_prod = Environment(
        name="prod",
        snapshots=[snap_b_dev_other.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="old_expired_plan",
        expiration_ts=1,
    )
    for live_prod in (None, expired_prod):
        state_sync = MagicMock()
        state_sync.get_environment.side_effect = lambda name, p=live_prod: (
            p if name == "prod" else dev_env
        )
        state_sync.get_environments_summary.return_value = [dev_env.summary]
        state_sync.get_snapshots_by_names.return_value = []

        builder = plan_stages.PlanStagesBuilder(state_sync, default_catalog=None)
        stage = builder._get_restatement_stage(
            evaluatable_plan,
            {snap_a_new.name: snap_a_new, snap_b_new.name: snap_b_new},
            live_prod,
        )
        assert stage is not None
        assert stage.expected_previous_snapshots == {}

        evaluator = BuiltInPlanEvaluator(
            state_sync=state_sync,
            snapshot_evaluator=MagicMock(),
            create_scheduler=MagicMock(),
            default_catalog=None,
            console=MagicMock(),
        )
        evaluator.visit_restatement_stage(stage, evaluatable_plan)
        state_sync.remove_intervals.assert_called_once()

    # 2. Conflicts if another plan concurrently created active prod with b (same or different version).
    for concurrent_b in (snap_b_new, snap_b_dev_other):
        concurrent_prod = Environment(
            name="prod",
            snapshots=[concurrent_b.table_info],
            start_at="2020-01-01",
            end_at="2023-01-03",
            plan_id="concurrent_init_plan",
        )
        state_sync = MagicMock()
        state_sync.get_environment.side_effect = lambda name, p=concurrent_prod: (
            p if name == "prod" else dev_env
        )
        state_sync.get_environments_summary.return_value = [dev_env.summary]
        state_sync.get_snapshots_by_names.return_value = []

        stage = plan_stages.RestatementStage(
            all_snapshots={snap_a_new.name: snap_a_new, snap_b_new.name: snap_b_new},
            expected_previous_snapshots={},
        )
        evaluator = BuiltInPlanEvaluator(
            state_sync=state_sync,
            snapshot_evaluator=MagicMock(),
            create_scheduler=MagicMock(),
            default_catalog=None,
            console=MagicMock(),
        )
        with pytest.raises(ConflictingPlanError, match="Another plan.*deployed new versions"):
            evaluator.visit_restatement_stage(stage, evaluatable_plan)
        state_sync.remove_intervals.assert_not_called()


def test_restatement_stage_concurrent_model_deletion_detected(make_snapshot):
    snap_b = make_snapshot(
        SqlModel(
            name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(
        SqlModel(
            name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_environment_snapshots={snap_b.name: snap_b.table_info},
    )
    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b.name: snap_b},
        expected_previous_snapshots={snap_b.name: snap_b.table_info},
    )
    # Concurrent plan removed b from prod.
    deployed_env = Environment(
        name="prod",
        snapshots=[],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id="delete_plan",
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
    state_sync.get_snapshots_by_names.return_value = []

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )
    with pytest.raises(ConflictingPlanError, match="Another plan.*deployed new versions"):
        evaluator.visit_restatement_stage(stage, evaluatable_plan)
    state_sync.remove_intervals.assert_not_called()


def test_restatement_stage_legacy_evaluatable_plan_without_previous_environment_snapshots(
    make_snapshot,
):
    model_b_v1 = SqlModel(
        name="b",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_v2 = SqlModel(
        name="b",
        query=parse_one("SELECT 2 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_dev = SqlModel(
        name="b",
        query=parse_one("SELECT 99 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )

    snap_b_v1 = make_snapshot(model_b_v1)
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2 = make_snapshot(model_b_v2)
    snap_b_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2.add_interval("2020-01-01", "2023-01-03")
    snap_b_dev = make_snapshot(model_b_dev)
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

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
        snapshots={snap_b_v2.snapshot_id: snap_b_v2},
        new_snapshots={},
        previous_plan_id="plan_1",
        previously_promoted_snapshot_ids={snap_b_v2.snapshot_id},
        previous_finalized_snapshots=[snap_b_v1.table_info],
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )
    plan = PlanBuilder(
        context_diff,
        restate_models={snap_b_v2.name},
        restate_all_snapshots=True,
        start="2023-01-01",
        end="2023-01-01",
        execution_time="2023-01-04 12:00:00 UTC",
    ).build()

    payload = json.loads(plan.to_evaluatable().json())
    payload.pop("previous_environment_snapshots", None)
    legacy_plan = EvaluatablePlan.parse_obj(payload)
    assert legacy_plan.previous_environment_snapshots is None

    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_v2.table_info],
        previous_finalized_snapshots=[snap_b_v1.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="plan_1",
        finalized_ts=None,
    )
    dev_env = Environment(
        name="dev",
        snapshots=[snap_b_dev.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="dev_plan",
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_env if name == "prod" else dev_env
    )
    state_sync.get_environments_summary.return_value = [dev_env.summary]
    state_sync.get_snapshots_by_names.return_value = []

    builder = plan_stages.PlanStagesBuilder(state_sync, default_catalog=None)
    stage = builder._get_restatement_stage(legacy_plan, {snap_b_v2.name: snap_b_v2})
    assert stage is not None
    assert stage.expected_previous_snapshots == {snap_b_v2.name: snap_b_v2.table_info}

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )
    evaluator.visit_restatement_stage(stage, legacy_plan)
    state_sync.remove_intervals.assert_called_once()


def test_identify_restatement_intervals_widens_overlapping_downstream_dev_snapshot(make_snapshot):
    # b is FULL starting at 2023-01-03 (restated for [Jan 3, Jan 5)), while downstream c is
    # INCREMENTAL starting at 2020-01-01 (restated for [Jan 1, Jan 2)).
    # In dev, b shares prod's version while c has a modified c_dev version; c_dev's clear request
    # must widen across both intervals to cover [Jan 1, Jan 5), not stop at [Jan 3, Jan 5).
    model_a = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-01",
    )
    model_b = SqlModel(
        name="b",
        query=parse_one("SELECT id, ds FROM a"),
        kind=FullKind(),
        start="2023-01-03",
    )
    model_c_prod = SqlModel(
        name="c",
        query=parse_one("SELECT id, ds FROM b"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_c_dev = SqlModel(
        name="c",
        query=parse_one("SELECT id, ds, 99 as extra FROM b"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )

    snap_a = make_snapshot(model_a)
    snap_a.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b = make_snapshot(model_b, nodes={snap_a.name: snap_a.model})
    snap_b.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_c_prod = make_snapshot(
        model_c_prod, nodes={snap_a.name: snap_a.model, snap_b.name: snap_b.model}
    )
    snap_c_prod.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_c_dev = make_snapshot(
        model_c_dev, nodes={snap_a.name: snap_a.model, snap_b.name: snap_b.model}
    )
    snap_c_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    dev_env = Environment(
        name="dev",
        snapshots=[snap_a.table_info, snap_b.table_info, snap_c_dev.table_info],
        start_at="2020-01-01",
        end_at="2023-01-04",
        plan_id="dev_plan",
    )

    state_reader = MagicMock()
    state_reader.get_environments_summary.return_value = [dev_env.summary]
    state_reader.get_environment.return_value = dev_env
    state_reader.get_snapshots_by_names.return_value = []

    prod_restatements = {
        snap_b.name: (to_timestamp("2023-01-03"), to_timestamp("2023-01-05")),
        snap_c_prod.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
    }
    cleared = identify_restatement_intervals_across_snapshot_versions(
        state_reader=state_reader,
        prod_restatements=prod_restatements,
        disable_restatement_models=set(),
        loaded_snapshots={
            snap_a.snapshot_id: snap_a,
            snap_b.snapshot_id: snap_b,
            snap_c_prod.snapshot_id: snap_c_prod,
        },
        current_ts=to_timestamp("2023-01-04"),
    )

    assert set(cleared) == {snap_c_dev.snapshot_id}
    assert cleared[snap_c_dev.snapshot_id].interval == (
        to_timestamp("2023-01-01"),
        to_timestamp("2023-01-05"),
    )


@pytest.mark.parametrize("ensure_finalized_snapshots", [False, True])
def test_restatement_stage_recreate_existing_environment_with_create_from_dev(
    make_snapshot, ensure_finalized_snapshots: bool
):
    # When always_recreate_environment=True and create_from="dev" targets an existing non-expired
    # prod environment, ContextDiff sets is_new_environment=True AND previous_plan_id=prod.plan_id,
    # while context_diff.environment_snapshots holds dev's snapshots.
    model_a_old = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-02",
    )
    model_a_new = SqlModel(
        name="a",
        query=parse_one("SELECT 1 as id, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-01",
    )
    model_b_prod = SqlModel(
        name="b",
        query=parse_one("SELECT 1 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_dev = SqlModel(
        name="b",
        query=parse_one("SELECT 2 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )
    model_b_other = SqlModel(
        name="b",
        query=parse_one("SELECT 99 as id, ds FROM a"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2020-01-01",
    )

    snap_a_old = make_snapshot(model_a_old)
    snap_a_old.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_a_old.add_interval("2023-01-02", "2023-01-03")
    snap_a_new = make_snapshot(model_a_new)
    snap_a_new.previous_versions = snap_a_old.all_versions

    snap_b_prod = make_snapshot(model_b_prod, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_prod.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_prod.add_interval("2020-01-01", "2023-01-03")

    snap_b_dev = make_snapshot(model_b_dev, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev.add_interval("2020-01-01", "2023-01-03")

    snap_b_new = make_snapshot(model_b_dev, nodes={snap_a_new.name: snap_a_new.model})
    snap_b_new.previous_versions = snap_b_dev.all_versions

    snap_b_other = make_snapshot(model_b_other, nodes={snap_a_old.name: snap_a_old.model})
    snap_b_other.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff = ContextDiff(
        environment="prod",
        is_new_environment=True,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="dev",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={
            snap_a_new.name: (snap_a_new, snap_a_old),
            snap_b_new.name: (snap_b_new, snap_b_dev),
        },
        snapshots={
            snap_a_new.snapshot_id: snap_a_new,
            snap_b_new.snapshot_id: snap_b_new,
        },
        new_snapshots={
            snap_a_new.snapshot_id: snap_a_new,
            snap_b_new.snapshot_id: snap_b_new,
        },
        previous_plan_id="prod_plan_1",
        previously_promoted_snapshot_ids=set(),
        previous_finalized_snapshots=[snap_a_old.table_info, snap_b_dev.table_info],
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )

    plan = PlanBuilder(
        context_diff,
        execution_time="2023-01-04 12:00:00 UTC",
        ensure_finalized_snapshots=ensure_finalized_snapshots,
    ).build()
    evaluatable_plan = plan.to_evaluatable()
    assert evaluatable_plan.previous_environment_snapshots is None

    deployed_prod = Environment(
        name="prod",
        snapshots=[snap_a_old.table_info, snap_b_prod.table_info],
        previous_finalized_snapshots=[snap_a_old.table_info, snap_b_prod.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="prod_plan_1",
        finalized_ts=100,
    )
    other_dev = Environment(
        name="other_dev",
        snapshots=[snap_b_other.table_info],
        start_at="2020-01-01",
        end_at="2023-01-03",
        plan_id="other_dev_plan",
    )

    state_sync = MagicMock()
    state_sync.get_environment.side_effect = lambda name: (
        deployed_prod if name == "prod" else other_dev
    )
    state_sync.get_environments_summary.return_value = [other_dev.summary]
    state_sync.get_snapshots_by_names.return_value = []

    builder = plan_stages.PlanStagesBuilder(state_sync, default_catalog=None)
    stage = builder._get_restatement_stage(
        evaluatable_plan,
        {snap_a_new.name: snap_a_new, snap_b_new.name: snap_b_new},
        deployed_prod,
    )
    assert stage is not None
    assert stage.expected_previous_snapshots is not None
    assert stage.expected_previous_snapshots[snap_b_prod.name].version == snap_b_prod.version

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )
    evaluator.visit_restatement_stage(stage, evaluatable_plan)
    state_sync.remove_intervals.assert_called_once()


def test_restatement_stage_same_plan_id_retry_is_idempotent(make_snapshot):
    snap_b_v1 = make_snapshot(
        SqlModel(
            name="b", query="SELECT 1 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_v1.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_v2 = make_snapshot(
        SqlModel(
            name="b", query="SELECT 2 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_v2.categorize_as(SnapshotChangeCategory.BREAKING)
    snap_b_dev = make_snapshot(
        SqlModel(
            name="b", query="SELECT 99 as id, ds", kind=IncrementalByTimeRangeKind(time_column="ds")
        )
    )
    snap_b_dev.categorize_as(SnapshotChangeCategory.BREAKING)

    evaluatable_plan = _make_evaluatable_plan(
        restatements={snap_b_v2.name: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02"))},
        previous_environment_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )
    evaluatable_plan.plan_id = evaluatable_plan.environment.plan_id
    stage = plan_stages.RestatementStage(
        all_snapshots={snap_b_v2.name: snap_b_v2},
        expected_previous_snapshots={snap_b_v1.name: snap_b_v1.table_info},
    )

    # Same plan_id ("plan_2") already promoted b_v2; retrying visit_restatement_stage must not conflict with itself.
    deployed_env = Environment(
        name="prod",
        snapshots=[snap_b_v2.table_info],
        start_at="2023-01-01",
        end_at="2023-01-02",
        plan_id=evaluatable_plan.plan_id,
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
    state_sync.get_snapshots_by_names.return_value = []

    evaluator = BuiltInPlanEvaluator(
        state_sync=state_sync,
        snapshot_evaluator=MagicMock(),
        create_scheduler=MagicMock(),
        default_catalog=None,
        console=MagicMock(),
    )
    evaluator.visit_restatement_stage(stage, evaluatable_plan)
    state_sync.remove_intervals.assert_called_once()
