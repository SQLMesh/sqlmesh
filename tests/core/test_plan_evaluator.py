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
from sqlmesh.core.snapshot import SnapshotChangeCategory, SnapshotId
from sqlmesh.utils.concurrency import NodeExecutionFailedError
from sqlmesh.utils.errors import PlanError


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


def test_migrate_schema_failure_includes_node_context(mocker: MockerFixture):
    error = NodeExecutionFailedError(SnapshotId(name="model", identifier="snapshot"))
    error.__cause__ = RuntimeError("driver error")

    snapshot_evaluator = mocker.Mock()
    snapshot_evaluator.migrate.side_effect = error
    evaluator = BuiltInPlanEvaluator(
        state_sync=mocker.Mock(),
        snapshot_evaluator=snapshot_evaluator,
        create_scheduler=mocker.Mock(),
        default_catalog=None,
        console=mocker.Mock(),
    )

    with pytest.raises(PlanError, match="model.*driver error") as ex:
        evaluator.visit_migrate_schemas_stage(mocker.Mock(), mocker.Mock())

    assert ex.value.__cause__ is error
