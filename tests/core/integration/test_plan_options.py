from __future__ import annotations

import logging
import typing as t
import pytest
from sqlmesh.core.console import (
    set_console,
    get_console,
    TerminalConsole,
)
import time_machine

from sqlmesh.core import dialect as d
from sqlmesh.core.console import get_console
from sqlmesh.core.model import (
    SqlModel,
    load_sql_based_model,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.core.snapshot import (
    SnapshotChangeCategory,
)
from sqlmesh.utils.date import to_datetime, to_timestamp
from sqlmesh.utils.errors import (
    NoChangesPlanError,
)
from tests.core.integration.utils import (
    add_projection_to_model,
)

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_empty_backfill(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    plan = context.plan_builder("prod", skip_tests=True, empty_backfill=True).build()
    assert plan.missing_intervals
    assert plan.empty_backfill
    assert not plan.requires_backfill

    context.apply(plan)

    for model in context.models.values():
        if model.is_seed or model.kind.is_symbolic:
            continue
        row_num = context.engine_adapter.fetchone(f"SELECT COUNT(*) FROM {model.name}")[0]
        assert row_num == 0

    plan = context.plan_builder("prod", skip_tests=True).build()
    assert not plan.requires_backfill
    assert not plan.has_changes
    assert not plan.missing_intervals

    snapshots = plan.snapshots
    for snapshot in snapshots.values():
        if not snapshot.intervals:
            continue
        assert snapshot.intervals[-1][1] <= to_timestamp("2023-01-08")


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_empty_backfill_new_model(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

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
    new_model_name = context.upsert_model(new_model).fqn

    with time_machine.travel("2023-01-09 00:00:00 UTC"):
        plan = context.plan_builder("dev", skip_tests=True, empty_backfill=True).build()
        assert plan.end == to_datetime("2023-01-09")
        assert plan.missing_intervals
        assert plan.empty_backfill
        assert not plan.requires_backfill

        context.apply(plan)

        for model in context.models.values():
            if model.is_seed or model.kind.is_symbolic:
                continue
            row_num = context.engine_adapter.fetchone(f"SELECT COUNT(*) FROM sushi__dev.new_model")[
                0
            ]
            assert row_num == 0

        plan = context.plan_builder("prod", skip_tests=True).build()
        assert not plan.requires_backfill
        assert not plan.missing_intervals

        snapshots = plan.snapshots
        for snapshot in snapshots.values():
            if not snapshot.intervals:
                continue
            elif snapshot.name == new_model_name:
                assert snapshot.intervals[-1][1] == to_timestamp("2023-01-09")
            else:
                assert snapshot.intervals[-1][1] <= to_timestamp("2023-01-08")


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_plan_explain(init_and_plan_context: t.Callable, caplog):
    old_console = get_console()
    set_console(TerminalConsole())

    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    waiter_revenue_by_day_model = context.get_model("sushi.waiter_revenue_by_day")
    waiter_revenue_by_day_model = add_projection_to_model(
        t.cast(SqlModel, waiter_revenue_by_day_model)
    )
    context.upsert_model(waiter_revenue_by_day_model)

    waiter_revenue_by_day_snapshot = context.get_snapshot(waiter_revenue_by_day_model.name)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters")

    common_kwargs = dict(skip_tests=True, no_prompts=True, explain=True)

    # For now just making sure the plan doesn't error
    with caplog.at_level(logging.ERROR, logger="sqlmesh.core.plan.explainer"):
        context.plan("dev", **common_kwargs)
        context.plan("dev", **common_kwargs, skip_backfill=True)
        context.plan("dev", **common_kwargs, empty_backfill=True)
        context.plan("dev", **common_kwargs, forward_only=True, enable_preview=True)
        context.plan("prod", **common_kwargs)
        context.plan("prod", **common_kwargs, forward_only=True)
        context.plan("prod", **common_kwargs, restate_models=[waiter_revenue_by_day_model.name])

    set_console(old_console)

    # Every stage produced by the plan must have a corresponding visit method on the explainer
    assert "Unexpected stage" not in caplog.text

    # Make sure that the now changes were actually applied
    for target_env in ("dev", "prod"):
        plan = context.plan_builder(target_env, skip_tests=True).build()
        assert plan.has_changes
        assert plan.missing_intervals
        assert plan.directly_modified == {waiter_revenue_by_day_snapshot.snapshot_id}
        assert len(plan.new_snapshots) == 2
        assert {s.snapshot_id for s in plan.new_snapshots} == {
            waiter_revenue_by_day_snapshot.snapshot_id,
            top_waiters_snapshot.snapshot_id,
        }


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_plan_ignore_cron(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context("examples/sushi")

    expressions = d.parse(
        f"""
        MODEL (
            name memory.sushi.test_allow_partials,
            kind INCREMENTAL_UNMANAGED,
            allow_partials true,
            start '2023-01-01',
        );

        SELECT @end_ts AS end_ts
        """
    )
    model = load_sql_based_model(expressions)

    context.upsert_model(model)
    context.plan("prod", skip_tests=True, auto_apply=True, no_prompts=True)

    assert (
        context.engine_adapter.fetchone("SELECT MAX(end_ts) FROM memory.sushi.test_allow_partials")[
            0
        ]
        == "2023-01-07 23:59:59.999999"
    )

    plan_no_ignore_cron = context.plan_builder(
        "prod", run=True, ignore_cron=False, skip_tests=True
    ).build()
    assert not plan_no_ignore_cron.missing_intervals

    plan = context.plan_builder("prod", run=True, ignore_cron=True, skip_tests=True).build()
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(model, raise_if_missing=True).snapshot_id,
            intervals=[
                (to_timestamp("2023-01-08"), to_timestamp("2023-01-08 15:00:00")),
            ],
        )
    ]
    context.apply(plan)

    assert (
        context.engine_adapter.fetchone("SELECT MAX(end_ts) FROM memory.sushi.test_allow_partials")[
            0
        ]
        == "2023-01-08 14:59:59.999999"
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_plan_with_run(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    with time_machine.travel("2023-01-09 00:00:00 UTC"):
        plan = context.plan(run=True)
        assert plan.has_changes
        assert plan.missing_intervals

        context.apply(plan)

        snapshots = context.state_sync.state_sync.get_snapshots(context.snapshots.values())
        assert {s.name: s.intervals[0][1] for s in snapshots.values() if s.intervals} == {
            '"memory"."sushi"."waiter_revenue_by_day"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."order_items"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."orders"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."items"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."customer_revenue_lifetime"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."customer_revenue_by_day"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."latest_order"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."waiter_names"': to_timestamp("2023-01-08"),
            '"memory"."sushi"."raw_marketing"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."marketing"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."waiter_as_customer_by_day"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."top_waiters"': to_timestamp("2023-01-09"),
            '"memory"."raw"."demographics"': to_timestamp("2023-01-09"),
            "assert_item_price_above_zero": to_timestamp("2023-01-09"),
            '"memory"."sushi"."active_customers"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."customers"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."count_customers_active"': to_timestamp("2023-01-09"),
            '"memory"."sushi"."count_customers_inactive"': to_timestamp("2023-01-09"),
        }


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_select_models(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Modify 2 models.
    model = context.get_model("sushi.waiter_revenue_by_day")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "query": model.query.order_by("waiter_id"),  # type: ignore
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    model = context.get_model("sushi.customer_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    expected_intervals = [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
    ]

    waiter_revenue_by_day_snapshot_id = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    ).snapshot_id

    # Select one of the modified models.
    plan_builder = context.plan_builder(
        "dev", select_models=["*waiter_revenue_by_day"], skip_tests=True
    )
    snapshot = plan_builder._context_diff.snapshots[waiter_revenue_by_day_snapshot_id]
    plan_builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
    plan = plan_builder.build()

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert len(dev_df) == 7

    # Make sure that we only create a view for the selected model.
    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert len(schema_objects) == 1
    assert schema_objects[0].name == "waiter_revenue_by_day"

    # Validate the other modified model.
    assert not context.get_snapshot("sushi.customer_revenue_by_day").change_category
    assert not context.get_snapshot("sushi.customer_revenue_by_day").version

    # Validate the downstream model.
    assert not context.engine_adapter.table_exists(
        context.get_snapshot("sushi.top_waiters").table_name()
    )
    assert not context.engine_adapter.table_exists(
        context.get_snapshot("sushi.top_waiters").table_name(False)
    )

    # Make sure that tables are created when deploying to prod.
    plan = context.plan("prod", skip_tests=True)
    context.apply(plan)
    assert context.engine_adapter.table_exists(
        context.get_snapshot("sushi.top_waiters").table_name()
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_select_models_for_backfill(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    expected_intervals = [
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
    ]

    plan = context.plan_builder(
        "dev", backfill_models=["+*waiter_revenue_by_day"], skip_tests=True
    ).build()

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.items", raise_if_missing=True).snapshot_id,
            intervals=expected_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.order_items", raise_if_missing=True
            ).snapshot_id,
            intervals=expected_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.orders", raise_if_missing=True).snapshot_id,
            intervals=expected_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.waiter_revenue_by_day", raise_if_missing=True
            ).snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert len(dev_df) == 1

    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {
        "items",
        "order_items",
        "orders",
        "waiter_revenue_by_day",
    }

    assert not context.engine_adapter.table_exists(
        context.get_snapshot("sushi.customer_revenue_by_day").table_name()
    )

    # Make sure that tables are created when deploying to prod.
    plan = context.plan("prod")
    context.apply(plan)
    assert context.engine_adapter.table_exists(
        context.get_snapshot("sushi.customer_revenue_by_day").table_name()
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_select_unchanged_model_for_backfill(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Modify 2 models.
    model = context.get_model("sushi.waiter_revenue_by_day")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "query": d.parse_one(
            f"{model.query.sql(dialect='duckdb')} ORDER BY waiter_id", dialect="duckdb"
        ),
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    model = context.get_model("sushi.customer_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    expected_intervals = [
        (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        (to_timestamp("2023-01-02"), to_timestamp("2023-01-03")),
        (to_timestamp("2023-01-03"), to_timestamp("2023-01-04")),
        (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
        (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
        (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
        (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
    ]

    waiter_revenue_by_day_snapshot_id = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    ).snapshot_id

    # Select one of the modified models.
    plan_builder = context.plan_builder(
        "dev", select_models=["*waiter_revenue_by_day"], skip_tests=True
    )
    snapshot = plan_builder._context_diff.snapshots[waiter_revenue_by_day_snapshot_id]
    plan_builder.set_choice(snapshot, SnapshotChangeCategory.BREAKING)
    plan = plan_builder.build()

    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    # Make sure that we only create a view for the selected model.
    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {"waiter_revenue_by_day"}

    # Now select a model downstream from the previously modified one in order to backfill it.
    plan = context.plan_builder("dev", select_models=["*top_waiters"], skip_tests=True).build()

    assert not plan.has_changes
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot(
                "sushi.top_waiters", raise_if_missing=True
            ).snapshot_id,
            intervals=expected_intervals,
        ),
    ]

    context.apply(plan)

    # Make sure that a view has been created for the downstream selected model.
    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {"waiter_revenue_by_day", "top_waiters"}


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_create_environment_no_changes_with_selector(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    with pytest.raises(NoChangesPlanError):
        context.plan_builder("dev").build()

    plan = context.plan_builder("dev", select_models=["*top_waiters"]).build()
    assert not plan.missing_intervals
    context.apply(plan)

    schema_objects = context.engine_adapter.get_data_objects("sushi__dev")
    assert {o.name for o in schema_objects} == {"top_waiters"}


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_include_unmodified(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    plan = context.plan_builder(
        "dev",
        include_unmodified=True,
        skip_tests=True,
    ).build()

    all_snapshots = context.snapshots

    assert len(plan.environment.snapshots) == len(all_snapshots)
    assert plan.environment.promoted_snapshot_ids is None

    context.apply(plan)

    data_objs = context.engine_adapter.get_data_objects("sushi__dev")
    assert len(data_objs) == len(
        [s for s in all_snapshots.values() if s.is_model and not s.is_symbolic]
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_select_models_with_include_unmodified(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    plan = context.plan_builder(
        "dev",
        select_models=["*top_waiters", "*customer_revenue_by_day"],
        include_unmodified=True,
        skip_tests=True,
    ).build()

    assert len(plan.environment.snapshots) == len(context.snapshots)

    promoted_set = {s_id.name for s_id in plan.environment.promoted_snapshot_ids}
    assert promoted_set == {
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."top_waiters"',
    }

    context.apply(plan)

    data_objs = context.engine_adapter.get_data_objects("sushi__dev")
    assert len(data_objs) == 2
    assert {o.name for o in data_objs} == {"customer_revenue_by_day", "top_waiters"}
