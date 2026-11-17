# SPDX-License-Identifier: Apache-2.0

from datetime import datetime
from pathlib import Path

import pytest
import time_machine
from sqlglot import parse_one
from pydantic import ValidationError

from sqlmesh.core.context import Context
from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import IncrementalByTimeRangeKind, SqlModel
from sqlmesh.core.plan import BuiltInPlanEvaluator, PlanBuilder
from sqlmesh.core.plan.definition import EvaluatablePlan
from sqlmesh.core.plan.stages import EnvironmentRecordUpdateStage, build_plan_stages
from sqlmesh.core.promotion import PromotionFrontier
from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory
from sqlmesh.utils.date import to_timestamp


def _context_diff(current: Snapshot, previous: Snapshot) -> ContextDiff:
    return ContextDiff(
        environment="prod",
        is_new_environment=False,
        is_unfinalized_environment=False,
        normalize_environment_name=True,
        create_from="prod",
        create_from_env_exists=True,
        added=set(),
        removed_snapshots={},
        modified_snapshots={current.name: (current, previous)},
        snapshots={current.snapshot_id: current},
        new_snapshots={current.snapshot_id: current},
        previous_plan_id="previous-plan",
        previously_promoted_snapshot_ids={previous.snapshot_id},
        previous_finalized_snapshots=None,
        previous_gateway_managed_virtual_layer=False,
        gateway_managed_virtual_layer=False,
        environment_statements=[],
    )


def _write_hourly_model(path: Path, value: int) -> None:
    (path / "models").mkdir(exist_ok=True)
    (path / "config.yaml").write_text("model_defaults:\n  dialect: duckdb\n")
    (path / "models" / "model.sql").write_text(
        f"""
MODEL (
  name test.model,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ts, batch_size 1),
  cron '@hourly',
  start '2024-01-01'
);
SELECT ts, {value} AS value
FROM generate_series(@start_dt, @end_dt, INTERVAL '1 hour') AS t(ts)
"""
    )


def _build_plan_with_choices(builder: PlanBuilder):
    plan = builder.build()
    for snapshot in plan.uncategorized:
        builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
    return builder.build()


def test_planned_frontier_uses_modified_incumbent_identity(make_snapshot):
    previous = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
        version="previous",
    )
    current = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds, 1 as value"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
        version="current",
    )
    previous.categorize_as(SnapshotChangeCategory.BREAKING)
    current.categorize_as(SnapshotChangeCategory.BREAKING)
    context_diff = _context_diff(current, previous)

    frontier_end = datetime(2024, 1, 2)
    frontiers = Context._get_planned_frontiers(context_diff, {current.name: frontier_end})

    assert frontiers == {
        current.name: PromotionFrontier(
            snapshot_id=previous.snapshot_id,
            end=to_timestamp(frontier_end),
        )
    }

    no_previous_plan_diff = context_diff.copy(update={"previous_plan_id": None})
    assert Context._get_planned_frontiers(no_previous_plan_diff, {current.name: frontier_end}) == {}


@pytest.mark.slow
def test_context_frontier_is_captured_before_end_and_execution_overrides(tmp_path):
    _write_hourly_model(tmp_path, 1)

    with time_machine.travel("2024-01-01 04:00:00 UTC", tick=False):
        context = Context(paths=tmp_path)
        context.plan(
            "prod",
            no_prompts=True,
            auto_apply=True,
            execution_time="2024-01-01 04:00:00",
        )

        _write_hourly_model(tmp_path, 2)
        context.load()
        execution_time_plan = _build_plan_with_choices(
            context.plan_builder(
                "prod",
                no_gaps=True,
                max_trailing_gap_intervals=1,
                execution_time="2024-01-01 06:00:00",
            )
        )
        assert len(execution_time_plan.planned_frontiers) == 1
        assert next(iter(execution_time_plan.planned_frontiers.values())).end == to_timestamp(
            "2024-01-01 04:00:00"
        )
        assert to_timestamp(execution_time_plan.end) == to_timestamp("2024-01-01 06:00:00")
        assert execution_time_plan.end_override_per_model
        assert all(
            to_timestamp(end) == to_timestamp("2024-01-01 06:00:00")
            for end in execution_time_plan.end_override_per_model.values()
        )

        plan = _build_plan_with_choices(
            context.plan_builder(
                "prod",
                no_gaps=True,
                max_trailing_gap_intervals=1,
                execution_time="2024-01-01 06:00:00",
                restate_models=["test.model"],
                end="2024-01-01 05:00:00",
                always_include_local_changes=True,
            )
        )

        assert plan.max_trailing_gap_intervals == 1
        assert len(plan.planned_frontiers) == 1
        frontier = next(iter(plan.planned_frontiers.values()))
        assert frontier.end == to_timestamp("2024-01-01 04:00:00")
        assert frontier.end < to_timestamp(plan.end)
        # The explicit end is handled by PlanBuilder and intentionally removes per-model
        # backfill caps. The separately captured frontier remains available for promotion.
        assert plan.end_override_per_model is None
        context.close()


@pytest.mark.slow
def test_context_run_frontier_capture_does_not_add_backfill_caps(tmp_path):
    _write_hourly_model(tmp_path, 1)

    with time_machine.travel("2024-01-01 04:00:00 UTC", tick=False):
        context = Context(paths=tmp_path)
        context.plan(
            "prod",
            no_prompts=True,
            auto_apply=True,
            execution_time="2024-01-01 04:00:00",
        )

        _write_hourly_model(tmp_path, 2)
        context.load()
        run_plan = _build_plan_with_choices(
            context.plan_builder(
                "prod",
                no_gaps=True,
                max_trailing_gap_intervals=1,
                run=True,
                execution_time="2024-01-01 06:00:00",
            )
        )
        strict_run_plan = _build_plan_with_choices(
            context.plan_builder(
                "prod",
                no_gaps=True,
                run=True,
                execution_time="2024-01-01 06:00:00",
            )
        )

        assert len(run_plan.planned_frontiers) == 1
        assert next(iter(run_plan.planned_frontiers.values())).end == to_timestamp(
            "2024-01-01 04:00:00"
        )
        assert run_plan.end_override_per_model is None
        assert strict_run_plan.end_override_per_model is None
        assert run_plan.missing_intervals == strict_run_plan.missing_intervals
        context.close()


