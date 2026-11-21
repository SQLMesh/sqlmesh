from __future__ import annotations

from pathlib import Path
import typing as t
import numpy as np  # noqa: TID253
import pandas as pd  # noqa: TID253
import pytest
from sqlglot import exp
import time_machine
from pytest_mock.plugin import MockerFixture

from sqlmesh.core import dialect as d
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)
from sqlmesh.core.context import Context
from sqlmesh.core.config.categorizer import CategorizerConfig
from sqlmesh.core.model import (
    FullKind,
    SqlModel,
    load_sql_based_model,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.core.snapshot import (
    SnapshotChangeCategory,
)
from sqlmesh.utils.date import to_datetime, to_timestamp
from sqlmesh.utils.errors import AdditiveChangeError, DestructiveChangeError, PlanError
from tests.core.integration.utils import add_projection_to_model

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
@pytest.mark.parametrize(
    "context_fixture",
    ["sushi_context", "sushi_no_default_catalog"],
)
def test_forward_only_plan_with_effective_date(context_fixture: Context, request):
    context = request.getfixturevalue(context_fixture)
    model_name = "sushi.waiter_revenue_by_day"
    model = context.get_model(model_name)
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)), start="2023-01-01")
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan_builder = context.plan_builder("dev", skip_tests=True, forward_only=True)
    plan = plan_builder.build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.context_diff.snapshots[snapshot.snapshot_id].is_forward_only
    assert plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].is_forward_only

    assert to_timestamp(plan.start) == to_timestamp("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
    ]

    plan = plan_builder.set_effective_from("2023-01-05").build()
    # Default start should be set to effective_from
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    plan = plan_builder.set_start("2023-01-06").build()
    # Start override should take precedence
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    plan = plan_builder.set_effective_from("2023-01-04").build()
    # Start should remain unchanged
    assert plan.start == "2023-01-06"
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [
        pd.to_datetime("2023-01-06"),
        pd.to_datetime("2023-01-07"),
    ]

    prod_plan = context.plan_builder(skip_tests=True).build()
    # Make sure that the previously set effective_from is respected
    assert prod_plan.start == to_timestamp("2023-01-04")
    assert prod_plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiters_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-04"), to_timestamp("2023-01-05")),
                (to_timestamp("2023-01-05"), to_timestamp("2023-01-06")),
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(prod_plan)

    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert prod_df["event_date"].tolist() == [
        pd.to_datetime(x) for x in ["2023-01-04", "2023-01-05", "2023-01-06", "2023-01-07"]
    ]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_forward_only_model_regular_plan(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = add_projection_to_model(t.cast(SqlModel, model))
    forward_only_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"kind": forward_only_kind})

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=False).build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.context_diff.snapshots[snapshot.snapshot_id].is_forward_only
    assert plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].is_forward_only

    assert plan.start == to_datetime("2023-01-01")
    assert not plan.missing_intervals

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert not dev_df["event_date"].tolist()

    # Run a restatement plan to preview changes
    plan_builder = context.plan_builder(
        "dev", skip_tests=True, restate_models=[model_name], enable_preview=False
    )
    plan_builder.set_start("2023-01-06")
    assert plan_builder.build().missing_intervals == [
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
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-06"), to_timestamp("2023-01-07")),
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    # Make sure that changed start is reflected in missing intervals
    plan_builder.set_start("2023-01-07")
    assert plan_builder.build().missing_intervals == [
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
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan_builder.build())

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [pd.to_datetime("2023-01-07")]

    # Promote changes to prod
    prod_plan = context.plan_builder(skip_tests=True).build()
    assert not prod_plan.missing_intervals

    context.apply(prod_plan)

    # The change was applied in a forward-only manner so no values in the new column should be populated
    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert not prod_df["event_date"].tolist()


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_forward_only_model_regular_plan_preview_enabled(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = add_projection_to_model(t.cast(SqlModel, model))
    forward_only_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"kind": forward_only_kind})

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=True).build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.context_diff.snapshots[snapshot.snapshot_id].is_forward_only
    assert plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].is_forward_only

    assert to_timestamp(plan.start) == to_timestamp("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [pd.to_datetime("2023-01-07")]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_forward_only_model_restate_full_history_in_dev(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    model_name = "memory.sushi.customer_max_revenue"
    expressions = d.parse(
        f"""
        MODEL (
            name {model_name},
            kind INCREMENTAL_BY_UNIQUE_KEY (
                unique_key customer_id,
                forward_only true,
            ),
        );

        SELECT
          customer_id, MAX(revenue) AS max_revenue
        FROM memory.sushi.customer_revenue_lifetime
        GROUP BY 1;
        """
    )

    model = load_sql_based_model(expressions)
    assert model.forward_only
    assert model.kind.full_history_restatement_only
    context.upsert_model(model)

    context.plan("prod", skip_tests=True, auto_apply=True, enable_preview=False)

    model_kwargs = {
        **model.dict(),
        # Make a breaking change.
        "query": model.query.order_by("customer_id"),  # type: ignore
    }
    context.upsert_model(SqlModel.parse_obj(model_kwargs))

    # Apply the model change in dev
    plan = context.plan_builder(
        "dev",
        skip_tests=True,
        enable_preview=False,
        categorizer_config=CategorizerConfig.all_full(),
    ).build()
    assert not plan.missing_intervals
    context.apply(plan)

    snapshot = context.get_snapshot(model, raise_if_missing=True)
    snapshot_table_name = snapshot.table_name(False)

    # Manually insert a dummy value to check that the table is recreated during the restatement
    context.engine_adapter.insert_append(
        snapshot_table_name,
        pd.DataFrame({"customer_id": [-1], "max_revenue": [100]}),
    )
    df = context.engine_adapter.fetchdf(
        "SELECT COUNT(*) AS cnt FROM sushi__dev.customer_max_revenue WHERE customer_id = -1"
    )
    assert df["cnt"][0] == 1

    # Apply a restatement plan in dev
    plan = context.plan("dev", restate_models=[model.name], auto_apply=True, enable_preview=False)
    assert len(plan.missing_intervals) == 1

    # Check that the dummy value is not present
    df = context.engine_adapter.fetchdf(
        "SELECT COUNT(*) AS cnt FROM sushi__dev.customer_max_revenue WHERE customer_id = -1"
    )
    assert df["cnt"][0] == 0

    # Check that the table is not empty
    df = context.engine_adapter.fetchdf(
        "SELECT COUNT(*) AS cnt FROM sushi__dev.customer_max_revenue"
    )
    assert df["cnt"][0] > 0


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_full_history_restatement_model_regular_plan_preview_enabled(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.marketing"  # SCD2 model

    model = context.get_model(model_name)
    model = add_projection_to_model(t.cast(SqlModel, model))

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    customers_snapshot = context.get_snapshot("sushi.customers", raise_if_missing=True)
    active_customers_snapshot = context.get_snapshot(
        "sushi.active_customers", raise_if_missing=True
    )
    waiter_as_customer_snapshot = context.get_snapshot(
        "sushi.waiter_as_customer_by_day", raise_if_missing=True
    )

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=True).build()

    assert len(plan.new_snapshots) == 6
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[customers_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[active_customers_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[waiter_as_customer_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert all(s.is_forward_only for s in plan.new_snapshots)

    assert to_timestamp(plan.start) == to_timestamp("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-07"), to_timestamp("2023-01-08")),
            ],
        ),
    ]

    context.apply(plan)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_metadata_changed_regular_plan_preview_enabled(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = model.copy(update={"owner": "new_owner"})

    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=True).build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.METADATA
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.METADATA
    )
    assert not plan.missing_intervals
    assert not plan.restatements


