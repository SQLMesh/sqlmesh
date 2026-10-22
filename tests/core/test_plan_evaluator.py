from datetime import date
from pathlib import Path

import pytest
import time_machine
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


@pytest.mark.slow
@time_machine.travel("2026-08-06 01:00:00 UTC", tick=False)
def test_builtin_evaluator_catches_up_no_gaps_plan_to_live_prod_frontier(
    tmp_path: Path,
) -> None:
    def project_for(value: int) -> Path:
        project_path = tmp_path / f"project_{value}"
        models_path = project_path / "models"
        models_path.mkdir(parents=True)
        (project_path / "config.yaml").write_text(
            f"""
default_gateway: local
gateways:
  local:
    connection:
      type: duckdb
      database: {tmp_path / "warehouse.db"}
model_defaults:
  dialect: duckdb
"""
        )
        (models_path / "daily.sql").write_text(
            f"""
MODEL (
  name repro.daily,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column ds,
    lookback 1,
    batch_size 1
  ),
  cron '@daily',
  start '2026-08-04'
);

SELECT
  @start_date AS ds,
  {value} AS value
;
"""
        )
        return project_path

    initial_project = project_for(1)
    changed_project = project_for(2)

    initial_context = Context(paths=[initial_project])
    initial_plan = initial_context.plan_builder(
        "prod",
        no_gaps=True,
        skip_tests=True,
        skip_linter=True,
    ).build()
    initial_context.apply(initial_plan)
    initial_context.close()

    context = Context(paths=[changed_project])
    try:
        stale_plan = context.plan_builder(
            "prod",
            no_gaps=True,
            skip_tests=True,
            skip_linter=True,
        ).build()

        with time_machine.travel("2026-08-07 01:00:00 UTC", tick=False):
            context.run("prod")
            assert context.engine_adapter.fetchall(
                'SELECT ds, value FROM "warehouse"."repro"."daily" ORDER BY ds'
            ) == [
                (date(2026, 8, 4), 1),
                (date(2026, 8, 5), 1),
                (date(2026, 8, 6), 1),
            ]
            context.apply(stale_plan)

        prod_environment = context.state_sync.get_environment("prod")
        assert prod_environment
        assert prod_environment.plan_id == stale_plan.plan_id
        assert context.engine_adapter.fetchall(
            'SELECT ds, value FROM "warehouse"."repro"."daily" ORDER BY ds'
        ) == [
            (date(2026, 8, 4), 2),
            (date(2026, 8, 5), 2),
            (date(2026, 8, 6), 2),
        ]
    finally:
        context.close()
