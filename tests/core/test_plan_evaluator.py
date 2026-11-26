import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import parse_one

from sqlmesh.core.context import Context
from sqlmesh.core.model import FullKind, SqlModel, ViewKind
from sqlmesh.core.plan import (
    BuiltInPlanEvaluator,
    Plan,
    PlanBuilder,
    stages as plan_stages,
)
from sqlmesh.core.snapshot import SnapshotChangeCategory


@pytest.fixture
def sushi_plan(sushi_context: Context, mocker: MockerFixture) -> Plan:
    mock_prompt = mocker.Mock()
    mock_prompt.ask.return_value = "2022-01-01"
    mocker.patch("sqlmesh.core.console.Prompt", mock_prompt)

    return PlanBuilder(
        sushi_context._context_diff("dev"),
        is_dev=True,
        include_unmodified=True,
    ).build()


@pytest.mark.slow
def test_builtin_evaluator_push(sushi_context: Context, make_snapshot):
    new_model = SqlModel(
        name="sushi.new_test_model",
        kind=FullKind(),
        owner="jen",
        cron="@daily",
        start="2020-01-01",
        query=parse_one("SELECT 1::INT AS one"),
        default_catalog="memory",
    )
    new_view_model = SqlModel(
        name="sushi.new_test_view_model",
        kind=ViewKind(),
        owner="jen",
        start="2020-01-01",
        query=parse_one("SELECT 1::INT AS one FROM sushi.new_test_model, sushi.waiters"),
        default_catalog="memory",
    )

    sushi_context.upsert_model(new_model)
    sushi_context.upsert_model(new_view_model)

    new_model_snapshot = sushi_context.get_snapshot(new_model, raise_if_missing=True)
    new_view_model_snapshot = sushi_context.get_snapshot(new_view_model, raise_if_missing=True)

    new_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    new_view_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    plan = PlanBuilder(sushi_context._context_diff("prod")).build()

    evaluator = BuiltInPlanEvaluator(
        sushi_context.state_sync,
        sushi_context.snapshot_evaluator,
        sushi_context.create_scheduler,
        sushi_context.default_catalog,
        console=sushi_context.console,
    )

    evaluatable_plan = plan.to_evaluatable()
    stages = plan_stages.build_plan_stages(
        evaluatable_plan, sushi_context.state_sync, sushi_context.default_catalog
    )
    assert isinstance(stages[1], plan_stages.CreateSnapshotRecordsStage)
    evaluator.visit_create_snapshot_records_stage(stages[1], evaluatable_plan)
    assert isinstance(stages[2], plan_stages.PhysicalLayerSchemaCreationStage)
    evaluator.visit_physical_layer_schema_creation_stage(stages[2], evaluatable_plan)
    assert isinstance(stages[3], plan_stages.BackfillStage)
    evaluator.visit_backfill_stage(stages[3], evaluatable_plan)

    assert (
        len(sushi_context.state_sync.get_snapshots([new_model_snapshot, new_view_model_snapshot]))
        == 2
    )
    assert sushi_context.engine_adapter.table_exists(new_model_snapshot.table_name())
    assert sushi_context.engine_adapter.table_exists(new_view_model_snapshot.table_name())