@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_forward_only_preview_child_that_runs_before_parent(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    # This model runs at minute 30 of every hour
    upstream_model = load_sql_based_model(
        d.parse(
            """
        MODEL (
            name memory.sushi.upstream_model,
            kind FULL,
            cron '30 * * * *',
            start '2023-01-01',
        );

        SELECT 1 AS a;
        """
        )
    )
    context.upsert_model(upstream_model)

    # This model runs at minute 0 of every hour, so it runs before the upstream model
    downstream_model = load_sql_based_model(
        d.parse(
            """
        MODEL (
            name memory.sushi.downstream_model,
            kind INCREMENTAL_BY_TIME_RANGE(
               time_column event_date,
               forward_only True,
            ),
            cron '0 * * * *',
            start '2023-01-01',
        );

        SELECT a, '2023-01-06' AS event_date FROM memory.sushi.upstream_model;
        """
        )
    )
    context.upsert_model(downstream_model)

    context.plan("prod", skip_tests=True, auto_apply=True)

    with time_machine.travel("2023-01-08 00:05:00 UTC"):
        # The downstream model runs but not the upstream model
        context.run("prod")

    # Now it's time for the upstream model to run but it hasn't run yet
    with time_machine.travel("2023-01-08 00:35:00 UTC"):
        # Make a change to the downstream model.
        downstream_model = add_projection_to_model(t.cast(SqlModel, downstream_model), literal=True)
        context.upsert_model(downstream_model)

        # The plan should only backfill the downstream model despite upstream missing intervals
        plan = context.plan_builder("dev", skip_tests=True, enable_preview=True).build()
        assert plan.missing_intervals == [
            SnapshotIntervals(
                snapshot_id=context.get_snapshot(
                    downstream_model.name, raise_if_missing=True
                ).snapshot_id,
                intervals=[
                    (to_timestamp("2023-01-07 23:00:00"), to_timestamp("2023-01-08 00:00:00"))
                ],
            ),
        ]


@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_forward_only_monthly_model(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.waiter_revenue_by_day")
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "kind": model.kind.copy(update={"forward_only": True}),
            "cron": "0 0 1 * *",
            "start": "2022-01-01",
            "audits": [],
        }
    )
    context.upsert_model(model)

    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    waiter_revenue_by_day_snapshot = context.get_snapshot(model.name, raise_if_missing=True)
    assert waiter_revenue_by_day_snapshot.intervals == [
        (to_timestamp("2022-01-01"), to_timestamp("2023-01-01"))
    ]

    model = add_projection_to_model(t.cast(SqlModel, model), literal=True)
    context.upsert_model(model)

    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )

    plan = context.plan_builder(
        "dev", select_models=[model.name], skip_tests=True, enable_preview=True
    ).build()
    assert to_timestamp(plan.start) == to_timestamp("2022-12-01")
    assert to_timestamp(plan.end) == to_timestamp("2023-01-08")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
            intervals=[(to_timestamp("2022-12-01"), to_timestamp("2023-01-01"))],
        ),
    ]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_forward_only_parent_created_in_dev_child_created_in_prod(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    waiter_revenue_by_day_model = context.get_model("sushi.waiter_revenue_by_day")
    waiter_revenue_by_day_model = add_projection_to_model(
        t.cast(SqlModel, waiter_revenue_by_day_model)
    )
    forward_only_kind = waiter_revenue_by_day_model.kind.copy(update={"forward_only": True})
    waiter_revenue_by_day_model = waiter_revenue_by_day_model.copy(
        update={"kind": forward_only_kind}
    )
    context.upsert_model(waiter_revenue_by_day_model)

    waiter_revenue_by_day_snapshot = context.get_snapshot(
        waiter_revenue_by_day_model, raise_if_missing=True
    )
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=False).build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[waiter_revenue_by_day_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert all(s.is_forward_only for s in plan.new_snapshots)
    assert plan.start == to_datetime("2023-01-01")
    assert not plan.missing_intervals

    context.apply(plan)

    # Update the child to refer to a newly added column.
    top_waiters_model = context.get_model("sushi.top_waiters")
    top_waiters_model = add_projection_to_model(t.cast(SqlModel, top_waiters_model), literal=False)
    context.upsert_model(top_waiters_model)

    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("prod", skip_tests=True, enable_preview=False).build()
    assert len(plan.new_snapshots) == 1
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )

    context.apply(plan)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_forward_only_view_migration(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.top_waiters")
    assert model.kind.is_view
    model = add_projection_to_model(t.cast(SqlModel, model))
    context.upsert_model(model)

    # Apply a forward-only plan
    context.plan("prod", skip_tests=True, no_prompts=True, auto_apply=True, forward_only=True)

    # Make sure that the new column got reflected in the view schema
    df = context.fetchdf("SELECT one FROM sushi.top_waiters LIMIT 1")
    assert len(df) == 1


@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_new_forward_only_model(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    context.plan("dev", skip_tests=True, no_prompts=True, auto_apply=True, enable_preview=False)

    snapshot = context.get_snapshot("sushi.marketing")

    # The deployable table should not exist yet
    assert not context.engine_adapter.table_exists(snapshot.table_name())
    assert context.engine_adapter.table_exists(snapshot.table_name(is_deployable=False))

    context.plan("prod", skip_tests=True, no_prompts=True, auto_apply=True)

    assert context.engine_adapter.table_exists(snapshot.table_name())
    assert context.engine_adapter.table_exists(snapshot.table_name(is_deployable=False))


@time_machine.travel("2023-01-08 15:00:00 UTC", tick=True)
@pytest.mark.parametrize("has_view_binding", [False, True])
def test_non_breaking_change_after_forward_only_in_dev(
    init_and_plan_context: t.Callable, has_view_binding: bool
):
    context, plan = init_and_plan_context("examples/sushi")
    context.snapshot_evaluator.adapter.HAS_VIEW_BINDING = has_view_binding
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, forward_only=True).build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[waiter_revenue_by_day_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert all(s.is_forward_only for s in plan.new_snapshots)
    assert to_timestamp(plan.start) == to_timestamp("2023-01-07")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
    ]

    # Apply the forward-only changes first.
    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [pd.to_datetime("2023-01-07")]

    # Make a non-breaking change to a model downstream.
    model = context.get_model("sushi.top_waiters")
    # Select 'one' column from the updated upstream model.
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model), literal=False))
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True).build()
    assert len(plan.new_snapshots) == 1
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert to_timestamp(plan.start) == to_timestamp("2023-01-01")
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

    # Apply the non-breaking changes.
    context.apply(plan)

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT waiter_id FROM sushi__dev.top_waiters WHERE one IS NOT NULL"
    )
    assert not dev_df.empty

    prod_df = context.engine_adapter.fetchdf("DESCRIBE sushi.top_waiters")
    assert "one" not in prod_df["column_name"].tolist()

    # Deploy both changes to prod.
    plan = context.plan_builder("prod", skip_tests=True).build()
    assert plan.start == to_timestamp("2023-01-01")
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

    context.apply(plan)

    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert prod_df.empty

    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT waiter_id FROM sushi.top_waiters WHERE one IS NOT NULL"
    )
    assert prod_df.empty


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_indirect_non_breaking_change_after_forward_only_in_dev(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")
    # Make sure that the most downstream model is a materialized model.
    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": FullKind()})
    context.upsert_model(model)
    context.plan("prod", skip_tests=True, auto_apply=True, no_prompts=True)

    # Make sushi.orders a forward-only model.
    model = context.get_model("sushi.orders")
    updated_model_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"stamp": "force new version", "kind": updated_model_kind})
    context.upsert_model(model)
    snapshot = context.get_snapshot(model, raise_if_missing=True)

    plan = context.plan_builder(
        "dev",
        skip_tests=True,
        enable_preview=False,
        categorizer_config=CategorizerConfig.all_full(),
    ).build()
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert plan.context_diff.snapshots[snapshot.snapshot_id].is_forward_only
    assert not plan.requires_backfill
    context.apply(plan)

    # Make a non-breaking change to a model.
    model = context.get_model("sushi.top_waiters")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=False).build()
    assert len(plan.new_snapshots) == 1
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert plan.start == to_timestamp("2023-01-01")
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

    # Apply the non-breaking changes.
    context.apply(plan)

    # Make a non-breaking change upstream from the previously modified model.
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    waiter_revenue_by_day_snapshot = context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder("dev", skip_tests=True, enable_preview=False).build()
    assert len(plan.new_snapshots) == 2
    assert (
        plan.context_diff.snapshots[waiter_revenue_by_day_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
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

    # Apply the upstream non-breaking changes.
    context.apply(plan)
    assert not context.plan_builder("dev", skip_tests=True).build().requires_backfill

    # Deploy everything to prod.
    plan = context.plan_builder("prod", skip_tests=True, enable_preview=False).build()
    assert plan.start == to_timestamp("2023-01-01")
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
        SnapshotIntervals(
            snapshot_id=waiter_revenue_by_day_snapshot.snapshot_id,
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

    context.apply(plan)
    assert (
        not context.plan_builder("prod", skip_tests=True, enable_preview=False)
        .build()
        .requires_backfill
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_changes_downstream_of_indirect_non_breaking_snapshot_without_intervals(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Make a breaking change first but don't backfill it
    model = context.get_model("sushi.orders")
    model = model.copy(update={"stamp": "force new version"})
    context.upsert_model(model)
    plan_builder = context.plan_builder(
        "dev", skip_backfill=True, skip_tests=True, no_auto_categorization=True
    )
    plan_builder.set_choice(context.get_snapshot(model), SnapshotChangeCategory.BREAKING)
    context.apply(plan_builder.build())

    # Now make a non-breaking change to the same snapshot.
    model = model.copy(update={"stamp": "force another new version"})
    context.upsert_model(model)
    plan_builder = context.plan_builder(
        "dev", skip_backfill=True, skip_tests=True, no_auto_categorization=True
    )
    plan_builder.set_choice(context.get_snapshot(model), SnapshotChangeCategory.NON_BREAKING)
    context.apply(plan_builder.build())

    # Now make a change to a model downstream of the above model.
    downstream_model = context.get_model("sushi.top_waiters")
    downstream_model = downstream_model.copy(update={"stamp": "yet another new version"})
    context.upsert_model(downstream_model)
    plan = context.plan_builder("dev", skip_tests=True).build()

    # If the parent is not representative then the child cannot be deployable
    deployability_index = plan.deployability_index
    assert not deployability_index.is_representative(
        context.get_snapshot("sushi.waiter_revenue_by_day")
    )
    assert not deployability_index.is_deployable(context.get_snapshot("sushi.top_waiters"))


@time_machine.travel("2023-01-08 15:00:00 UTC", tick=True)
def test_metadata_change_after_forward_only_results_in_migration(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Make a forward-only change
    model = context.get_model("sushi.waiter_revenue_by_day")
    model = model.copy(update={"kind": model.kind.copy(update={"forward_only": True})})
    model = add_projection_to_model(t.cast(SqlModel, model))
    context.upsert_model(model)
    plan = context.plan("dev", skip_tests=True, auto_apply=True, no_prompts=True)
    assert len(plan.new_snapshots) == 2
    assert all(s.is_forward_only for s in plan.new_snapshots)

    # Follow-up with a metadata change in the same environment
    model = model.copy(update={"owner": "new_owner"})
    context.upsert_model(model)
    plan = context.plan("dev", skip_tests=True, auto_apply=True, no_prompts=True)
    assert len(plan.new_snapshots) == 2
    assert all(s.change_category == SnapshotChangeCategory.METADATA for s in plan.new_snapshots)

    # Deploy the latest change to prod
    context.plan("prod", skip_tests=True, auto_apply=True, no_prompts=True)

    # Check that the new column was added in prod
    columns = context.engine_adapter.columns("sushi.waiter_revenue_by_day")
    assert "one" in columns


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_indirect_non_breaking_downstream_of_forward_only(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Make sushi.orders a forward-only model.
    forward_only_model = context.get_model("sushi.orders")
    updated_model_kind = forward_only_model.kind.copy(update={"forward_only": True})
    forward_only_model = forward_only_model.copy(
        update={"stamp": "force new version", "kind": updated_model_kind}
    )
    context.upsert_model(forward_only_model)
    forward_only_snapshot = context.get_snapshot(forward_only_model, raise_if_missing=True)

    non_breaking_model = context.get_model("sushi.waiter_revenue_by_day")
    non_breaking_model = non_breaking_model.copy(update={"start": "2023-01-01"})
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, non_breaking_model)))
    non_breaking_snapshot = context.get_snapshot(non_breaking_model, raise_if_missing=True)
    top_waiter_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan = context.plan_builder(
        "dev",
        skip_tests=True,
        enable_preview=False,
        categorizer_config=CategorizerConfig.all_full(),
    ).build()
    assert (
        plan.context_diff.snapshots[forward_only_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert (
        plan.context_diff.snapshots[non_breaking_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiter_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.context_diff.snapshots[forward_only_snapshot.snapshot_id].is_forward_only
    assert not plan.context_diff.snapshots[non_breaking_snapshot.snapshot_id].is_forward_only
    assert not plan.context_diff.snapshots[top_waiter_snapshot.snapshot_id].is_forward_only

    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiter_snapshot.snapshot_id,
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
        SnapshotIntervals(
            snapshot_id=non_breaking_snapshot.snapshot_id,
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

    context.apply(plan)
    assert (
        not context.plan_builder("dev", skip_tests=True, enable_preview=False)
        .build()
        .requires_backfill
    )

    # Deploy everything to prod.
    plan = context.plan_builder("prod", skip_tests=True).build()
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=top_waiter_snapshot.snapshot_id,
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
        SnapshotIntervals(
            snapshot_id=non_breaking_snapshot.snapshot_id,
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

    context.apply(plan)
    assert (
        not context.plan_builder("prod", skip_tests=True, enable_preview=False)
        .build()
        .requires_backfill
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_indirect_non_breaking_view_model_non_representative_snapshot(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context("examples/sushi")

    # Forward-only parent
    forward_only_model_name = "memory.sushi.test_forward_only_model"
    forward_only_model_expressions = d.parse(
        f"""
        MODEL (
            name {forward_only_model_name},
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only true,
            ),
        );

        SELECT '2023-01-01' AS ds, 'value' AS value;
        """
    )
    forward_only_model = load_sql_based_model(forward_only_model_expressions)
    assert forward_only_model.forward_only
    context.upsert_model(forward_only_model)

    # FULL downstream model.
    full_downstream_model_name = "memory.sushi.test_full_downstream_model"
    full_downstream_model_expressions = d.parse(
        f"""
        MODEL (
            name {full_downstream_model_name},
            kind FULL,
        );

        SELECT ds, value FROM {forward_only_model_name};
        """
    )
    full_downstream_model = load_sql_based_model(full_downstream_model_expressions)
    context.upsert_model(full_downstream_model)

    # VIEW downstream of the previous FULL model.
    view_downstream_model_name = "memory.sushi.test_view_downstream_model"
    view_downstream_model_expressions = d.parse(
        f"""
        MODEL (
            name {view_downstream_model_name},
            kind VIEW,
        );

        SELECT ds, value FROM {full_downstream_model_name};
        """
    )
    view_downstream_model = load_sql_based_model(view_downstream_model_expressions)
    context.upsert_model(view_downstream_model)

    # Apply the initial plan with all 3 models.
    context.plan(auto_apply=True, no_prompts=True)

    # Make a change to the forward-only model and apply it in dev.
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, forward_only_model)))
    forward_only_model_snapshot_id = context.get_snapshot(forward_only_model_name).snapshot_id
    full_downstream_model_snapshot_id = context.get_snapshot(full_downstream_model_name).snapshot_id
    view_downstream_model_snapshot_id = context.get_snapshot(view_downstream_model_name).snapshot_id
    dev_plan = context.plan("dev", auto_apply=True, no_prompts=True, enable_preview=False)
    assert (
        dev_plan.snapshots[forward_only_model_snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        dev_plan.snapshots[full_downstream_model_snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert (
        dev_plan.snapshots[view_downstream_model_snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert not dev_plan.missing_intervals

    # Make a follow-up breaking change to the downstream full model.
    new_full_downstream_model_expressions = d.parse(
        f"""
        MODEL (
            name {full_downstream_model_name},
            kind FULL,
        );

        SELECT ds, 'new_value' AS value FROM {forward_only_model_name};
        """
    )
    new_full_downstream_model = load_sql_based_model(new_full_downstream_model_expressions)
    context.upsert_model(new_full_downstream_model)
    full_downstream_model_snapshot_id = context.get_snapshot(full_downstream_model_name).snapshot_id
    view_downstream_model_snapshot_id = context.get_snapshot(view_downstream_model_name).snapshot_id
    dev_plan = context.plan(
        "dev",
        categorizer_config=CategorizerConfig.all_full(),
        auto_apply=True,
        no_prompts=True,
        enable_preview=False,
    )
    assert (
        dev_plan.snapshots[full_downstream_model_snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert (
        dev_plan.snapshots[view_downstream_model_snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_BREAKING
    )
    assert len(dev_plan.missing_intervals) == 2
    assert dev_plan.missing_intervals[0].snapshot_id == full_downstream_model_snapshot_id
    assert dev_plan.missing_intervals[1].snapshot_id == view_downstream_model_snapshot_id

    # Check that the representative view hasn't been created yet.
    assert not context.engine_adapter.table_exists(
        context.get_snapshot(view_downstream_model_name).table_name()
    )

    # Now promote the very first change to prod without promoting the 2nd breaking change.
    context.upsert_model(full_downstream_model)
    context.plan(auto_apply=True, no_prompts=True, categorizer_config=CategorizerConfig.all_full())

    # Finally, make a non-breaking change to the full model in the same dev environment.
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, new_full_downstream_model)))
    full_downstream_model_snapshot_id = context.get_snapshot(full_downstream_model_name).snapshot_id
    view_downstream_model_snapshot_id = context.get_snapshot(view_downstream_model_name).snapshot_id
    dev_plan = context.plan(
        "dev",
        categorizer_config=CategorizerConfig.all_full(),
        auto_apply=True,
        no_prompts=True,
        enable_preview=False,
    )
    assert (
        dev_plan.snapshots[full_downstream_model_snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        dev_plan.snapshots[view_downstream_model_snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )

    # Deploy changes to prod
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Check that the representative view has been created.
    assert context.engine_adapter.table_exists(
        context.get_snapshot(view_downstream_model_name).table_name()
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_indirect_non_breaking_view_model_non_representative_snapshot_migration(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context("examples/sushi")

    forward_only_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.forward_only_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    forward_only_model = load_sql_based_model(forward_only_model_expr)
    context.upsert_model(forward_only_model)

    downstream_view_a_expr = d.parse(
        """
        MODEL (
            name memory.sushi.downstream_view_a,
            kind VIEW,
        );

        SELECT a from memory.sushi.forward_only_model;
        """
    )
    downstream_view_a = load_sql_based_model(downstream_view_a_expr)
    context.upsert_model(downstream_view_a)

    downstream_view_b_expr = d.parse(
        """
        MODEL (
            name memory.sushi.downstream_view_b,
            kind VIEW,
        );

        SELECT a from memory.sushi.downstream_view_a;
        """
    )
    downstream_view_b = load_sql_based_model(downstream_view_b_expr)
    context.upsert_model(downstream_view_b)

    context.plan(auto_apply=True, no_prompts=True, skip_tests=True)

    # Make a forward-only change
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, forward_only_model)))
    # Make a non-breaking change downstream
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, downstream_view_a)))

    context.plan(auto_apply=True, no_prompts=True, skip_tests=True)

    # Make sure the downstrean indirect non-breaking view is available in prod
    count = context.engine_adapter.fetchone("SELECT COUNT(*) FROM memory.sushi.downstream_view_b")[
        0
    ]
    assert count > 0


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_new_forward_only_model_concurrent_versions(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    new_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    new_model = load_sql_based_model(new_model_expr)

    # Add the first version of the model and apply it to dev_a.
    context.upsert_model(new_model)
    snapshot_a = context.get_snapshot(new_model.name)
    plan_a = context.plan_builder("dev_a").build()
    snapshot_a = plan_a.snapshots[snapshot_a.snapshot_id]

    assert snapshot_a.snapshot_id in plan_a.context_diff.new_snapshots
    assert snapshot_a.snapshot_id in plan_a.context_diff.added
    assert snapshot_a.change_category == SnapshotChangeCategory.BREAKING

    context.apply(plan_a)

    new_model_alt_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS b;
        """
    )
    new_model_alt = load_sql_based_model(new_model_alt_expr)

    # Add the second version of the model but don't apply it yet
    context.upsert_model(new_model_alt)
    snapshot_b = context.get_snapshot(new_model_alt.name)
    plan_b = context.plan_builder("dev_b").build()
    snapshot_b = plan_b.snapshots[snapshot_b.snapshot_id]

    assert snapshot_b.snapshot_id in plan_b.context_diff.new_snapshots
    assert snapshot_b.snapshot_id in plan_b.context_diff.added
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING

    assert snapshot_b.fingerprint != snapshot_a.fingerprint
    assert snapshot_b.version == snapshot_a.version

    # Apply the 1st version to prod
    context.upsert_model(new_model)
    plan_prod_a = context.plan_builder("prod").build()
    assert snapshot_a.snapshot_id in plan_prod_a.snapshots
    assert (
        plan_prod_a.snapshots[snapshot_a.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    context.apply(plan_prod_a)

    df = context.fetchdf("SELECT * FROM memory.sushi.new_model")
    assert df.to_dict() == {"ds": {0: "2023-01-07"}, "a": {0: 1}}

    # Modify the 1st version in prod to trigger a forward-only change
    new_model = add_projection_to_model(t.cast(SqlModel, new_model))
    context.upsert_model(new_model)
    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)

    # Apply the 2nd version to dev_b.
    # At this point the snapshot of the 2nd version has already been categorized but not
    # persisted in the state. This means that when the snapshot of the 1st version was
    # being unpaused during promotion to prod, the state of the 2nd version snapshot was not updated
    context.apply(plan_b)

    # Apply the 2nd version to prod
    context.upsert_model(new_model_alt)
    plan_prod_b = context.plan_builder("prod").build()
    assert (
        plan_prod_b.snapshots[snapshot_b.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert not plan_prod_b.requires_backfill
    context.apply(plan_prod_b)

    df = context.fetchdf("SELECT * FROM memory.sushi.new_model").replace({np.nan: None})
    assert df.to_dict() == {"ds": {0: "2023-01-07"}, "b": {0: None}}


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_new_forward_only_model_same_dev_environment(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    new_model_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS a;
        """
    )
    new_model = load_sql_based_model(new_model_expr)

    # Add the first version of the model and apply it to dev.
    context.upsert_model(new_model)
    snapshot_a = context.get_snapshot(new_model.name)
    plan_a = context.plan_builder("dev").build()
    snapshot_a = plan_a.snapshots[snapshot_a.snapshot_id]

    assert snapshot_a.snapshot_id in plan_a.context_diff.new_snapshots
    assert snapshot_a.snapshot_id in plan_a.context_diff.added
    assert snapshot_a.change_category == SnapshotChangeCategory.BREAKING

    context.apply(plan_a)

    df = context.fetchdf("SELECT * FROM memory.sushi__dev.new_model")
    assert df.to_dict() == {"ds": {0: "2023-01-07"}, "a": {0: 1}}

    new_model_alt_expr = d.parse(
        """
        MODEL (
            name memory.sushi.new_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only TRUE,
                on_destructive_change 'allow',
            ),
        );

        SELECT '2023-01-07' AS ds, 1 AS b;
        """
    )
    new_model_alt = load_sql_based_model(new_model_alt_expr)

    # Add the second version of the model and apply it to the same environment.
    context.upsert_model(new_model_alt)
    snapshot_b = context.get_snapshot(new_model_alt.name)

    context.invalidate_environment("dev", sync=True)
    plan_b = context.plan_builder("dev").build()
    snapshot_b = plan_b.snapshots[snapshot_b.snapshot_id]

    context.apply(plan_b)

    df = context.fetchdf("SELECT * FROM memory.sushi__dev.new_model").replace({np.nan: None})
    assert df.to_dict() == {"ds": {0: "2023-01-07"}, "b": {0: 1}}


def _make_duckdb_context(project_path: Path) -> Context:
    cfg = Config(
        gateways={"default": GatewayConfig(connection=DuckDBConnectionConfig())},
        default_gateway="default",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    return Context(paths=[project_path], config=cfg)


def _assert_failed_on_db_down(spy: t.Any, error_type: t.Type[Exception]) -> None:
    """Asserts that dev apply failed because schema validation of `db.down` raised `error_type`."""
    errors = [e for call in spy.call_args_list for e in call.args[0]]
    assert any(
        isinstance(e.__cause__, error_type) and '"db"."down"' in str(e.__cause__) for e in errors
    ), [repr(e.__cause__) for e in errors]


def test_unannotated_star_destructive_and_additive_caught_in_dev_apply(
    tmp_path: Path, mocker: MockerFixture
):
    """Gap S6 (SQL SELECT *): On non-cloning engines (DuckDB, SUPPORTS_CLONING=False), destructive
    and disallowed additive changes through unannotated SELECT * chains must fail during dev apply,
    leave prod untouched, and clean up the temporary __dev preview table."""
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "up.sql").write_text(
        """
        MODEL (name db.up, kind FULL);
        SELECT * FROM raw_src;
        """
    )
    (models_dir / "down.sql").write_text(
        """
        MODEL (
            name db.down,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                forward_only true,
                on_additive_change error
            ),
            start '2023-01-01'
        );
        SELECT * FROM db.up WHERE ds BETWEEN @start_ds AND @end_ds;
        """
    )

    context = _make_duckdb_context(tmp_path)
    failed_models_spy = mocker.spy(context.console, "log_failed_models")
    assert context.engine_adapter.SUPPORTS_CLONING is False
    assert context.get_model("db.up").columns_to_types is None
    assert context.get_model("db.down").columns_to_types is None

    context.engine_adapter.execute(
        "CREATE TABLE raw_src AS SELECT 1 AS id, 'x' AS b, '2023-01-01' AS ds"
    )
    context.plan("prod", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "b", "ds"}

    # 1. Drop column 'b' in raw_src and touch db.up -> dev apply fails, prod intact, __dev dropped
    context.engine_adapter.execute("ALTER TABLE raw_src DROP COLUMN b")
    (models_dir / "up.sql").write_text(
        """
        MODEL (name db.up, kind FULL);
        SELECT * FROM raw_src WHERE id > 0;
        """
    )
    context.load()

    failed_models_spy.reset_mock()
    with pytest.raises(PlanError):
        context.plan("dev", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    _assert_failed_on_db_down(failed_models_spy, DestructiveChangeError)

    down_snapshot = context.get_snapshot("db.down", raise_if_missing=True)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "b", "ds"}
    prod_env = context.state_reader.get_environment("prod")
    assert prod_env is not None and prod_env.finalized_ts is not None
    assert not context.engine_adapter.table_exists(down_snapshot.table_name(is_deployable=False))

    # 2. Add column 'c' in raw_src with on_additive_change=error -> dev apply fails and drops __dev
    context.engine_adapter.execute(
        "CREATE OR REPLACE TABLE raw_src AS SELECT 1 AS id, 'x' AS b, 2 AS c, '2023-01-01' AS ds"
    )
    (models_dir / "up.sql").write_text(
        """
        MODEL (name db.up, kind FULL);
        SELECT * FROM raw_src WHERE id >= 1;
        """
    )
    context.load()

    failed_models_spy.reset_mock()
    with pytest.raises(PlanError):
        context.plan("dev", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    _assert_failed_on_db_down(failed_models_spy, AdditiveChangeError)

    down_snapshot_add = context.get_snapshot("db.down", raise_if_missing=True)
    assert not context.engine_adapter.table_exists(
        down_snapshot_add.table_name(is_deployable=False)
    )
    assert set(context.engine_adapter.columns("db.down")) == {"id", "b", "ds"}

    # 3. Re-apply destructive drop with allow_destructive_models -> succeeds in dev AND prod
    context.engine_adapter.execute(
        "CREATE OR REPLACE TABLE raw_src AS SELECT 1 AS id, '2023-01-01' AS ds"
    )
    (models_dir / "up.sql").write_text(
        """
        MODEL (name db.up, kind FULL);
        SELECT * FROM raw_src WHERE id = 1;
        """
    )
    context.load()

    context.plan(
        "dev",
        execution_time="2023-01-02",
        auto_apply=True,
        no_prompts=True,
        allow_destructive_models=["db.up+"],
    )
    assert set(context.engine_adapter.columns("db__dev.down")) == {"id", "ds"}

    context.plan(
        "prod",
        execution_time="2023-01-02",
        auto_apply=True,
        no_prompts=True,
        allow_destructive_models=["db.down"],
    )
    assert set(context.engine_adapter.columns("db.down")) == {"id", "ds"}


def test_unannotated_python_model_downstream_forward_only_destructive_caught_in_dev(
    tmp_path: Path, mocker: MockerFixture
):
    """Gap S6 (Python @model without columns metadata): Dynamic Python SQL model upstream of a
    forward_only=True SELECT * model surfaces destructive column drops during dev apply."""
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "py_up.py").write_text(
        """
from sqlglot import parse_one
from sqlmesh import model

@model("db.py_up", is_sql=True, kind="full")
def execute(evaluator):
    if evaluator.runtime_stage == "loading":
        return parse_one("SELECT * FROM raw_src")
    return parse_one("SELECT 1 AS id, 'x' AS b, '2023-01-01' AS ds")
"""
    )
    (models_dir / "down.sql").write_text(
        """
        MODEL (
            name db.down,
            kind INCREMENTAL_BY_TIME_RANGE (time_column ds, forward_only true),
            start '2023-01-01'
        );
        SELECT * FROM db.py_up WHERE ds BETWEEN @start_ds AND @end_ds;
        """
    )

    context = _make_duckdb_context(tmp_path)
    failed_models_spy = mocker.spy(context.console, "log_failed_models")
    assert context.get_model("db.py_up").columns_to_types is None
    assert context.get_model("db.down").columns_to_types is None

    context.plan("prod", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "b", "ds"}

    # Drop column 'b' in db.py_up
    (models_dir / "py_up.py").write_text(
        """
from sqlglot import parse_one
from sqlmesh import model

@model("db.py_up", is_sql=True, kind="full")
def execute(evaluator):
    if evaluator.runtime_stage == "loading":
        return parse_one("SELECT * FROM raw_src")
    return parse_one("SELECT 1 AS id, '2023-01-01' AS ds")
"""
    )
    context.load()

    failed_models_spy.reset_mock()
    with pytest.raises(PlanError):
        context.plan("dev", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    _assert_failed_on_db_down(failed_models_spy, DestructiveChangeError)

    down_snapshot = context.get_snapshot("db.down", raise_if_missing=True)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "b", "ds"}
    assert not context.engine_adapter.table_exists(down_snapshot.table_name(is_deployable=False))

    # Allowing db.down succeeds in both dev and prod
    context.plan(
        "dev",
        execution_time="2023-01-02",
        auto_apply=True,
        no_prompts=True,
        allow_destructive_models=["db.down"],
    )
    context.plan(
        "prod",
        execution_time="2023-01-02",
        auto_apply=True,
        no_prompts=True,
        allow_destructive_models=["db.down"],
    )
    assert set(context.engine_adapter.columns("db.down")) == {"id", "ds"}


def test_forward_only_dev_plan_destructive_change_blocks_prod_plan_until_allowed(
    tmp_path: Path,
):
    """Gap S9 end-to-end: Applying a destructive change in dev via --forward-only
    --allow-destructive-model stores a forward_only=True snapshot in state. A subsequent prod plan
    without --allow-destructive-model must fail immediately at plan_builder.build() before touching
    prod, and then succeed once allow_destructive_models is provided."""
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "m.sql").write_text(
        """
        MODEL (
            name db.m,
            kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
            start '2023-01-01'
        );
        SELECT 1::INT AS id, 'x'::TEXT AS val, '2023-01-01'::DATE AS ds;
        """
    )

    context = _make_duckdb_context(tmp_path)
    context.plan("prod", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    assert context.engine_adapter.columns("db.m")["val"] == exp.DataType.build("TEXT")

    # Destructive column type change TEXT -> INT applied to dev as --forward-only
    (models_dir / "m.sql").write_text(
        """
        MODEL (
            name db.m,
            kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
            start '2023-01-01'
        );
        SELECT 1::INT AS id, 1::INT AS val, '2023-01-01'::DATE AS ds;
        """
    )
    context.load()
    context.plan(
        "dev",
        execution_time="2023-01-02",
        forward_only=True,
        allow_destructive_models=["db.m"],
        auto_apply=True,
        no_prompts=True,
    )

    # Prod plan without allow_destructive_models MUST fail at PlanBuilder.build() before touching prod
    with pytest.raises(
        PlanError, match=r"Plan requires a destructive change to a forward-only model\."
    ):
        context.plan_builder("prod", execution_time="2023-01-02").build()

    prod_env = context.state_reader.get_environment("prod")
    assert prod_env is not None and prod_env.finalized_ts is not None
    assert context.engine_adapter.columns("db.m")["val"] == exp.DataType.build("TEXT")

    # Prod plan WITH allow_destructive_models succeeds
    context.plan(
        "prod",
        execution_time="2023-01-02",
        allow_destructive_models=["db.m"],
        auto_apply=True,
        no_prompts=True,
    )
    assert context.engine_adapter.columns("db.m")["val"] == exp.DataType.build("INT")


def test_drop_after_ignored_additive_change_still_caught_by_physical_validation(
    tmp_path: Path, mocker: MockerFixture
):
    """The static plan check defers drops when the previous definition ignored additive changes
    (the physical table may not have those columns), but dropping a column that really exists
    physically must still be rejected by the physical validation during dev apply."""
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)

    def write_model(columns: str, *, on_additive: str) -> None:
        (models_dir / "down.sql").write_text(
            f"""
            MODEL (
                name db.down,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change error,
                    on_additive_change {on_additive}
                ),
                start '2023-01-01'
            );
            SELECT {columns} FROM raw_src WHERE ds BETWEEN @start_ds AND @end_ds;
            """
        )

    write_model("id::INT AS id, val::TEXT AS val, ds::DATE AS ds", on_additive="error")
    context = _make_duckdb_context(tmp_path)
    failed_models_spy = mocker.spy(context.console, "log_failed_models")
    context.engine_adapter.execute(
        "CREATE TABLE raw_src AS SELECT 1 AS id, 'x' AS val, 2 AS c, '2023-01-01' AS ds"
    )
    context.plan("prod", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "val", "ds"}

    # Adds `c` under on_additive_change=ignore: the physical table never gets it.
    write_model(
        "id::INT AS id, val::TEXT AS val, ds::DATE AS ds, c::INT AS c", on_additive="ignore"
    )
    context.load()
    context.plan("prod", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "val", "ds"}

    # Drops `c` (never physical, deferred) and `val` (physical, must be rejected).
    write_model("id::INT AS id, ds::DATE AS ds", on_additive="ignore")
    context.load()
    failed_models_spy.reset_mock()
    with pytest.raises(PlanError):
        context.plan("dev", execution_time="2023-01-02", auto_apply=True, no_prompts=True)
    _assert_failed_on_db_down(failed_models_spy, DestructiveChangeError)
    assert set(context.engine_adapter.columns("db.down")) == {"id", "val", "ds"}
    down_snapshot = context.get_snapshot("db.down", raise_if_missing=True)
    assert not context.engine_adapter.table_exists(down_snapshot.table_name(is_deployable=False))
