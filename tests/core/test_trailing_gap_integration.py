# SPDX-License-Identifier: Apache-2.0

import pytest
import time_machine

from sqlmesh.core.context import Context
from sqlmesh.core.plan.definition import EvaluatablePlan
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import SQLMeshError


PLAN_TIME = "2024-01-01 04:00:00 UTC"
PROMOTION_TIME = "2024-01-01 05:00:00 UTC"
PLAN_FRONTIER = to_timestamp("2024-01-01 04:00")
OBSERVED_FRONTIER = to_timestamp("2024-01-01 05:00")


def _write_parent(path, value, *, partial_daily=False):
    model_options = (
        """
  kind INCREMENTAL_BY_TIME_RANGE (time_column ts, batch_size 1, lookback 14),
  cron '@hourly',
  allow_partials true,
  interval_unit 'day',
"""
        if partial_daily
        else """
  kind INCREMENTAL_BY_TIME_RANGE (time_column ts, batch_size 1),
  cron '@hourly',
"""
    )
    path.write_text(
        f"""
MODEL (
  name test.parent,
{model_options}
  start '2024-01-01'
);
SELECT ts, {value} AS value
FROM generate_series(@start_dt, @end_dt, INTERVAL '1 hour') AS t(ts)
"""
    )


def _create_project(tmp_path, *, partial_daily=False):
    (tmp_path / "models").mkdir()
    (tmp_path / "config.yaml").write_text("model_defaults:\n  dialect: duckdb\n")
    parent_path = tmp_path / "models" / "parent.sql"
    _write_parent(parent_path, 1, partial_daily=partial_daily)
    (tmp_path / "models" / "child.sql").write_text(
        """
MODEL (
  name test.child,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ts, batch_size 1),
  cron '@hourly',
  start '2024-01-01'
);
SELECT ts, value FROM test.parent WHERE ts >= @start_dt AND ts < @end_dt
"""
    )
    return parent_path


def _add_interval_to_snapshots(context, snapshots, start, end):
    for snapshot in snapshots:
        if snapshot.is_model:
            context.state_sync.add_interval(snapshot, start, end)