def test_visit_audit_only_run_stage(make_snapshot, mocker: MockerFixture):
    from datetime import datetime
    from sqlmesh.core.environment import Environment
    from sqlmesh.core.plan.definition import EvaluatablePlan
    from sqlmesh.core.snapshot import DeployabilityIndex
    from sqlmesh.utils import CompletionStatus
    from sqlmesh.utils.errors import PlanError

    gp_model = SqlModel(name="db.gp", kind=FullKind(), query=parse_one("SELECT 1 AS id"))
    parent_model = SqlModel(
        name="db.parent", kind=FullKind(), query=parse_one("SELECT id FROM db.gp")
    )
    child_model = SqlModel(
        name="db.child",
        kind=FullKind(),
        query=parse_one("SELECT id FROM db.parent"),
        audits=[("not_null", {})],
    )
    sibling_model = SqlModel(name="db.sibling", kind=FullKind(), query=parse_one("SELECT 1 AS id"))

    gp_snapshot = make_snapshot(gp_model)
    gp_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    gp_snapshot.add_interval("2024-01-01", "2024-01-02")

    parent_snapshot = make_snapshot(parent_model, nodes={gp_model.fqn: gp_model})
    parent_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    parent_snapshot.add_interval("2024-01-01", "2024-01-02")

    child_snapshot = make_snapshot(
        child_model, nodes={gp_model.fqn: gp_model, parent_model.fqn: parent_model}
    )
    child_snapshot.categorize_as(SnapshotChangeCategory.METADATA)
    child_snapshot.add_interval("2024-01-01", "2024-01-02")
    child_snapshot.add_interval("2024-01-01", "2024-01-02", is_dev=True)

    sibling_snapshot = make_snapshot(sibling_model)
    sibling_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    sibling_snapshot.add_interval("2024-01-01", "2024-01-02")

    original_child_intervals = child_snapshot.intervals.copy()
    original_child_dev_intervals = child_snapshot.dev_intervals.copy()
    original_parent_intervals = parent_snapshot.intervals.copy()

    all_snapshots = {
        gp_snapshot.name: gp_snapshot,
        parent_snapshot.name: parent_snapshot,
        child_snapshot.name: child_snapshot,
        sibling_snapshot.name: sibling_snapshot,
    }
    deployability_index = DeployabilityIndex.create(list(all_snapshots.values()))
    stage = plan_stages.AuditOnlyRunStage(
        snapshots=[child_snapshot],
        all_snapshots=all_snapshots,
        deployability_index=deployability_index,
    )

    env = Environment(
        name="dev",
        snapshots=[s.table_info for s in all_snapshots.values()],
        start_at="2024-01-01",
        end_at="2024-01-02",
        plan_id="test_plan",
        previous_plan_id=None,
    )
    start_override = {child_snapshot.name: datetime(2024, 1, 1)}
    end_override = {child_snapshot.name: datetime(2024, 1, 2)}
    plan = EvaluatablePlan(
        start="2024-01-01",
        end="2024-01-02",
        new_snapshots=[child_snapshot],
        environment=env,
        no_gaps=False,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=True,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=True,
        ensure_finalized_snapshots=False,
        ignore_cron=True,
        directly_modified_snapshots=[child_snapshot.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=False,
        models_to_backfill=None,
        execution_time="2024-01-02",
        disabled_restatement_models=set(),
        environment_statements=None,
        user_provided_flags=None,
        start_override_per_model=start_override,
        end_override_per_model=end_override,
    )

    captured_snapshots = []
    mock_scheduler = mocker.Mock()

    def _audit(*args, **kwargs):
        # Mutate intervals on snapshots passed to Scheduler to verify stage.all_snapshots is isolated
        for s in captured_snapshots:
            s.intervals.clear()
            s.dev_intervals.clear()
            s.pending_restatement_intervals.append((1, 2))
        return CompletionStatus.SUCCESS

    mock_scheduler.audit.side_effect = _audit

    def _create_scheduler(snaps, _snapshot_evaluator):
        captured_snapshots.extend(snaps)
        return mock_scheduler

    circuit_breaker = lambda: False
    evaluator = BuiltInPlanEvaluator(
        state_sync=mocker.Mock(),
        snapshot_evaluator=mocker.Mock(),
        create_scheduler=_create_scheduler,
        default_catalog=None,
    )
    evaluator._circuit_breaker = circuit_breaker

    evaluator.visit_audit_only_run_stage(stage, plan)

    # Only audited snapshot + upstream ancestors are passed (unrelated sibling excluded)
    assert {s.name for s in captured_snapshots} == {
        gp_snapshot.name,
        parent_snapshot.name,
        child_snapshot.name,
    }
    assert sibling_snapshot.name not in {s.name for s in captured_snapshots}

    # Original snapshots in stage.all_snapshots must not be mutated
    assert child_snapshot.intervals == original_child_intervals
    assert child_snapshot.dev_intervals == original_child_dev_intervals
    assert child_snapshot.pending_restatement_intervals == []
    assert parent_snapshot.intervals == original_parent_intervals

    mock_scheduler.audit.assert_called_once_with(
        plan.environment,
        plan.start,
        plan.end,
        execution_time=plan.execution_time,
        end_bounded=True,
        ignore_cron=True,
        start_override_per_model=start_override,
        end_override_per_model=end_override,
        selected_snapshots={child_snapshot.name},
        deployability_index=deployability_index,
        circuit_breaker=circuit_breaker,
    )

    # Failure status raises PlanError
    mock_scheduler.audit.side_effect = None
    mock_scheduler.audit.return_value = CompletionStatus.FAILURE
    with pytest.raises(PlanError):
        evaluator.visit_audit_only_run_stage(stage, plan)
