from __future__ import annotations

import typing as t
from sqlmesh.core.model.common import ParsableSql
from sqlglot import exp
from sqlglot.expressions import DataType

from sqlmesh.core import constants as c
from sqlmesh.core.context import Context
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.model import (
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    ModelKind,
    ModelKindName,
    SqlModel,
    TimeColumn,
)
from sqlmesh.core.model.kind import model_kind_type_from_name
from sqlmesh.core.plan import Plan, PlanBuilder
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotChangeCategory,
    SnapshotId,
    SnapshotInfoLike,
    SnapshotTableInfo,
)
from sqlmesh.utils.date import TimeLike


def select_all(table: str, adapter: EngineAdapter) -> t.Iterable:
    return adapter.fetchall(f"select * from {table} order by 1")


def snapshots_to_versions(snapshots: t.Iterable[Snapshot]) -> t.Dict[str, str]:
    return {snapshot.name: snapshot.version or "" for snapshot in snapshots}


def to_snapshot_info(snapshot: SnapshotInfoLike) -> SnapshotTableInfo:
    return snapshot.table_info


def start(context: Context) -> TimeLike:
    env = context.state_sync.get_environment("prod")
    assert env
    return env.start_at


def add_projection_to_model(model: SqlModel, literal: bool = True) -> SqlModel:
    one_expr = exp.Literal.number(1).as_("one") if literal else exp.column("one")
    kwargs = {
        **model.dict(),
        "query": model.query.select(one_expr),  # type: ignore
    }
    return SqlModel.parse_obj(kwargs)


def plan_choice(plan_builder: PlanBuilder, choice: SnapshotChangeCategory) -> None:
    for snapshot in plan_builder.build().snapshots.values():
        if not snapshot.version:
            plan_builder.set_choice(snapshot, choice)


def apply_to_environment(
    context: Context,
    environment: str,
    choice: t.Optional[SnapshotChangeCategory] = None,
    plan_validators: t.Optional[t.Iterable[t.Callable]] = None,
    apply_validators: t.Optional[t.Iterable[t.Callable]] = None,
    plan_start: t.Optional[TimeLike] = None,
    allow_destructive_models: t.Optional[t.List[str]] = None,
    enable_preview: bool = False,
):
    plan_validators = plan_validators or []
    apply_validators = apply_validators or []

    plan_builder = context.plan_builder(
        environment,
        start=plan_start or start(context) if environment != c.PROD else None,
        forward_only=choice == SnapshotChangeCategory.FORWARD_ONLY,
        include_unmodified=True,
        allow_destructive_models=allow_destructive_models if allow_destructive_models else [],
        enable_preview=enable_preview,
    )
    if environment != c.PROD:
        plan_builder.set_start(plan_start or start(context))

    if choice:
        if choice == SnapshotChangeCategory.FORWARD_ONLY:
            # FORWARD_ONLY is deprecated, fallback to NON_BREAKING to keep the existing tests
            choice = SnapshotChangeCategory.NON_BREAKING
        plan_choice(plan_builder, choice)
    for validator in plan_validators:
        validator(context, plan_builder.build())

    plan = plan_builder.build()
    context.apply(plan)

    validate_apply_basics(context, environment, plan.snapshots.values(), plan.deployability_index)
    for validator in apply_validators:
        validator(context)
    return plan


def change_data_type(
    context: Context,
    model_name: str,
    old_type: exp.DType,
    new_type: exp.DType,
) -> None:
    model = context.get_model(model_name)
    assert model is not None

    if isinstance(model, SqlModel):
        query = model.query.copy()
        data_types = query.find_all(DataType)
        for data_type in data_types:
            if data_type.this == old_type:
                data_type.set("this", new_type)
        context.upsert_model(model_name, query_=ParsableSql(sql=query.sql(dialect=model.dialect)))
    elif model.columns_to_types_ is not None:
        for k, v in model.columns_to_types_.items():
            if v.this == old_type:
                model.columns_to_types_[k] = DataType.build(new_type)
        context.upsert_model(model_name, columns=model.columns_to_types_)


def validate_snapshots_in_state_sync(snapshots: t.Iterable[Snapshot], context: Context) -> None:
    snapshot_infos = map(to_snapshot_info, snapshots)
    state_sync_table_infos = map(
        to_snapshot_info, context.state_reader.get_snapshots(snapshots).values()
    )
    assert set(snapshot_infos) == set(state_sync_table_infos)


def validate_state_sync_environment(
    snapshots: t.Iterable[Snapshot], env: str, context: Context
) -> None:
    environment = context.state_reader.get_environment(env)
    assert environment
    snapshot_infos = map(to_snapshot_info, snapshots)
    environment_table_infos = map(to_snapshot_info, environment.snapshots)
    assert set(snapshot_infos) == set(environment_table_infos)


def validate_tables(
    snapshots: t.Iterable[Snapshot],
    context: Context,
    deployability_index: t.Optional[DeployabilityIndex] = None,
) -> None:
    adapter = context.engine_adapter
    deployability_index = deployability_index or DeployabilityIndex.all_deployable()
    for snapshot in snapshots:
        is_deployable = deployability_index.is_representative(snapshot)
        if not snapshot.is_model or snapshot.is_external:
            continue
        table_should_exist = not snapshot.is_embedded
        assert adapter.table_exists(snapshot.table_name(is_deployable)) == table_should_exist
        if table_should_exist:
            assert select_all(snapshot.table_name(is_deployable), adapter)