@pytest.mark.parametrize("bound", [0, 1])
@pytest.mark.parametrize(
    "race_variant,partial_daily",
    [
        pytest.param("before_apply", False, id="before-apply-hourly"),
        pytest.param("before_apply", True, id="before-apply-partial-daily-lookback"),
        pytest.param("during_backfill", False, id="during-backfill-hourly"),
        pytest.param("serialized", False, id="serialized-before-apply-hourly"),
    ],
)
def test_apply_preserves_frontier_and_scheduler_repairs_lineage(
    tmp_path, bound, race_variant, partial_daily
):
    parent_path = _create_project(tmp_path, partial_daily=partial_daily)

    with time_machine.travel(PLAN_TIME, tick=False):
        context = Context(paths=tmp_path)
        context.apply(context.plan_builder("prod").build())
        incumbent = context.state_sync.get_environment("prod")
        assert incumbent and incumbent.finalized_ts
        _write_parent(parent_path, 2, partial_daily=partial_daily)
        context.load()
        builder = context.plan_builder("prod", no_gaps=True, max_trailing_gap_intervals=bound)
        plan = builder.build()
        for snapshot in plan.uncategorized:
            builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
        plan = builder.build()
        original_ends = dict(plan.end_override_per_model)
        assert original_ends
        assert all(to_timestamp(end) == PLAN_FRONTIER for end in original_ends.values())

        serialized_plan = (
            EvaluatablePlan.parse_raw(plan.to_evaluatable().json())
            if race_variant == "serialized"
            else None
        )
        if serialized_plan:
            assert serialized_plan.max_trailing_gap_intervals == bound
            assert serialized_plan.end_override_per_model == original_ends
            assert serialized_plan.planned_frontiers == plan.planned_frontiers

    with time_machine.travel(PROMOTION_TIME, tick=False):
        if race_variant in {"before_apply", "serialized"}:
            assert context.run()
            assert all(
                end == OBSERVED_FRONTIER
                for end in context.state_sync.max_interval_end_per_model("prod").values()
            )

        if race_variant == "during_backfill":
            real_promote = context.state_sync.promote

            def promote_with_race(*args, **kwargs):
                candidate_snapshots = context.state_sync.get_snapshots(
                    plan.environment.snapshots
                ).values()
                assert all(
                    snapshot.intervals
                    and max(end for _, end in snapshot.intervals) == PLAN_FRONTIER
                    for snapshot in candidate_snapshots
                    if snapshot.is_model
                )
                _add_interval_to_snapshots(
                    context,
                    context.state_sync.get_snapshots(incumbent.snapshots).values(),
                    PLAN_FRONTIER,
                    OBSERVED_FRONTIER,
                )
                return real_promote(*args, **kwargs)

            context.state_sync.promote = promote_with_race

        def evaluate_plan():
            if serialized_plan:
                context._scheduler.create_plan_evaluator(context).evaluate(serialized_plan)
            else:
                context.apply(plan)

        if not bound:
            with pytest.raises(SQLMeshError, match="Detected missing intervals"):
                evaluate_plan()
            assert context.state_sync.get_environment("prod").plan_id == incumbent.plan_id
            context.close()
            return

        assert all(f.end == PLAN_FRONTIER for f in plan.planned_frontiers.values())
        assert plan.planned_frontiers
        evaluate_plan()
        assert plan.end_override_per_model == original_ends
        if serialized_plan:
            assert serialized_plan.end_override_per_model == original_ends
        promoted = context.state_sync.get_environment("prod")
        assert promoted.plan_id == plan.plan_id
        assert promoted.finalized_ts
        assert all(
            end == PLAN_FRONTIER
            for end in context.state_sync.max_interval_end_per_model("prod").values()
        )
        assert context.engine_adapter.fetchall(
            "SELECT count(*), min(value), max(value) FROM test.child"
        ) == [(4, 2, 2)]
        assert context.run()
        assert all(
            end == OBSERVED_FRONTIER
            for end in context.state_sync.max_interval_end_per_model("prod").values()
        )
        assert context.engine_adapter.fetchall(
            "SELECT count(*), min(value), max(value) FROM test.child"
        ) == [(5, 2, 2)]
        context.close()


def test_apply_rejects_trailing_gap_with_downstream_coverage(tmp_path):
    parent_path = _create_project(tmp_path)

    with time_machine.travel(PLAN_TIME, tick=False):
        context = Context(paths=tmp_path)
        context.apply(context.plan_builder("prod").build())
        incumbent = context.state_sync.get_environment("prod")
        assert incumbent and incumbent.finalized_ts
        _write_parent(parent_path, 2)
        context.load()
        builder = context.plan_builder("prod", no_gaps=True, max_trailing_gap_intervals=1)
        plan = builder.build()
        for snapshot in plan.uncategorized:
            builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
        plan = builder.build()

    with time_machine.travel(PROMOTION_TIME, tick=False):
        real_promote = context.state_sync.promote

        def promote_with_downstream_coverage(*args, **kwargs):
            _add_interval_to_snapshots(
                context,
                context.state_sync.get_snapshots(incumbent.snapshots).values(),
                PLAN_FRONTIER,
                OBSERVED_FRONTIER,
            )
            candidate_snapshots = context.state_sync.get_snapshots(
                plan.environment.snapshots
            ).values()
            child = next(
                snapshot
                for snapshot in candidate_snapshots
                if snapshot.node.name == context.get_model("test.child").name
            )
            context.state_sync.add_interval(child, PLAN_FRONTIER, OBSERVED_FRONTIER)
            return real_promote(*args, **kwargs)

        context.state_sync.promote = promote_with_downstream_coverage
        with pytest.raises(SQLMeshError, match="Detected missing intervals"):
            context.apply(plan)
        assert context.state_sync.get_environment("prod").plan_id == incumbent.plan_id
        context.close()
