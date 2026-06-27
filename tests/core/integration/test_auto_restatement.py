from __future__ import annotations

import typing as t
import pandas as pd  # noqa: TID253
import pytest
import time_machine
from sqlglot import exp

from sqlmesh.core import dialect as d
from sqlmesh.core.macros import macro
from sqlmesh.core.model import (
    load_sql_based_model,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.utils.date import to_timestamp

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 01:00:00 UTC")
def test_run_auto_restatement(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    context.engine_adapter.execute(
        "CREATE TABLE _test_auto_restatement_intervals (name STRING, start_ds STRING, end_ds STRING)"
    )

    @macro()
    def record_intervals(
        evaluator, name: exp.Expr, start: exp.Expr, end: exp.Expr, **kwargs: t.Any
    ) -> None:
        if evaluator.runtime_stage == "evaluating":
            evaluator.engine_adapter.insert_append(
                "_test_auto_restatement_intervals",
                pd.DataFrame({"name": [name.name], "start_ds": [start.name], "end_ds": [end.name]}),
            )

    new_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                auto_restatement_cron '0 6 * * 7',  -- At 6am every Sunday
                auto_restatement_intervals 3,
            ),
            start '2023-01-01',
        );

        @record_intervals('new_model', @start_ds, @end_ds);

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    new_model = load_sql_based_model(new_model_expr)
    context.upsert_model(new_model)

    new_model_downstream_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model_downstream,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
            ),
            cron '@hourly',
        );

        @record_intervals('new_model_downstream', @start_ts, @end_ts);

        SELECT * FROM memory.sushi.new_model;
        """
    )
    new_model_downstream = load_sql_based_model(new_model_downstream_expr)
    context.upsert_model(new_model_downstream)

    plan = context.plan_builder("prod").build()
    context.apply(plan)

    with time_machine.travel("2023-01-08 06:01:00 UTC"):
        assert context.run()

        recorded_intervals_df = context.engine_adapter.fetchdf(
            "SELECT start_ds, end_ds FROM _test_auto_restatement_intervals WHERE name = 'new_model'"
        )
        # The first interval is the first backfill and the second interval should be the 3 auto restated intervals
        assert recorded_intervals_df.to_dict() == {
            "start_ds": {0: "2023-01-01", 1: "2023-01-05"},
            "end_ds": {0: "2023-01-07", 1: "2023-01-07"},
        }
        recorded_intervals_downstream_df = context.engine_adapter.fetchdf(
            "SELECT start_ds, end_ds FROM _test_auto_restatement_intervals WHERE name = 'new_model_downstream'"
        )
        # The first interval is the first backfill, the second interval should be the 3 days of restated intervals, and
        # the third interval should catch up to the current hour
        assert recorded_intervals_downstream_df.to_dict() == {
            "start_ds": {
                0: "2023-01-01 00:00:00",
                1: "2023-01-05 00:00:00",
                2: "2023-01-08 01:00:00",
            },
            "end_ds": {
                0: "2023-01-08 00:59:59.999999",
                1: "2023-01-07 23:59:59.999999",
                2: "2023-01-08 05:59:59.999999",
            },
        }

        snapshot = context.get_snapshot(new_model.name)
        snapshot = context.state_sync.state_sync.get_snapshots([snapshot.snapshot_id])[
            snapshot.snapshot_id
        ]
        assert snapshot.next_auto_restatement_ts == to_timestamp("2023-01-15 06:00:00")
        assert not snapshot.pending_restatement_intervals

        snapshot_downstream = context.get_snapshot(new_model_downstream.name)
        snapshot_downstream = context.state_sync.state_sync.get_snapshots(
            [snapshot_downstream.snapshot_id]
        )[snapshot_downstream.snapshot_id]
        assert not snapshot_downstream.next_auto_restatement_ts
        assert not snapshot_downstream.pending_restatement_intervals


@time_machine.travel("2023-01-08 01:00:00 UTC")
def test_run_auto_restatement_plan_preview(init_and_plan_context: t.Callable):
    context, init_plan = init_and_plan_context("examples/sushi")
    context.apply(init_plan)

    new_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                auto_restatement_cron '0 6 * * 7',
            ),
            start '2023-01-01',
        );

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    new_model = load_sql_based_model(new_model_expr)
    context.upsert_model(new_model)
    snapshot = context.get_snapshot(new_model.name)

    plan_dev = context.plan_builder("dev").build()
    # Make sure that a limited preview is computed by default
    assert to_timestamp(plan_dev.start) == to_timestamp("2023-01-07")
    assert plan_dev.missing_intervals == [
        SnapshotIntervals(
            snapshot.snapshot_id,
            [(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        )
    ]
    assert not plan_dev.deployability_index.is_deployable(snapshot.snapshot_id)
    context.apply(plan_dev)

    plan_prod = context.plan_builder("prod").build()
    assert plan_prod.missing_intervals == [
        SnapshotIntervals(
            context.get_snapshot(new_model.name).snapshot_id,
            [
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        )
    ]
    context.apply(plan_prod)


@time_machine.travel("2023-01-08 01:00:00 UTC")
def test_run_auto_restatement_failure(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    @macro()
    def fail_auto_restatement(evaluator, start: exp.Expr, **kwargs: t.Any) -> None:
        if evaluator.runtime_stage == "evaluating" and start.name != "2023-01-01":
            raise Exception("Failed")

    new_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                auto_restatement_cron '0 6 * * 7',  -- At 6am every Sunday
                auto_restatement_intervals 3,
            ),
            start '2023-01-01',
        );

        @fail_auto_restatement(@start_ds);

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    new_model = load_sql_based_model(new_model_expr)
    context.upsert_model(new_model)

    plan = context.plan_builder("prod").build()
    context.apply(plan)

    with time_machine.travel("2023-01-08 06:01:00 UTC"):
        run_status = context.run()
        assert run_status.is_failure

        snapshot = context.get_snapshot(new_model.name)
        snapshot = context.state_sync.state_sync.get_snapshots([snapshot.snapshot_id])[
            snapshot.snapshot_id
        ]
        assert snapshot.next_auto_restatement_ts == to_timestamp("2023-01-15 06:00:00")
        assert snapshot.pending_restatement_intervals == [
            (to_timestamp("2023-01-05"), to_timestamp("2023-01-08"))
        ]