def validate_environment_views(
    snapshots: t.Iterable[Snapshot],
    environment: str,
    context: Context,
    deployability_index: t.Optional[DeployabilityIndex] = None,
) -> None:
    adapter = context.engine_adapter
    deployability_index = deployability_index or DeployabilityIndex.all_deployable()
    for snapshot in snapshots:
        is_deployable = deployability_index.is_representative(snapshot)
        if not snapshot.is_model or snapshot.is_symbolic:
            continue
        view_name = snapshot.qualified_view_name.for_environment(
            EnvironmentNamingInfo.from_environment_catalog_mapping(
                context.config.environment_catalog_mapping,
                name=environment,
                suffix_target=context.config.environment_suffix_target,
            )
        )

        assert adapter.table_exists(view_name)
        assert select_all(snapshot.table_name(is_deployable), adapter) == select_all(
            view_name, adapter
        )


def validate_apply_basics(
    context: Context,
    environment: str,
    snapshots: t.Iterable[Snapshot],
    deployability_index: t.Optional[DeployabilityIndex] = None,
) -> None:
    validate_snapshots_in_state_sync(snapshots, context)
    validate_state_sync_environment(snapshots, environment, context)
    validate_tables(snapshots, context, deployability_index)
    validate_environment_views(snapshots, environment, context, deployability_index)


def validate_plan_changes(
    plan: Plan,
    *,
    added: t.Optional[t.Iterable[SnapshotId]] = None,
    modified: t.Optional[t.Iterable[str]] = None,
    removed: t.Optional[t.Iterable[SnapshotId]] = None,
) -> None:
    added = added or []
    modified = modified or []
    removed = removed or []
    assert set(added) == plan.context_diff.added
    assert set(modified) == set(plan.context_diff.modified_snapshots)
    assert set(removed) == set(plan.context_diff.removed_snapshots)


def validate_versions_same(
    model_names: t.List[str],
    versions: t.Dict[str, str],
    other_versions: t.Dict[str, str],
) -> None:
    for name in model_names:
        assert versions[name] == other_versions[name]


def validate_versions_different(
    model_names: t.List[str],
    versions: t.Dict[str, str],
    other_versions: t.Dict[str, str],
) -> None:
    for name in model_names:
        assert versions[name] != other_versions[name]


def validate_query_change(
    context: Context,
    environment: str,
    change_category: SnapshotChangeCategory,
    logical: bool,
):
    versions = snapshots_to_versions(context.snapshots.values())

    change_data_type(
        context,
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
    not_modified = [
        snapshot.name
        for snapshot in context.snapshots.values()
        if snapshot.name not in directly_modified and snapshot.name not in indirectly_modified
    ]

    if change_category == SnapshotChangeCategory.BREAKING and not logical:
        models_same = not_modified
        models_different = directly_modified + indirectly_modified
    elif change_category == SnapshotChangeCategory.FORWARD_ONLY:
        models_same = not_modified + directly_modified + indirectly_modified
        models_different = []
    else:
        models_same = not_modified + indirectly_modified
        models_different = directly_modified

    def _validate_plan(context, plan):
        validate_plan_changes(plan, modified=directly_modified + indirectly_modified)
        assert bool(plan.missing_intervals) != logical

    def _validate_apply(context):
        current_versions = snapshots_to_versions(context.snapshots.values())
        validate_versions_same(models_same, versions, current_versions)
        validate_versions_different(models_different, versions, current_versions)

    apply_to_environment(
        context,
        environment,
        change_category,
        plan_validators=[_validate_plan],
        apply_validators=[_validate_apply],
    )


def initial_add(context: Context, environment: str):
    assert not context.state_reader.get_environment(environment)

    plan = context.plan(environment, start=start(context), create_from="nonexistent_env")
    validate_plan_changes(plan, added={x.snapshot_id for x in context.snapshots.values()})

    context.apply(plan)
    validate_apply_basics(context, environment, plan.snapshots.values())


def change_model_kind(context: Context, kind: ModelKindName):
    if kind in (ModelKindName.VIEW, ModelKindName.EMBEDDED, ModelKindName.FULL):
        context.upsert_model(
            "sushi.items",
            partitioned_by=[],
        )
    context.upsert_model("sushi.items", kind=model_kind_type_from_name(kind)())  # type: ignore


def validate_model_kind_change(
    kind_name: ModelKindName,
    context: Context,
    environment: str,
    *,
    logical: bool,
):
    directly_modified = ['"memory"."sushi"."items"']
    indirectly_modified = [
        '"memory"."sushi"."order_items"',
        '"memory"."sushi"."waiter_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_by_day"',
        '"memory"."sushi"."customer_revenue_lifetime"',
        '"memory"."sushi"."top_waiters"',
        "assert_item_price_above_zero",
    ]
    if kind_name == ModelKindName.INCREMENTAL_BY_TIME_RANGE:
        kind: ModelKind = IncrementalByTimeRangeKind(time_column=TimeColumn(column="event_date"))
    elif kind_name == ModelKindName.INCREMENTAL_BY_UNIQUE_KEY:
        kind = IncrementalByUniqueKeyKind(unique_key="id")
    else:
        kind = model_kind_type_from_name(kind_name)()  # type: ignore

    def _validate_plan(context, plan):
        validate_plan_changes(plan, modified=directly_modified + indirectly_modified)
        assert (
            next(
                snapshot
                for snapshot in plan.snapshots.values()
                if snapshot.name == '"memory"."sushi"."items"'
            ).model.kind.name
            == kind.name
        )
        assert bool(plan.missing_intervals) != logical

    apply_to_environment(
        context,
        environment,
        SnapshotChangeCategory.NON_BREAKING,
        plan_validators=[_validate_plan],
    )
