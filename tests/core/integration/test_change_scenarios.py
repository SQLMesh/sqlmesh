from __future__ import annotations

import typing as t
import json
from datetime import timedelta
from unittest import mock
import pandas as pd  # noqa: TID253
import pytest
from pathlib import Path
from sqlmesh.core.model.common import ParsableSql
import time_machine
from sqlglot.expressions import DataType
import re

from sqlmesh.cli.project_init import init_example_project
from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.core.config import (
    AutoCategorizationMode,
    Config,
    GatewayConfig,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
)
from sqlmesh.core.context import Context
from sqlmesh.core.config.categorizer import CategorizerConfig
from sqlmesh.core.model import (
    FullKind,
    ModelKind,
    ModelKindName,
    SqlModel,
    PythonModel,
    ViewKind,
    load_sql_based_model,
)
from sqlmesh.core.model.kind import model_kind_type_from_name
from sqlmesh.core.plan import Plan, SnapshotIntervals
from sqlmesh.core.snapshot import (
    SnapshotChangeCategory,
)
from sqlmesh.utils.date import now, to_timestamp
from sqlmesh.utils.errors import (
    SQLMeshError,
)
from tests.core.integration.utils import (
    apply_to_environment,
    add_projection_to_model,
    initial_add,
    change_data_type,
    validate_apply_basics,
    change_model_kind,
    validate_model_kind_change,
    validate_query_change,
    validate_plan_changes,
)

pytestmark = pytest.mark.slow