def test_evaluatable_plan_round_trip_preserves_policy_and_backfill_overrides(make_snapshot):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
        version="current",
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    planned_frontier = PromotionFrontier(snapshot_id=snapshot.snapshot_id, end=123)
    start_override = datetime(2024, 1, 1)
    end_override = datetime(2024, 1, 2)
    plan = EvaluatablePlan(
        start="2024-01-01",
        end="2024-01-03",
        new_snapshots=[snapshot],
        environment=Environment(
            snapshots=[snapshot.table_info],
            start_at="2024-01-01",
            end_at="2024-01-03",
            plan_id="plan",
            previous_plan_id="previous-plan",
        ),
        no_gaps=True,
        skip_backfill=False,
        empty_backfill=False,
        restatements={},
        restate_all_snapshots=False,
        is_dev=False,
        allow_destructive_models=set(),
        allow_additive_models=set(),
        forward_only=False,
        end_bounded=True,
        ensure_finalized_snapshots=False,
        directly_modified_snapshots=[snapshot.snapshot_id],
        indirectly_modified_snapshots={},
        metadata_updated_snapshots=[],
        removed_snapshots=[],
        requires_backfill=True,
        start_override_per_model={snapshot.name: start_override},
        end_override_per_model={snapshot.name: end_override},
        execution_time="2024-01-03",
        disabled_restatement_models=set(),
        max_trailing_gap_intervals=1,
        planned_frontiers={snapshot.name: planned_frontier},
    )

    restored = EvaluatablePlan.parse_raw(plan.json())

    assert restored.max_trailing_gap_intervals == 1
    assert restored.planned_frontiers == {snapshot.name: planned_frontier}
    assert restored.start_override_per_model == {snapshot.name: start_override}
    assert restored.end_override_per_model == {snapshot.name: end_override}


def test_plan_builder_threads_policy_and_frontier(make_snapshot):
    previous = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            start="2024-01-01",
        ),
        version="previous",
    )
    current = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds, 1 as value"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            start="2024-01-01",
        ),
        version="current",
    )
    previous.categorize_as(SnapshotChangeCategory.BREAKING)
    current.categorize_as(SnapshotChangeCategory.BREAKING)
    planned_frontier = PromotionFrontier(snapshot_id=previous.snapshot_id, end=123)

    plan = PlanBuilder(
        _context_diff(current, previous),
        no_gaps=True,
        execution_time="2024-01-03",
        max_trailing_gap_intervals=1,
        planned_frontiers={current.name: planned_frontier},
    ).build()

    assert plan.max_trailing_gap_intervals == 1
    assert plan.planned_frontiers == {current.name: planned_frontier}
    assert plan.to_evaluatable().planned_frontiers == plan.planned_frontiers


def test_plan_evaluator_passes_policy_to_promotion(make_snapshot, mocker):
    previous = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            start="2024-01-01",
        ),
        version="previous",
    )
    current = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select ds, 1 as value"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
            start="2024-01-01",
        ),
        version="current",
    )
    previous.categorize_as(SnapshotChangeCategory.BREAKING)
    current.categorize_as(SnapshotChangeCategory.BREAKING)
    planned_frontier = PromotionFrontier(snapshot_id=previous.snapshot_id, end=123)
    plan = PlanBuilder(
        _context_diff(current, previous),
        no_gaps=True,
        execution_time="2024-01-03",
        max_trailing_gap_intervals=1,
        planned_frontiers={current.name: planned_frontier},
    ).build()

    state_reader = mocker.Mock()
    state_reader.get_snapshots.return_value = {}
    state_reader.get_environment.return_value = None
    evaluatable_plan = plan.to_evaluatable()
    stages = build_plan_stages(evaluatable_plan, state_reader, None)
    environment_stage = next(
        stage for stage in stages if isinstance(stage, EnvironmentRecordUpdateStage)
    )
    state_sync = mocker.Mock()
    evaluator = BuiltInPlanEvaluator(
        state_sync,
        mocker.Mock(),
        mocker.Mock(),
        None,
    )

    evaluator.visit_environment_record_update_stage(environment_stage, evaluatable_plan)

    _, kwargs = state_sync.promote.call_args
    assert kwargs["max_trailing_gap_intervals"] == 1
    assert kwargs["planned_frontiers"] == {current.name: planned_frontier}


@pytest.mark.parametrize("invalid", [-1, True, 1.0, "1"])
def test_max_trailing_gap_intervals_must_be_nonnegative(make_snapshot, invalid):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    environment = Environment(
        snapshots=[snapshot.table_info],
        start_at="2024-01-01",
        end_at="2024-01-03",
        plan_id="plan",
    )

    with pytest.raises(ValidationError):
        EvaluatablePlan(
            start="2024-01-01",
            end="2024-01-03",
            new_snapshots=[snapshot],
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
            directly_modified_snapshots=[],
            indirectly_modified_snapshots={},
            metadata_updated_snapshots=[],
            removed_snapshots=[],
            requires_backfill=False,
            disabled_restatement_models=set(),
            max_trailing_gap_intervals=invalid,
        )
