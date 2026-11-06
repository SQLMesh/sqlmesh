from __future__ import annotations

import typing as t
import pytest
import time_machine

from sqlmesh.core import dialect as d
from sqlmesh.core.model import (
    SqlModel,
    load_sql_based_model,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.utils.date import to_timestamp
from tests.core.integration.utils import add_projection_to_model

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 00:00:00 UTC")
@pytest.mark.parametrize(
    "forward_only, expected_intervals",
    [
        (
            False,
            [
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
            ],
        ),
        (
            True,
            [
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
            ],
        ),
    ],
)
def test_cron_not_aligned_with_day_boundary(
    init_and_plan_context: t.Callable,
    forward_only: bool,
    expected_intervals: t.List[t.Tuple[int, int]],
):
    context, plan = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "kind": model.kind.copy(update={"forward_only": forward_only}),
            "cron": "0 12 * * *",
        }
    )
    context.upsert_model(model)

    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    waiter_revenue_by_day_snapshot = context.get_snapshot(model.name, raise_if_missing=True)
    assert waiter_revenue_by_day_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-07"))
    ]

    model = add_projection_to_model(t.cast(SqlModel, model), literal=True)
    context.upsert_model(model)

    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )

    with time_machine.travel("2023-01-08 00:10:00 UTC"):  # Past model's cron.
        plan = context.plan_builder(
            "dev", select_models=[model.name], skip_tests=True, enable_preview=True
        ).build()
        assert plan.missing_intervals == [
            SnapshotIntervals(
                snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
                intervals=expected_intervals,
            ),
        ]


@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_cron_not_aligned_with_day_boundary_new_model(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    existing_model = context.get_model("sushi.waiter_revenue_by_day")
    existing_model = SqlModel.parse_obj(
        {
            **existing_model.dict(),
            "kind": existing_model.kind.copy(update={"forward_only": True}),
        }
    )
    context.upsert_model(existing_model)

    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    # Add a new model and make a change to a forward-only model.
    # The cron of the new model is not aligned with the day boundary.
    new_model = load_sql_based_model(
        d.parse(
            """
        MODEL (
            name memory.sushi.new_model,
            kind FULL,
            cron '0 8 * * *',
            start '2023-01-01',
        );

        SELECT 1 AS one;
        """
        )
    )
    context.upsert_model(new_model)

    existing_model = add_projection_to_model(t.cast(SqlModel, existing_model), literal=True)
    context.upsert_model(existing_model)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=True).build()
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "memory.sushi.new_model", raise_if_missing=True
            ).snapshot_id,
            intervals=[(to_timestamp("2023-01-06"), to_timestamp("2023-01-07"))],
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.waiter_revenue_by_day", raise_if_missing=True
            ).snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]


@time_machine.travel("2023-01-08 00:00:00 UTC", tick=False)
def test_parent_cron_after_child(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "cron": "50 23 * * *",
        }
    )
    context.upsert_model(model)

    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    waiter_revenue_by_day_snapshot = context.get_snapshot(model.name, raise_if_missing=True)
    assert waiter_revenue_by_day_snapshot.intervals == [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-07"))
    ]

    top_waiters_model = context.get_model("sushi.top_waiters")
    top_waiters_model = add_projection_to_model(t.cast(SqlModel, top_waiters_model), literal=True)
    context.upsert_model(top_waiters_model)

    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    with time_machine.travel("2023-01-08 23:55:00 UTC"):  # Past parent's cron, but before child's
        plan = context.plan_builder("dev", skip_tests=True).build()
        # Make sure the waiter_revenue_by_day model is not backfilled.
        assert plan.missing_intervals == [
            SnapshotIntervals(
                snapshot_id=top_waiters_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
                    (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
                    (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
                    (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                    (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                    (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                    (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
                ],
            ),
        ]


@time_machine.travel("2025-03-08 00:00:00 UTC")
def test_tz(init_and_plan_context):
    context, _ = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(
        SqlModel.parse_obj(
            {**model.dict(), "cron_tz": "America/Los_Angeles", "start": "2025-03-07"}
        )
    )

    def assert_intervals(plan, intervals):
        assert (
            next(
                intervals.intervals
                for intervals in plan.missing_intervals
                if intervals.snapshot_id.name == model.fqn
            )
            == intervals
        )

    plan = context.plan_builder("prod", skip_tests=True).build()

    # we have missing intervals but not waiter_revenue_by_day because it's not midnight pacific yet
    assert plan.missing_intervals

    with pytest.raises(StopIteration):
        assert_intervals(plan, [])

    # now we're ready 8AM UTC == midnight PST
    with time_machine.travel("2025-03-08 08:00:00 UTC"):
        plan = context.plan_builder("prod", skip_tests=True).build()
        assert_intervals(plan, [(to_timestamp("2025-03-07"), to_timestamp("2025-03-08"))])

    with time_machine.travel("2025-03-09 07:00:00 UTC"):
        plan = context.plan_builder("prod", skip_tests=True).build()

        assert_intervals(
            plan,
            [
                (to_timestamp("2025-03-07"), to_timestamp("2025-03-08")),
            ],
        )

    with time_machine.travel("2025-03-09 08:00:00 UTC"):
        plan = context.plan_builder("prod", skip_tests=True).build()

        assert_intervals(
            plan,
            [
                (to_timestamp("2025-03-07"), to_timestamp("2025-03-08")),
                (to_timestamp("2025-03-08"), to_timestamp("2025-03-09")),
            ],
        )

        context.apply(plan)

        plan = context.plan_builder("prod", skip_tests=True).build()
        assert not plan.missing_intervals


@time_machine.travel("2023-01-08 00:00:00 UTC", tick=False)
def test_new_model_with_cron_and_no_missing_intervals(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    # The cron of this model hasn't elapsed for the only interval in the plan's date range
    cron_model = load_sql_based_model(
        d.parse(
            """
        MODEL (
            name memory.sushi.cron_model,
            kind FULL,
            cron '20 8 * * *',
        );

        SELECT 1 AS one;
        """
        )
    )
    daily_model = load_sql_based_model(
        d.parse(
            """
        MODEL (
            name memory.sushi.daily_model,
            kind FULL,
            cron '@daily',
        );

        SELECT 1 AS one;
        """
        )
    )
    context.upsert_model(cron_model)
    context.upsert_model(daily_model)

    with time_machine.travel("2023-01-09 01:49:00 UTC", tick=False):
        plan = context.plan_builder("dev", start="2023-01-08", skip_tests=True).build()
        missing_names = {i.snapshot_id.name for i in plan.missing_intervals}
        assert '"memory"."sushi"."daily_model"' in missing_names
        assert '"memory"."sushi"."cron_model"' not in missing_names
        context.apply(plan)

    assert context.fetchdf("SELECT one FROM sushi__dev.cron_model")["one"].tolist() == []