def test_auto_categorization(sushi_context: Context):
    environment = "dev"
    for config in sushi_context.configs.values():
        config.plan.auto_categorize_changes.sql = AutoCategorizationMode.FULL
    initial_add(sushi_context, environment)

    version = sushi_context.get_snapshot(
        "sushi.waiter_as_customer_by_day", raise_if_missing=True
    ).version
    fingerprint = sushi_context.get_snapshot(
        "sushi.waiter_as_customer_by_day", raise_if_missing=True
    ).fingerprint

    model = t.cast(SqlModel, sushi_context.get_model("sushi.customers", raise_if_missing=True))
    sushi_context.upsert_model(
        "sushi.customers",
        query_=ParsableSql(sql=model.query.select("'foo' AS foo").sql(dialect=model.dialect)),  # type: ignore
    )
    apply_to_environment(sushi_context, environment)

    assert (
        sushi_context.get_snapshot(
            "sushi.waiter_as_customer_by_day", raise_if_missing=True
        ).change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert (
        sushi_context.get_snapshot(
            "sushi.waiter_as_customer_by_day", raise_if_missing=True
        ).fingerprint
        != fingerprint
    )
    assert (
        sushi_context.get_snapshot("sushi.waiter_as_customer_by_day", raise_if_missing=True).version
        == version
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_breaking_only_impacts_immediate_children(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")
    context.upsert_model(context.get_model("sushi.top_waiters").copy(update={"kind": FullKind()}))
    context.plan("prod", skip_tests=True, auto_apply=True, no_prompts=True)

    breaking_model = context.get_model("sushi.orders")
    breaking_model = breaking_model.copy(update={"stamp": "force new version"})
    context.upsert_model(breaking_model)
    breaking_snapshot = context.get_snapshot(breaking_model, raise_if_missing=True)

    non_breaking_model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, non_breaking_model)))
    non_breaking_snapshot = context.get_snapshot(non_breaking_model, raise_if_missing=True)
    top_waiter_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan_builder = context.plan_builder("dev", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(breaking_snapshot, SnapshotChangeCategory.BREAKING)
    plan = plan_builder.build()
    assert (
        plan.context_diff.snapshots[breaking_snapshot.snapshot_id].change_category
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
    assert plan.start == to_timestamp("2023-01-01")
    assert not any(i.snapshot_id == top_waiter_snapshot.snapshot_id for i in plan.missing_intervals)

    context.apply(plan)
    assert (
        not context.plan_builder("dev", skip_tests=True, enable_preview=False)
        .build()
        .requires_backfill
    )

    # Deploy everything to prod.
    plan = context.plan_builder("prod", skip_tests=True).build()
    assert not plan.missing_intervals

    context.apply(plan)
    assert (
        not context.plan_builder("prod", skip_tests=True, enable_preview=False)
        .build()
        .requires_backfill
    )


@pytest.mark.parametrize(
    "context_fixture",
    ["sushi_context", "sushi_dbt_context", "sushi_test_dbt_context", "sushi_no_default_catalog"],
)
def test_model_add(context_fixture: Context, request):
    initial_add(request.getfixturevalue(context_fixture), "dev")


def test_model_removed(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)

    top_waiters_snapshot_id = sushi_context.get_snapshot(
        "sushi.top_waiters", raise_if_missing=True
    ).snapshot_id

    sushi_context._models.pop('"memory"."sushi"."top_waiters"')

    def _validate_plan(context, plan):
        validate_plan_changes(plan, removed=[top_waiters_snapshot_id])
        assert not plan.missing_intervals

    def _validate_apply(context):
        assert not sushi_context.get_snapshot("sushi.top_waiters", raise_if_missing=False)
        assert sushi_context.state_reader.get_snapshots([top_waiters_snapshot_id])
        env = sushi_context.state_reader.get_environment(environment)
        assert env
        assert all(snapshot.name != '"memory"."sushi"."top_waiters"' for snapshot in env.snapshots)

    apply_to_environment(
        sushi_context,
        environment,
        SnapshotChangeCategory.BREAKING,
        plan_validators=[_validate_plan],
        apply_validators=[_validate_apply],
    )


def test_non_breaking_change(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    validate_query_change(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING, False)


def test_breaking_change(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    validate_query_change(sushi_context, environment, SnapshotChangeCategory.BREAKING, False)


def test_logical_change(sushi_context: Context):
    environment = "dev"
    initial_add(sushi_context, environment)
    previous_sushi_items_version = sushi_context.get_snapshot(
        "sushi.items", raise_if_missing=True
    ).version

    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.FLOAT,
        DataType.Type.DOUBLE,
    )
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    assert (
        sushi_context.get_snapshot("sushi.items", raise_if_missing=True).version
        == previous_sushi_items_version
    )


@pytest.mark.parametrize(
    "from_, to",
    [
        (ModelKindName.INCREMENTAL_BY_TIME_RANGE, ModelKindName.FULL),
        (ModelKindName.FULL, ModelKindName.INCREMENTAL_BY_TIME_RANGE),
    ],
)
def test_model_kind_change(from_: ModelKindName, to: ModelKindName, sushi_context: Context):
    environment = f"test_model_kind_change__{from_.value.lower()}__{to.value.lower()}"
    incremental_snapshot = sushi_context.get_snapshot("sushi.items", raise_if_missing=True).copy()

    if from_ != ModelKindName.INCREMENTAL_BY_TIME_RANGE:
        change_model_kind(sushi_context, from_)
        apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    if to == ModelKindName.INCREMENTAL_BY_TIME_RANGE:
        sushi_context.upsert_model(incremental_snapshot.model)
    else:
        change_model_kind(sushi_context, to)

    logical = to in (ModelKindName.INCREMENTAL_BY_TIME_RANGE, ModelKindName.EMBEDDED)
    validate_model_kind_change(to, sushi_context, environment, logical=logical)


def test_environment_isolation(sushi_context: Context):
    prod_snapshots = sushi_context.snapshots.values()

    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    directly_modified = ['"memory"."sushi"."items"']
    indirectly_modified = [
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."waiter_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_lifetime"',
        '"memory"."sushi"."top_waiters"',
        "assert_item_price_above_zero",
    ]

    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.BREAKING)

    # Verify prod unchanged
    validate_apply_basics(sushi_context, "prod", prod_snapshots)

    def _validate_plan(context, plan):
        validate_plan_changes(plan, modified=directly_modified + indirectly_modified)
        assert not plan.missing_intervals

    apply_to_environment(
        sushi_context,
        "prod",
        SnapshotChangeCategory.BREAKING,
        plan_validators=[_validate_plan],
    )


def test_environment_promotion(sushi_context: Context):
    initial_add(sushi_context, "dev")

    # Simulate prod "ahead"
    change_data_type(sushi_context, "sushi.items", DataType.Type.DOUBLE, DataType.Type.FLOAT)
    apply_to_environment(sushi_context, "prod", SnapshotChangeCategory.BREAKING)

    # Simulate rebase
    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.BREAKING)

    # Make changes in dev
    change_data_type(sushi_context, "sushi.items", DataType.Type.FLOAT, DataType.Type.DECIMAL)
    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.NON_BREAKING)

    change_data_type(sushi_context, "sushi.top_waiters", DataType.Type.DOUBLE, DataType.Type.INT)
    apply_to_environment(sushi_context, "dev", SnapshotChangeCategory.BREAKING)

    change_data_type(
        sushi_context,
        "sushi.customer_revenue_by_day",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    apply_to_environment(
        sushi_context,
        "dev",
        SnapshotChangeCategory.FORWARD_ONLY,
        allow_destructive_models=['"memory"."sushi"."customer_revenue_by_day"'],
    )

    # Promote to prod
    def _validate_plan(context, plan):
        sushi_items_snapshot = context.get_snapshot("sushi.items", raise_if_missing=True)
        sushi_top_waiters_snapshot = context.get_snapshot(
            "sushi.top_waiters", raise_if_missing=True
        )
        sushi_customer_revenue_by_day_snapshot = context.get_snapshot(
            "sushi.customer_revenue_by_day", raise_if_missing=True
        )

        assert (
            plan.context_diff.modified_snapshots[sushi_items_snapshot.name][0].change_category
            == SnapshotChangeCategory.NON_BREAKING
        )
        assert (
            plan.context_diff.modified_snapshots[sushi_top_waiters_snapshot.name][0].change_category
            == SnapshotChangeCategory.BREAKING
        )
        assert (
            plan.context_diff.modified_snapshots[sushi_customer_revenue_by_day_snapshot.name][
                0
            ].change_category
            == SnapshotChangeCategory.NON_BREAKING
        )
        assert plan.context_diff.snapshots[
            sushi_customer_revenue_by_day_snapshot.snapshot_id
        ].is_forward_only

    apply_to_environment(
        sushi_context,
        "prod",
        SnapshotChangeCategory.NON_BREAKING,
        plan_validators=[_validate_plan],
        allow_destructive_models=['"memory"."sushi"."customer_revenue_by_day"'],
    )


def test_no_override(sushi_context: Context) -> None:
    change_data_type(
        sushi_context,
        "sushi.items",
        DataType.Type.INT,
        DataType.Type.BIGINT,
    )

    change_data_type(
        sushi_context,
        "sushi.order_items",
        DataType.Type.INT,
        DataType.Type.BIGINT,
    )

    plan_builder = sushi_context.plan_builder("prod")
    plan = plan_builder.build()

    sushi_items_snapshot = sushi_context.get_snapshot("sushi.items", raise_if_missing=True)
    sushi_order_items_snapshot = sushi_context.get_snapshot(
        "sushi.order_items", raise_if_missing=True
    )
    sushi_water_revenue_by_day_snapshot = sushi_context.get_snapshot(
        "sushi.waiter_revenue_by_day", raise_if_missing=True
    )

    items = plan.context_diff.snapshots[sushi_items_snapshot.snapshot_id]
    order_items = plan.context_diff.snapshots[sushi_order_items_snapshot.snapshot_id]
    waiter_revenue = plan.context_diff.snapshots[sushi_water_revenue_by_day_snapshot.snapshot_id]

    plan_builder.set_choice(items, SnapshotChangeCategory.BREAKING).set_choice(
        order_items, SnapshotChangeCategory.NON_BREAKING
    )
    plan_builder.build()
    assert items.is_new_version
    assert waiter_revenue.is_new_version
    plan_builder.set_choice(items, SnapshotChangeCategory.NON_BREAKING)
    plan_builder.build()
    assert not waiter_revenue.is_new_version


@pytest.mark.parametrize(
    "change_categories, expected",
    [
        ([SnapshotChangeCategory.NON_BREAKING], SnapshotChangeCategory.BREAKING),
        ([SnapshotChangeCategory.BREAKING], SnapshotChangeCategory.BREAKING),
        (
            [SnapshotChangeCategory.NON_BREAKING, SnapshotChangeCategory.NON_BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
        (
            [SnapshotChangeCategory.NON_BREAKING, SnapshotChangeCategory.BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
        (
            [SnapshotChangeCategory.BREAKING, SnapshotChangeCategory.NON_BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
        (
            [SnapshotChangeCategory.BREAKING, SnapshotChangeCategory.BREAKING],
            SnapshotChangeCategory.BREAKING,
        ),
    ],
)
def test_revert(
    sushi_context: Context,
    change_categories: t.List[SnapshotChangeCategory],
    expected: SnapshotChangeCategory,
):
    environment = "prod"
    original_snapshot_id = sushi_context.get_snapshot("sushi.items", raise_if_missing=True)

    types = (DataType.Type.DOUBLE, DataType.Type.FLOAT, DataType.Type.DECIMAL)
    assert len(change_categories) < len(types)

    for i, category in enumerate(change_categories):
        change_data_type(sushi_context, "sushi.items", *types[i : i + 2])
        apply_to_environment(sushi_context, environment, category)
        assert (
            sushi_context.get_snapshot("sushi.items", raise_if_missing=True) != original_snapshot_id
        )

    change_data_type(sushi_context, "sushi.items", types[len(change_categories)], types[0])

    def _validate_plan(_, plan):
        snapshot = next(s for s in plan.snapshots.values() if s.name == '"memory"."sushi"."items"')
        assert snapshot.change_category == expected
        assert not plan.missing_intervals

    apply_to_environment(
        sushi_context,
        environment,
        change_categories[-1],
        plan_validators=[_validate_plan],
    )
    assert sushi_context.get_snapshot("sushi.items", raise_if_missing=True) == original_snapshot_id


def test_revert_after_downstream_change(sushi_context: Context):
    environment = "prod"
    change_data_type(sushi_context, "sushi.items", DataType.Type.DOUBLE, DataType.Type.FLOAT)
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.BREAKING)

    change_data_type(
        sushi_context,
        "sushi.waiter_revenue_by_day",
        DataType.Type.DOUBLE,
        DataType.Type.FLOAT,
    )
    apply_to_environment(sushi_context, environment, SnapshotChangeCategory.NON_BREAKING)

    change_data_type(sushi_context, "sushi.items", DataType.Type.FLOAT, DataType.Type.DOUBLE)

    def _validate_plan(_, plan):
        snapshot = next(s for s in plan.snapshots.values() if s.name == '"memory"."sushi"."items"')
        assert snapshot.change_category == SnapshotChangeCategory.BREAKING
        assert plan.missing_intervals

    apply_to_environment(
        sushi_context,
        environment,
        SnapshotChangeCategory.BREAKING,
        plan_validators=[_validate_plan],
    )


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
@pytest.mark.parametrize("forward_only", [False, True])
def test_plan_repairs_unrenderable_snapshot_state(
    init_and_plan_context: t.Callable, forward_only: bool
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    target_snapshot = context.get_snapshot("sushi.waiter_revenue_by_day")
    assert target_snapshot

    # Manually corrupt the snapshot's query
    raw_snapshot = context.state_sync.state_sync.engine_adapter.fetchone(
        f"SELECT snapshot FROM sqlmesh._snapshots WHERE name = '{target_snapshot.name}' AND identifier = '{target_snapshot.identifier}'"
    )[0]  # type: ignore
    parsed_snapshot = json.loads(raw_snapshot)
    parsed_snapshot["node"]["query"] = "SELECT @missing_macro()"
    context.state_sync.state_sync.engine_adapter.update_table(
        "sqlmesh._snapshots",
        {"snapshot": json.dumps(parsed_snapshot)},
        f"name = '{target_snapshot.name}' AND identifier = '{target_snapshot.identifier}'",
    )

    context.clear_caches()
    target_snapshot_in_state = context.state_sync.get_snapshots([target_snapshot.snapshot_id])[
        target_snapshot.snapshot_id
    ]

    with pytest.raises(Exception):
        target_snapshot_in_state.model.render_query_or_raise()

    # Repair the snapshot by creating a new version of it
    context.upsert_model(target_snapshot.model.name, stamp="repair")
    target_snapshot = context.get_snapshot(target_snapshot.name)

    plan_builder = context.plan_builder("prod", forward_only=forward_only)
    plan = plan_builder.build()
    if not forward_only:
        assert target_snapshot.snapshot_id in {i.snapshot_id for i in plan.missing_intervals}
    assert plan.directly_modified == {target_snapshot.snapshot_id}
    plan_builder.set_choice(target_snapshot, SnapshotChangeCategory.NON_BREAKING)
    plan = plan_builder.build()

    context.apply(plan)

    context.clear_caches()
    assert context.get_snapshot(target_snapshot.name).model.render_query_or_raise()
    target_snapshot_in_state = context.state_sync.get_snapshots([target_snapshot.snapshot_id])[
        target_snapshot.snapshot_id
    ]
    assert target_snapshot_in_state.model.render_query_or_raise()


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_no_backfill_for_model_downstream_of_metadata_change(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    # Make sushi.waiter_revenue_by_day a forward-only model.
    forward_only_model = context.get_model("sushi.waiter_revenue_by_day")
    updated_model_kind = forward_only_model.kind.copy(update={"forward_only": True})
    forward_only_model = forward_only_model.copy(update={"kind": updated_model_kind})
    context.upsert_model(forward_only_model)

    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)

    # Make a metadata change upstream of the forward-only model.
    context.upsert_model("sushi.orders", owner="new_owner")

    plan = context.plan_builder("test_dev").build()
    assert plan.has_changes
    assert not plan.directly_modified
    assert not plan.indirectly_modified
    assert not plan.missing_intervals
    assert all(
        snapshot.change_category == SnapshotChangeCategory.METADATA
        for snapshot in plan.new_snapshots
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_plan_set_choice_is_reflected_in_missing_intervals(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")
    context.upsert_model(context.get_model("sushi.top_waiters").copy(update={"kind": FullKind()}))
    context.plan("prod", skip_tests=True, no_prompts=True, auto_apply=True)

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))
    snapshot = context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    plan_builder = context.plan_builder("dev", skip_tests=True)
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
    assert plan.start == to_timestamp("2023-01-01")
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
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

    # Change the category to BREAKING
    plan = plan_builder.set_choice(
        plan.context_diff.snapshots[snapshot.snapshot_id], SnapshotChangeCategory.BREAKING
    ).build()
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_BREAKING
    )
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
            snapshot_id=snapshot.snapshot_id,
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

    # Change the category back to NON_BREAKING
    plan = plan_builder.set_choice(
        plan.context_diff.snapshots[snapshot.snapshot_id], SnapshotChangeCategory.NON_BREAKING
    ).build()
    assert (
        plan.context_diff.snapshots[snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.NON_BREAKING
    )
    assert (
        plan.context_diff.snapshots[top_waiters_snapshot.snapshot_id].change_category
        == SnapshotChangeCategory.INDIRECT_NON_BREAKING
    )
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=snapshot.snapshot_id,
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

    dev_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi__dev.waiter_revenue_by_day ORDER BY event_date"
    )
    assert dev_df["event_date"].tolist() == [
        pd.to_datetime(x)
        for x in [
            "2023-01-01",
            "2023-01-02",
            "2023-01-03",
            "2023-01-04",
            "2023-01-05",
            "2023-01-06",
            "2023-01-07",
        ]
    ]

    # Promote changes to prod
    prod_plan = context.plan_builder(skip_tests=True).build()
    assert not prod_plan.missing_intervals

    context.apply(prod_plan)
    prod_df = context.engine_adapter.fetchdf(
        "SELECT DISTINCT event_date FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL ORDER BY event_date"
    )
    assert prod_df["event_date"].tolist() == [
        pd.to_datetime(x)
        for x in [
            "2023-01-01",
            "2023-01-02",
            "2023-01-03",
            "2023-01-04",
            "2023-01-05",
            "2023-01-06",
            "2023-01-07",
        ]
    ]


def test_plan_production_environment_statements(tmp_path: Path):
    model_a = """
    MODEL (
        name test_schema.a,
        kind FULL,
    );

    @IF(
        @runtime_stage IN ('evaluating', 'creating'),
        INSERT INTO schema_names_for_prod (physical_schema_name) VALUES (@resolve_template('@{schema_name}'))
    );

    SELECT 1 AS account_id
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    for path, defn in {"a.sql": model_a}.items():
        with open(models_dir / path, "w") as f:
            f.write(defn)

    before_all = [
        "CREATE TABLE IF NOT EXISTS schema_names_for_@this_env (physical_schema_name VARCHAR)",
        "@IF(@runtime_stage = 'before_all', CREATE TABLE IF NOT EXISTS should_create AS SELECT @runtime_stage)",
    ]
    after_all = [
        "@IF(@this_env = 'prod', CREATE TABLE IF NOT EXISTS after_t AS SELECT @var_5)",
        "@IF(@runtime_stage = 'before_all', CREATE TABLE IF NOT EXISTS not_create AS SELECT @runtime_stage)",
    ]
    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        before_all=before_all,
        after_all=after_all,
        variables={"var_5": 5},
    )
    ctx = Context(paths=[tmp_path], config=config)
    ctx.plan(auto_apply=True, no_prompts=True)

    before_t = ctx.fetchdf("select * from schema_names_for_prod").to_dict()
    assert before_t["physical_schema_name"][0] == "sqlmesh__test_schema"

    after_t = ctx.fetchdf("select * from after_t").to_dict()
    assert after_t["5"][0] == 5

    environment_statements = ctx.state_reader.get_environment_statements(c.PROD)
    assert environment_statements[0].before_all == before_all
    assert environment_statements[0].after_all == after_all
    assert environment_statements[0].python_env.keys() == {"__sqlmesh__vars__"}
    assert environment_statements[0].python_env["__sqlmesh__vars__"].payload == "{'var_5': 5}"

    should_create = ctx.fetchdf("select * from should_create").to_dict()
    assert should_create["before_all"][0] == "before_all"

    with pytest.raises(
        Exception, match=r"Catalog Error: Table with name not_create does not exist!"
    ):
        ctx.fetchdf("select * from not_create")


def test_environment_statements_error_handling(tmp_path: Path):
    model_a = """
    MODEL (
        name test_schema.a,
        kind FULL,
    );

    SELECT 1 AS account_id
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    for path, defn in {"a.sql": model_a}.items():
        with open(models_dir / path, "w") as f:
            f.write(defn)

    before_all = [
        "CREATE TABLE identical_table (physical_schema_name VARCHAR)",
        "CREATE TABLE identical_table (physical_schema_name VARCHAR)",
    ]

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        before_all=before_all,
    )
    ctx = Context(paths=[tmp_path], config=config)

    expected_error_message = re.escape(
        """An error occurred during execution of the following 'before_all' statement:

CREATE TABLE identical_table (physical_schema_name TEXT)

Catalog Error: Table with name "identical_table" already exists!"""
    )

    with pytest.raises(SQLMeshError, match=expected_error_message):
        ctx.plan(auto_apply=True, no_prompts=True)

    after_all = [
        "@bad_macro()",
    ]

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        after_all=after_all,
    )
    ctx = Context(paths=[tmp_path], config=config)

    expected_error_message = re.escape(
        """An error occurred during rendering of the 'after_all' statements:

Failed to resolve macros for

@bad_macro()

Macro 'bad_macro' does not exist."""
    )

    with pytest.raises(SQLMeshError, match=expected_error_message):
        ctx.plan(auto_apply=True, no_prompts=True)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_full_model_change_with_plan_start_not_matching_model_start(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.top_waiters")
    context.upsert_model(model, kind=model_kind_type_from_name("FULL")())  # type: ignore

    # Apply the change with --skip-backfill first and no plan start
    context.plan("dev", skip_tests=True, skip_backfill=True, no_prompts=True, auto_apply=True)

    # Apply the plan again but this time don't skip backfill and set start
    # to be later than the model start
    context.plan("dev", skip_tests=True, no_prompts=True, auto_apply=True, start="1 day ago")

    # Check that the number of rows is not 0
    row_num = context.engine_adapter.fetchone(f"SELECT COUNT(*) FROM sushi__dev.top_waiters")[0]
    assert row_num > 0


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_hourly_model_with_lookback_no_backfill_in_dev(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")

    model_name = "sushi.waiter_revenue_by_day"

    model = context.get_model(model_name)
    model = SqlModel.parse_obj(
        {
            **model.dict(),
            "kind": model.kind.copy(update={"lookback": 1}),
            "cron": "@hourly",
            "audits": [],
        }
    )
    context.upsert_model(model)

    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    top_waiters_model = context.get_model("sushi.top_waiters")
    top_waiters_model = add_projection_to_model(t.cast(SqlModel, top_waiters_model), literal=True)
    context.upsert_model(top_waiters_model)

    context.get_snapshot(model, raise_if_missing=True)
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)

    with time_machine.travel(now() + timedelta(hours=2)):
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


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_max_interval_end_per_model_not_applied_when_end_is_provided(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    with time_machine.travel("2023-01-09 00:00:00 UTC"):
        context.run()

        plan = context.plan_builder(
            restate_models=["*"], start="2023-01-09", end="2023-01-09"
        ).build()
        context.apply(plan)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_plan_against_expired_environment(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    modified_models = {model.fqn, context.get_model("sushi.top_waiters").fqn}

    plan = context.plan_builder("dev").build()
    assert plan.has_changes
    assert set(plan.context_diff.modified_snapshots) == modified_models
    assert plan.missing_intervals
    context.apply(plan)

    # Make sure there are no changes when comparing against the existing environment.
    plan = context.plan_builder("dev").build()
    assert not plan.has_changes
    assert not plan.context_diff.modified_snapshots
    assert not plan.missing_intervals

    # Invalidate the environment and make sure that the plan detects the changes.
    context.invalidate_environment("dev")
    plan = context.plan_builder("dev").build()
    assert plan.has_changes
    assert set(plan.context_diff.modified_snapshots) == modified_models
    assert not plan.missing_intervals
    context.apply(plan)


def test_plan_environment_statements_doesnt_cause_extra_diff(tmp_path: Path):
    model_a = """
    MODEL (
        name test_schema.a,
        kind FULL,
    );

    SELECT 1;
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    (models_dir / "a.sql").write_text(model_a)

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        before_all=["select 1 as before_all"],
        after_all=["select 2 as after_all"],
    )
    ctx = Context(paths=[tmp_path], config=config)

    # first plan - should apply changes
    assert ctx.plan(auto_apply=True, no_prompts=True).has_changes

    # second plan - nothing has changed so should report no changes
    assert not ctx.plan(auto_apply=True, no_prompts=True).has_changes


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_plan_snapshot_table_exists_for_promoted_snapshot(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    context.plan("dev", auto_apply=True, no_prompts=True, skip_tests=True)

    # Drop the views and make sure SQLMesh recreates them later
    top_waiters_snapshot = context.get_snapshot("sushi.top_waiters", raise_if_missing=True)
    context.engine_adapter.drop_view(top_waiters_snapshot.table_name())
    context.engine_adapter.drop_view(top_waiters_snapshot.table_name(False))

    # Make the environment unfinalized to force recreation of all views in the virtual layer
    context.state_sync.state_sync.engine_adapter.execute(
        "UPDATE sqlmesh._environments SET finalized_ts = NULL WHERE name = 'dev'"
    )

    context.plan(
        "prod",
        restate_models=["sushi.top_waiters"],
        auto_apply=True,
        no_prompts=True,
        skip_tests=True,
    )
    assert context.engine_adapter.table_exists(top_waiters_snapshot.table_name())


def test_plan_twice_with_star_macro_yields_no_diff(tmp_path: Path):
    init_example_project(tmp_path, engine_type="duckdb")

    star_model_definition = """
        MODEL (
          name sqlmesh_example.star_model,
          kind FULL
        );

        SELECT @STAR(sqlmesh_example.full_model) FROM sqlmesh_example.full_model
    """

    star_model_path = tmp_path / "models" / "star_model.sql"
    star_model_path.write_text(star_model_definition)

    db_path = str(tmp_path / "db.db")
    config = Config(
        gateways={"main": GatewayConfig(connection=DuckDBConnectionConfig(database=db_path))},
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    context = Context(paths=tmp_path, config=config)
    context.plan(auto_apply=True, no_prompts=True)

    # Instantiate new context to remove caches etc
    new_context = Context(paths=tmp_path, config=config)

    star_model = new_context.get_model("sqlmesh_example.star_model")
    assert (
        star_model.render_query_or_raise().sql()
        == 'SELECT CAST("full_model"."item_id" AS INT) AS "item_id", CAST("full_model"."num_orders" AS BIGINT) AS "num_orders" FROM "db"."sqlmesh_example"."full_model" AS "full_model"'
    )

    new_plan = new_context.plan_builder().build()
    assert not new_plan.has_changes
    assert not new_plan.new_snapshots


class OldPythonModel(PythonModel):
    kind: ModelKind = ViewKind()


def test_python_model_default_kind_change(init_and_plan_context: t.Callable):
    """
    Around 2024-07-17 Python models had their default Kind changed from VIEW to FULL in order to
    avoid some edge cases where the views might not get updated in certain situations.

    This test ensures that if a user had a Python `kind: VIEW` model stored in state,
    it can still be loaded without error and just show as a breaking change from `kind: VIEW`
    to `kind: FULL`
    """

    # note: we deliberately dont specify a Kind here to allow the defaults to be picked up
    python_model_file = """import typing as t
import pandas as pd  # noqa: TID253
from sqlmesh import ExecutionContext, model

@model(
    "sushi.python_view_model",
    columns={
        "id": "int",
    }
)
def execute(
    context: ExecutionContext,
    **kwargs: t.Any,
) -> pd.DataFrame:
    return pd.DataFrame([
        {"id": 1}
    ])
"""

    context: Context
    context, _ = init_and_plan_context("examples/sushi")

    with open(context.path / "models" / "python_view_model.py", mode="w", encoding="utf8") as f:
        f.write(python_model_file)

    # monkey-patch PythonModel to default to kind: View again
    # and ViewKind to allow python models again
    with (
        mock.patch.object(ViewKind, "supports_python_models", return_value=True),
        mock.patch("sqlmesh.core.model.definition.PythonModel", OldPythonModel),
    ):
        context.load()

    # check the monkey-patching worked
    model = context.get_model("sushi.python_view_model")
    assert model.kind.name == ModelKindName.VIEW
    assert model.source_type == "python"

    # apply plan
    plan: Plan = context.plan(auto_apply=True)

    # check that run() still works even though we have a Python model with kind: View in the state
    snapshot_ids = [s for s in plan.directly_modified if "python_view_model" in s.name]
    snapshot_from_state = list(context.state_sync.get_snapshots(snapshot_ids).values())[0]
    assert snapshot_from_state.model.kind.name == ModelKindName.VIEW
    assert snapshot_from_state.model.source_type == "python"
    context.run()

    # reload context to load model with new defaults
    # this also shows the earlier monkey-patching is no longer in effect
    context.load()
    model = context.get_model("sushi.python_view_model")
    assert model.kind.name == ModelKindName.FULL
    assert model.source_type == "python"

    plan = context.plan(
        categorizer_config=CategorizerConfig.all_full()
    )  # the default categorizer_config doesnt auto-categorize python models

    assert plan.has_changes
    assert not plan.indirectly_modified

    assert len(plan.directly_modified) == 1
    snapshot_id = list(plan.directly_modified)[0]
    assert snapshot_id.name == '"memory"."sushi"."python_view_model"'
    assert plan.modified_snapshots[snapshot_id].change_category == SnapshotChangeCategory.BREAKING

    context.apply(plan)

    df = context.engine_adapter.fetchdf("SELECT id FROM sushi.python_view_model")
    assert df["id"].to_list() == [1]


@time_machine.travel("2023-01-08 15:00:00 UTC")
@pytest.mark.parametrize(
    "parent_a_category,parent_b_category,expected_child_category",
    [
        (
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.INDIRECT_BREAKING,
        ),
        (
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.INDIRECT_NON_BREAKING,
        ),
        (
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.INDIRECT_NON_BREAKING,
        ),
        (
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.INDIRECT_BREAKING,
        ),
        (
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.METADATA,
        ),
        (
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.METADATA,
        ),
        (
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.INDIRECT_BREAKING,
        ),
        (
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.INDIRECT_NON_BREAKING,
        ),
        (
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.METADATA,
        ),
    ],
)
def test_rebase_two_changed_parents(
    init_and_plan_context: t.Callable,
    parent_a_category: SnapshotChangeCategory,  # This change is deployed to prod first
    parent_b_category: SnapshotChangeCategory,  # This change is deployed to prod second
    expected_child_category: SnapshotChangeCategory,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    initial_model_a = context.get_model("sushi.orders")
    initial_model_b = context.get_model("sushi.items")

    # Make change A and deploy it to dev_a
    context.upsert_model(initial_model_a.name, stamp="1")
    plan_builder = context.plan_builder("dev_a", skip_tests=True)
    plan_builder.set_choice(context.get_snapshot(initial_model_a.name), parent_a_category)
    context.apply(plan_builder.build())

    # Make change B and deploy it to dev_b
    context.upsert_model(initial_model_a)
    context.upsert_model(initial_model_b.name, stamp="1")
    plan_builder = context.plan_builder("dev_b", skip_tests=True)
    plan_builder.set_choice(context.get_snapshot(initial_model_b.name), parent_b_category)
    context.apply(plan_builder.build())

    # Deploy change A to prod
    context.upsert_model(initial_model_a.name, stamp="1")
    context.upsert_model(initial_model_b)
    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)

    # Apply change B in addition to A and plan against prod
    context.upsert_model(initial_model_b.name, stamp="1")
    plan = context.plan_builder("prod", skip_tests=True).build()

    # Validate the category of child snapshots
    direct_child_snapshot = plan.snapshots[context.get_snapshot("sushi.order_items").snapshot_id]
    assert direct_child_snapshot.change_category == expected_child_category

    indirect_child_snapshot = plan.snapshots[context.get_snapshot("sushi.top_waiters").snapshot_id]
    assert indirect_child_snapshot.change_category == expected_child_category


@pytest.mark.parametrize(
    "context_fixture",
    ["sushi_context", "sushi_no_default_catalog"],
)
def test_unaligned_start_snapshots(context_fixture: Context, request):
    context = request.getfixturevalue(context_fixture)
    environment = "dev"
    apply_to_environment(context, environment)
    # Make breaking change to model upstream of a depends_on_self model
    context.upsert_model("sushi.order_items", stamp="1")
    # Apply the change starting at a date later then the beginning of the downstream depends_on_self model
    plan = apply_to_environment(
        context,
        environment,
        choice=SnapshotChangeCategory.BREAKING,
        plan_start="2 days ago",
        enable_preview=True,
    )
    revenue_lifetime_snapshot = context.get_snapshot(
        "sushi.customer_revenue_lifetime", raise_if_missing=True
    )
    # Validate that the depends_on_self model is non-deployable
    assert not plan.deployability_index.is_deployable(revenue_lifetime_snapshot)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_unaligned_start_snapshot_with_non_deployable_downstream(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    downstream_model_name = "memory.sushi.customer_max_revenue"

    expressions = d.parse(
        f"""
        MODEL (
            name {downstream_model_name},
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

    downstream_model = load_sql_based_model(expressions)
    assert downstream_model.forward_only
    context.upsert_model(downstream_model)

    context.plan(auto_apply=True, no_prompts=True)

    customer_revenue_lifetime_model = context.get_model("sushi.customer_revenue_lifetime")
    kwargs = {
        **customer_revenue_lifetime_model.dict(),
        "name": "memory.sushi.customer_revenue_lifetime_new",
        "kind": dict(
            name="INCREMENTAL_UNMANAGED"
        ),  # Make it incremental unmanaged to ensure the depends_on_past behavior.
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))
    context.upsert_model(
        downstream_model_name,
        query_=ParsableSql(
            sql="SELECT customer_id, MAX(revenue) AS max_revenue FROM memory.sushi.customer_revenue_lifetime_new GROUP BY 1"
        ),
    )

    plan = context.plan_builder("dev", enable_preview=True).build()
    assert {s.name for s in plan.new_snapshots} == {
        '"memory"."sushi"."customer_revenue_lifetime_new"',
        '"memory"."sushi"."customer_max_revenue"',
    }
    for snapshot_interval in plan.missing_intervals:
        assert not plan.deployability_index.is_deployable(snapshot_interval.snapshot_id)
        assert snapshot_interval.intervals[0][0] == to_timestamp("2023-01-07")


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_indirect_non_breaking_view_is_updated_with_new_table_references(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Add a new projection to the base model
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)

    # Run the janitor to delete the old snapshot record
    context.run_janitor(ignore_ttl=True)

    # Check the downstream view and make sure it's still queryable
    assert context.get_model("sushi.top_waiters").kind.is_view
    row_num = context.engine_adapter.fetchone(f"SELECT COUNT(*) FROM sushi.top_waiters")[0]
    assert row_num > 0


@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_annotated_self_referential_model(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    # Projections are fully annotated in the query but columns were not specified explicitly
    expressions = d.parse(
        f"""
        MODEL (
            name memory.sushi.test_self_ref,
            kind FULL,
            start '2023-01-01',
        );

        SELECT 1::INT AS one FROM memory.sushi.test_self_ref;
        """
    )
    model = load_sql_based_model(expressions)
    assert model.depends_on_self
    context.upsert_model(model)

    context.plan("prod", skip_tests=True, no_prompts=True, auto_apply=True)

    df = context.fetchdf("SELECT one FROM memory.sushi.test_self_ref")
    assert len(df) == 0


@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_creating_stage_for_first_batch_only(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    expressions = d.parse(
        """
        MODEL (
            name memory.sushi.test_batch_size,
            kind INCREMENTAL_BY_UNIQUE_KEY (
                unique_key one,
                batch_size 1,
            ),

            start '2023-01-01',
        );

        CREATE SCHEMA IF NOT EXISTS test_schema;
        CREATE TABLE IF NOT EXISTS test_schema.creating_counter (a INT);

        SELECT 1::INT AS one;

        @IF(@runtime_stage = 'creating', INSERT INTO test_schema.creating_counter (a) VALUES (1));
        """
    )
    model = load_sql_based_model(expressions)
    context.upsert_model(model)

    context.plan("prod", skip_tests=True, no_prompts=True, auto_apply=True)
    assert (
        context.engine_adapter.fetchone("SELECT COUNT(*) FROM test_schema.creating_counter")[0] == 1
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
@pytest.mark.parametrize(
    "stored_parent_category, new_parent_category, expected_child_category",
    [
        (
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.INDIRECT_BREAKING,
        ),
        (
            SnapshotChangeCategory.BREAKING,
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.INDIRECT_BREAKING,
        ),
        (
            SnapshotChangeCategory.NON_BREAKING,
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.INDIRECT_NON_BREAKING,
        ),
        (
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.METADATA,
            SnapshotChangeCategory.METADATA,
        ),
    ],
)
def test_stored_changed_parent_with_new_parent(
    init_and_plan_context: t.Callable,
    stored_parent_category: SnapshotChangeCategory,  # Stored in state (dev) but not deployed to prod
    new_parent_category: SnapshotChangeCategory,  # Introduced by the prod plan itself
    expected_child_category: SnapshotChangeCategory,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    initial_model_a = context.get_model("sushi.orders")
    initial_model_b = context.get_model("sushi.items")
    child_before = context.get_snapshot("sushi.order_items", raise_if_missing=True)

    # Make change A and deploy it to dev only. Prod keeps the previous version of the child.
    context.upsert_model(initial_model_a.name, stamp="1")
    parent_a = context.get_snapshot(initial_model_a.name, raise_if_missing=True)
    plan_builder = context.plan_builder("dev", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_a, stored_parent_category)
    context.apply(plan_builder.build())

    # Keep change A and make change B, creating a combination of parents not seen before.
    context.upsert_model(initial_model_b.name, stamp="1")
    parent_b = context.get_snapshot(initial_model_b.name, raise_if_missing=True)
    plan_builder = context.plan_builder("prod", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_b, new_parent_category)
    plan = plan_builder.build()

    # Only B and its downstream snapshots are new in this plan.
    assert parent_a.snapshot_id not in plan.context_diff.new_snapshots
    assert parent_b.snapshot_id in plan.context_diff.new_snapshots
    assert plan.snapshots[parent_a.snapshot_id].change_category == stored_parent_category
    assert plan.snapshots[parent_b.snapshot_id].change_category == new_parent_category

    child_after = context.get_snapshot("sushi.order_items", raise_if_missing=True)
    assert child_after.snapshot_id in plan.context_diff.new_snapshots
    assert plan.snapshots[child_after.snapshot_id].change_category == expected_child_category

    # A breaking change to a parent requires a new version of the child, others reuse the old one.
    child_requires_new_version = expected_child_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert (plan.snapshots[child_after.snapshot_id].version != child_before.version) == (
        child_requires_new_version
    )
    assert any(i.snapshot_id == child_after.snapshot_id for i in plan.missing_intervals) == (
        child_requires_new_version
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_unchanged_historic_breaking_parent_with_new_parent(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    initial_model_b = context.get_model("sushi.items")

    # Deploy a breaking change to the first parent to prod, including its children.
    context.upsert_model("sushi.orders", stamp="1")
    plan_builder = context.plan_builder("prod", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(
        context.get_snapshot("sushi.orders", raise_if_missing=True),
        SnapshotChangeCategory.BREAKING,
    )
    context.apply(plan_builder.build())
    child_before = context.get_snapshot("sushi.order_items", raise_if_missing=True)

    # The first parent is unchanged relative to the deployed child, so its history must be ignored.
    context.upsert_model(initial_model_b.name, stamp="1")
    parent_b = context.get_snapshot(initial_model_b.name, raise_if_missing=True)
    plan_builder = context.plan_builder("prod", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_b, SnapshotChangeCategory.METADATA)
    plan = plan_builder.build()

    child_after = context.get_snapshot("sushi.order_items", raise_if_missing=True)
    assert child_after.snapshot_id in plan.context_diff.new_snapshots
    assert (
        plan.snapshots[child_after.snapshot_id].change_category == SnapshotChangeCategory.METADATA
    )
    assert plan.snapshots[child_after.snapshot_id].version == child_before.version
    assert not any(i.snapshot_id == child_after.snapshot_id for i in plan.missing_intervals)


@time_machine.travel("2023-01-08 15:00:00 UTC")
@pytest.mark.parametrize(
    "new_parent_category, expected_forward_only",
    [
        # These changes don't require downstream history to be recomputed, so they don't affect
        # the forward-only decision.
        (SnapshotChangeCategory.METADATA, True),
        (SnapshotChangeCategory.NON_BREAKING, True),
        # A breaking change that is not forward-only changes the history of the new parent.
        (SnapshotChangeCategory.BREAKING, False),
    ],
)
def test_stored_forward_only_parent_with_new_parent(
    init_and_plan_context: t.Callable,
    new_parent_category: SnapshotChangeCategory,
    expected_forward_only: bool,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    initial_model_b = context.get_model("sushi.items")
    child_before = context.get_snapshot("sushi.order_items", raise_if_missing=True)

    # Make sushi.orders a forward-only model and deploy a breaking change to it to dev only.
    model = context.get_model("sushi.orders")
    updated_model_kind = model.kind.copy(update={"forward_only": True})
    context.upsert_model(model.copy(update={"stamp": "1", "kind": updated_model_kind}))
    parent_a = context.get_snapshot("sushi.orders", raise_if_missing=True)
    plan_builder = context.plan_builder("dev", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_a, SnapshotChangeCategory.BREAKING)
    context.apply(plan_builder.build())
    assert context.get_snapshot("sushi.orders", raise_if_missing=True).is_forward_only

    context.upsert_model(initial_model_b.name, stamp="1")
    parent_b = context.get_snapshot(initial_model_b.name, raise_if_missing=True)
    plan_builder = context.plan_builder("prod", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_b, new_parent_category)
    plan = plan_builder.build()

    assert parent_a.snapshot_id not in plan.context_diff.new_snapshots
    child_after = plan.snapshots[context.get_snapshot("sushi.order_items").snapshot_id]
    assert child_after.change_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert child_after.is_forward_only == expected_forward_only
    # A forward-only child keeps its previous version, otherwise it's rebuilt.
    assert (child_after.version == child_before.version) == expected_forward_only
    assert any(i.snapshot_id == child_after.snapshot_id for i in plan.missing_intervals) == (
        not expected_forward_only
    )


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_stored_breaking_parent_with_new_forward_only_parent(init_and_plan_context: t.Callable):
    """A forward-only change to a new parent must not make the child forward-only when a stored
    parent also changed in a non-forward-only way, otherwise the child skips the required rebuild."""
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    child_before = context.get_snapshot("sushi.order_items", raise_if_missing=True)

    # Make a breaking, not forward-only, change to sushi.orders and deploy it to dev only.
    context.upsert_model("sushi.orders", stamp="1")
    parent_a = context.get_snapshot("sushi.orders", raise_if_missing=True)
    plan_builder = context.plan_builder("dev", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_a, SnapshotChangeCategory.BREAKING)
    context.apply(plan_builder.build())
    assert not context.get_snapshot("sushi.orders", raise_if_missing=True).is_forward_only

    # Make a forward-only change to sushi.items, which is new in the prod plan.
    model = context.get_model("sushi.items")
    updated_model_kind = model.kind.copy(update={"forward_only": True})
    context.upsert_model(model.copy(update={"stamp": "1", "kind": updated_model_kind}))
    parent_b = context.get_snapshot("sushi.items", raise_if_missing=True)
    plan = context.plan_builder(
        "prod",
        skip_tests=True,
        enable_preview=False,
        categorizer_config=CategorizerConfig.all_full(),
    ).build()

    assert parent_a.snapshot_id not in plan.context_diff.new_snapshots
    assert plan.snapshots[parent_b.snapshot_id].is_forward_only
    child_after = plan.snapshots[context.get_snapshot("sushi.order_items").snapshot_id]
    assert child_after.change_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert not child_after.is_forward_only
    assert child_after.version != child_before.version
    assert any(i.snapshot_id == child_after.snapshot_id for i in plan.missing_intervals)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_stored_parent_history_with_earlier_non_forward_only_change(
    init_and_plan_context: t.Callable,
):
    """Only the latest version of the stored parent is forward-only. An earlier breaking version
    that is not forward-only was never applied to the child, so the child has to be rebuilt."""
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    child_before = context.get_snapshot("sushi.order_items", raise_if_missing=True)

    # First, a breaking change that is not forward-only is deployed to dev only.
    context.upsert_model("sushi.orders", stamp="1")
    plan_builder = context.plan_builder("dev", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(
        context.get_snapshot("sushi.orders", raise_if_missing=True), SnapshotChangeCategory.BREAKING
    )
    context.apply(plan_builder.build())

    # Then a forward-only breaking change to the same model is deployed to dev only.
    model = context.get_model("sushi.orders")
    updated_model_kind = model.kind.copy(update={"forward_only": True})
    context.upsert_model(model.copy(update={"stamp": "2", "kind": updated_model_kind}))
    parent_a = context.get_snapshot("sushi.orders", raise_if_missing=True)
    plan_builder = context.plan_builder("dev", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_a, SnapshotChangeCategory.BREAKING)
    context.apply(plan_builder.build())
    assert context.get_snapshot("sushi.orders", raise_if_missing=True).is_forward_only

    # The prod plan only creates a metadata change to the other parent.
    context.upsert_model("sushi.items", stamp="1")
    parent_b = context.get_snapshot("sushi.items", raise_if_missing=True)
    plan_builder = context.plan_builder("prod", skip_tests=True, enable_preview=False)
    plan_builder.set_choice(parent_b, SnapshotChangeCategory.METADATA)
    plan = plan_builder.build()

    assert parent_a.snapshot_id not in plan.context_diff.new_snapshots
    child_after = plan.snapshots[context.get_snapshot("sushi.order_items").snapshot_id]
    assert child_after.change_category == SnapshotChangeCategory.INDIRECT_BREAKING
    assert not child_after.is_forward_only
    assert child_after.version != child_before.version
    assert any(i.snapshot_id == child_after.snapshot_id for i in plan.missing_intervals)
