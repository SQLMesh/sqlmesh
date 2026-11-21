from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import typing as t
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import exp
from sqlmesh.core.model.common import ParsableSql
import time_machine

from sqlmesh.core import dialect as d
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)
from sqlmesh.core.config.common import VirtualEnvironmentMode
from sqlmesh.core.context import Context
from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObjectType
from sqlmesh.core.janitor import delete_expired_snapshots
from sqlmesh.core.model import (
    FullKind,
    IncrementalUnmanagedKind,
    SqlModel,
    ViewKind,
    load_sql_based_model,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.core.snapshot import (
    SnapshotChangeCategory,
)
from sqlmesh.utils.date import now_timestamp, to_date, to_timestamp
from sqlmesh.utils.errors import PlanError, SQLMeshError
from tests.core.integration.utils import add_projection_to_model

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )

    assert all(
        s.virtual_environment_mode.is_dev_only or not s.is_model or s.is_symbolic
        for s in context.snapshots.values()
    )

    # Init prod
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Make a change in dev
    original_model = context.get_model("sushi.waiter_revenue_by_day")
    original_fingerprint = context.get_snapshot(original_model.name).fingerprint
    model = original_model.copy(
        update={
            "query_": ParsableSql(
                sql=original_model.query.order_by("waiter_id").sql(dialect=original_model.dialect)
            )
        }
    )
    model = add_projection_to_model(t.cast(SqlModel, model))
    context.upsert_model(model)

    plan_dev = context.plan_builder("dev").build()
    assert to_timestamp(plan_dev.start) == to_timestamp("2023-01-07")
    assert plan_dev.requires_backfill
    assert plan_dev.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.top_waiters").snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.waiter_revenue_by_day").snapshot_id,
            intervals=[(to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))],
        ),
    ]
    assert plan_dev.context_diff.snapshots[context.get_snapshot(model.name).snapshot_id].intervals
    assert plan_dev.context_diff.snapshots[
        context.get_snapshot("sushi.top_waiters").snapshot_id
    ].intervals
    assert plan_dev.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].dev_intervals
    assert plan_dev.context_diff.snapshots[
        context.get_snapshot("sushi.top_waiters").snapshot_id
    ].dev_intervals
    context.apply(plan_dev)

    # Make sure the waiter_revenue_by_day model is a table in prod and a view in dev
    table_types_df = context.engine_adapter.fetchdf(
        "SELECT table_schema, table_type FROM INFORMATION_SCHEMA.TABLES WHERE table_name = 'waiter_revenue_by_day'"
    )
    assert table_types_df.to_dict("records") == [
        {"table_schema": "sushi", "table_type": "BASE TABLE"},
        {"table_schema": "sushi__dev", "table_type": "VIEW"},
    ]

    # Check that the specified dates were backfilled
    min_event_date = context.engine_adapter.fetchone(
        "SELECT MIN(event_date) FROM sushi__dev.waiter_revenue_by_day"
    )[0]
    assert min_event_date == to_date("2023-01-07")

    # Make sure the changes are applied without backfill in prod
    plan_prod = context.plan_builder("prod").build()
    assert not plan_prod.requires_backfill
    assert not plan_prod.missing_intervals
    context.apply(plan_prod)
    assert "one" in context.engine_adapter.columns("sushi.waiter_revenue_by_day")

    # Make sure the revert of a breaking changes results in a full rebuild
    context.upsert_model(original_model)
    assert context.get_snapshot(original_model.name).fingerprint == original_fingerprint

    plan_prod = context.plan_builder(
        "prod", allow_destructive_models=["sushi.waiter_revenue_by_day"]
    ).build()
    assert not plan_prod.requires_backfill
    assert not plan_prod.missing_intervals
    context.apply(plan_prod)
    assert "one" not in context.engine_adapter.columns("sushi.waiter_revenue_by_day")


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_model_kind_change(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.apply(plan)

    # Change to full kind
    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": FullKind()})
    context.upsert_model(model)
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.missing_intervals
    assert prod_plan.requires_backfill
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"top_waiters"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "table"

    # Change back to view
    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": ViewKind()})
    context.upsert_model(model)
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"top_waiters"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "view"

    # Change to incremental
    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": IncrementalUnmanagedKind()})
    context.upsert_model(model)
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"top_waiters"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "table"

    # Change back to full
    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": FullKind()})
    context.upsert_model(model)
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"top_waiters"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "table"


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_model_kind_change_incremental(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )

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
    forward_only_model = forward_only_model.copy(
        update={"virtual_environment_mode": VirtualEnvironmentMode.DEV_ONLY}
    )
    context.upsert_model(forward_only_model)

    context.plan("prod", auto_apply=True, no_prompts=True)

    # Change to view
    model = context.get_model(forward_only_model_name)
    original_kind = model.kind
    model = model.copy(update={"kind": ViewKind()})
    context.upsert_model(model)
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"test_forward_only_model"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "view"

    model = model.copy(update={"kind": original_kind})
    context.upsert_model(model)
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"test_forward_only_model"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "table"


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_model_kind_change_with_follow_up_changes_in_dev(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.apply(plan)

    # Make sure the initial state is a view
    data_objects = context.engine_adapter.get_data_objects("sushi", {"top_waiters"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "view"

    # Change to incremental unmanaged kind
    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": IncrementalUnmanagedKind()})
    context.upsert_model(model)
    dev_plan = context.plan_builder("dev", skip_tests=True).build()
    assert dev_plan.missing_intervals
    assert dev_plan.requires_backfill
    context.apply(dev_plan)

    # Make a follow-up forward-only change
    model = add_projection_to_model(t.cast(SqlModel, model))
    context.upsert_model(model)
    dev_plan = context.plan_builder("dev", skip_tests=True, forward_only=True).build()
    context.apply(dev_plan)

    # Deploy to prod
    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals
    assert not prod_plan.context_diff.snapshots[
        context.get_snapshot(model.name).snapshot_id
    ].intervals
    context.apply(prod_plan)
    data_objects = context.engine_adapter.get_data_objects("sushi", {"top_waiters"})
    assert len(data_objects) == 1
    assert data_objects[0].type == "table"


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_model_kind_change_manual_categorization(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.apply(plan)

    model = context.get_model("sushi.top_waiters")
    model = model.copy(update={"kind": FullKind()})
    context.upsert_model(model)
    dev_plan_builder = context.plan_builder("dev", skip_tests=True, no_auto_categorization=True)
    dev_plan_builder.set_choice(
        dev_plan_builder._context_diff.snapshots[context.get_snapshot(model.name).snapshot_id],
        SnapshotChangeCategory.NON_BREAKING,
    )
    dev_plan = dev_plan_builder.build()
    assert dev_plan.requires_backfill
    assert len(dev_plan.missing_intervals) == 1
    context.apply(dev_plan)

    prod_plan = context.plan_builder("prod", skip_tests=True).build()
    assert prod_plan.requires_backfill
    assert prod_plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=context.get_snapshot("sushi.top_waiters").snapshot_id,
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
def test_virtual_environment_mode_dev_only_seed_model_change(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.load()
    context.plan("prod", auto_apply=True, no_prompts=True)

    seed_model = context.get_model("sushi.waiter_names")
    with open(seed_model.seed_path, "a") as fd:
        fd.write("\n123,New Test Name")

    context.load()
    seed_model_snapshot = context.get_snapshot("sushi.waiter_names")
    plan = context.plan_builder("dev").build()
    assert plan.directly_modified == {seed_model_snapshot.snapshot_id}
    assert len(plan.missing_intervals) == 2
    context.apply(plan)

    actual_seed_df_in_dev = context.fetchdf("SELECT * FROM sushi__dev.waiter_names WHERE id = 123")
    assert actual_seed_df_in_dev.to_dict("records") == [{"id": 123, "name": "New Test Name"}]
    actual_seed_df_in_prod = context.fetchdf("SELECT * FROM sushi.waiter_names WHERE id = 123")
    assert actual_seed_df_in_prod.empty

    plan = context.plan_builder("prod").build()
    assert plan.directly_modified == {seed_model_snapshot.snapshot_id}
    assert len(plan.missing_intervals) == 1
    assert plan.missing_intervals[0].snapshot_id == seed_model_snapshot.snapshot_id
    context.apply(plan)

    actual_seed_df_in_prod = context.fetchdf("SELECT * FROM sushi.waiter_names WHERE id = 123")
    assert actual_seed_df_in_prod.to_dict("records") == [{"id": 123, "name": "New Test Name"}]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_model_change_downstream_of_seed(
    init_and_plan_context: t.Callable,
):
    """This test covers a scenario when a model downstream of a seed model is modified and explicitly selected
    causing an (unhydrated) seed model to sourced from the state. If SQLMesh attempts to create
    a table for the unchanged seed model, it will fail because the seed model is not hydrated.
    """
    context, _ = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.load()
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Make sure that a different version of the seed model is loaded
    seed_model = context.get_model("sushi.waiter_names")
    seed_model = seed_model.copy(update={"stamp": "force new version"})
    context.upsert_model(seed_model)

    # Make a change to the downstream model
    model = context.get_model("sushi.waiter_as_customer_by_day")
    model = model.copy(update={"stamp": "force new version"})
    context.upsert_model(model)

    # It is important to clear the cache so that the hydrated seed model is not sourced from the cache
    context.clear_caches()

    # Make sure to use the selector so that the seed model is sourced from the state
    plan = context.plan_builder("dev", select_models=[model.name]).build()
    assert len(plan.directly_modified) == 1
    assert list(plan.directly_modified)[0].name == model.fqn
    assert len(plan.missing_intervals) == 1
    assert plan.missing_intervals[0].snapshot_id.name == model.fqn

    # Make sure there's no error when applying the plan
    context.apply(plan)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_model_change_standalone_audit(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.apply(plan)

    # Change a model upstream from a standalone audit
    model = context.get_model("sushi.items")
    model = model.copy(update={"stamp": "force new version"})
    context.upsert_model(model)

    plan = context.plan_builder("prod", skip_tests=True).build()

    # Make sure the standalone audit is among modified
    assert (
        context.get_snapshot("assert_item_price_above_zero").snapshot_id
        in plan.indirectly_modified[context.get_snapshot("sushi.items").snapshot_id]
    )

    # Make sure there's no error when applying the plan
    context.apply(plan)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_virtual_environment_mode_dev_only_seed_model_change_schema(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context(
        "examples/sushi", config="test_config_virtual_environment_mode_dev_only"
    )
    context.apply(plan)

    new_csv = []
    with open(context.path / "seeds" / "waiter_names.csv", "r") as fd:
        is_header = True
        for idx, line in enumerate(fd):
            line = line.strip()
            if not line:
                continue
            if is_header:
                new_csv.append(line + ",new_column")
                is_header = False
            else:
                new_csv.append(line + f",v{idx}")

    with open(context.path / "seeds" / "waiter_names.csv", "w") as fd:
        fd.write("\n".join(new_csv))

    context.load()

    downstream_model = context.get_model("sushi.waiter_as_customer_by_day")
    downstream_model_kind = downstream_model.kind.dict()
    downstream_model_kwargs = {
        **downstream_model.dict(),
        "kind": {
            **downstream_model_kind,
            "on_destructive_change": "allow",
        },
        "audits": [],
        # Use the new column
        "query": "SELECT '2023-01-07' AS event_date, new_column AS new_column FROM sushi.waiter_names",
    }
    context.upsert_model(SqlModel.parse_obj(downstream_model_kwargs))

    context.plan("dev", auto_apply=True, no_prompts=True, skip_tests=True, enable_preview=True)

    assert (
        context.engine_adapter.fetchone(
            "SELECT COUNT(*) FROM sushi__dev.waiter_as_customer_by_day"
        )[0]
        == len(new_csv) - 1
    )

    # Deploy to prod
    context.clear_caches()
    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)
    assert "new_column" in context.engine_adapter.columns("sushi.waiter_as_customer_by_day")


class CloningDuckDBEngineAdapter(DuckDBEngineAdapter):
    SUPPORTS_CLONING = True

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        cloned_tables = kwargs.get("cloned_tables")
        if cloned_tables is None:
            cloned_tables = []
            kwargs["cloned_tables"] = cloned_tables
        super().__init__(*args, **kwargs)
        self.cloned_tables: t.List[t.Tuple[t.Any, t.Any]] = cloned_tables

    def clone_table(
        self,
        target_table_name: t.Union[str, exp.Table],
        source_table_name: t.Union[str, exp.Table],
        replace: bool = False,
        exists: bool = True,
        clone_kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        table_format: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> None:
        self._clear_data_object_cache(source_table_name)
        source_obj = self.get_data_object(source_table_name)
        if source_obj is None or not (source_obj.type.is_table or source_obj.type.is_managed_table):
            fmt = source_obj.type.value.upper() if source_obj else "MISSING"
            raise SQLMeshError(
                f"[DELTA_CLONE_UNSUPPORTED_SOURCE] Unsupported clone source '{source_table_name}', whose format is {fmt}"
            )

        self.cloned_tables.append((target_table_name, source_table_name))
        target_sql = exp.to_table(target_table_name, dialect=self.dialect).sql(dialect=self.dialect)
        source_sql = exp.to_table(source_table_name, dialect=self.dialect).sql(dialect=self.dialect)
        if replace:
            self.execute(f"CREATE OR REPLACE TABLE {target_sql} AS SELECT * FROM {source_sql}")
        elif exists:
            self.execute(f"CREATE TABLE IF NOT EXISTS {target_sql} AS SELECT * FROM {source_sql}")
        else:
            self.execute(f"CREATE TABLE {target_sql} AS SELECT * FROM {source_sql}")
        self._clear_data_object_cache(target_table_name)


def _snap_id(ctx: Context, name: str) -> t.Any:
    snap = ctx.get_snapshot(name)
    assert snap is not None
    return snap.snapshot_id


def _scalar(ctx: Context, sql: str) -> t.Any:
    row = ctx.engine_adapter.fetchone(sql)
    assert row is not None
    return row[0]


def _obj_type(ctx: Context, name: str) -> DataObjectType:
    obj = ctx.engine_adapter.get_data_object(name)
    assert obj is not None
    return obj.type


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_vde_mode_transition_full_to_dev_only_with_cloning(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("sqlmesh.core.engine_adapter.DuckDBEngineAdapter", CloningDuckDBEngineAdapter)

    seeds_dir = tmp_path / "seeds"
    models_dir = tmp_path / "models"
    seeds_dir.mkdir()
    models_dir.mkdir()
    db_path = tmp_path / "warehouse.db"

    (seeds_dir / "seed_data.csv").write_text("id,label\n1,alpha\n2,beta\n", encoding="utf-8")
    (models_dir / "seed_m.sql").write_text(
        """
        MODEL (
            name db.seed_m,
            kind SEED (
                path '../seeds/seed_data.csv'
            )
        );
        """,
        encoding="utf-8",
    )
    (models_dir / "full_m.sql").write_text(
        """
        MODEL (
            name db.full_m,
            kind FULL
        );
        SELECT id, label FROM db.seed_m;
        """,
        encoding="utf-8",
    )

    def write_upstream(id_val: int) -> None:
        (models_dir / "upstream.sql").write_text(
            f"""
            MODEL (
                name db.upstream,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1
                ),
                start '2023-01-05'
            );
            SELECT {id_val}::INT AS id, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )

    def write_downstream(val: int) -> None:
        (models_dir / "downstream.sql").write_text(
            f"""
            MODEL (
                name db.downstream,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                ),
                start '2023-01-05'
            );
            SELECT id, {val}::INT AS val, ds FROM db.upstream WHERE ds BETWEEN @start_ds AND @end_ds;
            """,
            encoding="utf-8",
        )

    (models_dir / "v_model.sql").write_text(
        """
        MODEL (
            name db.v_model,
            kind VIEW
        );
        SELECT id, val, ds FROM db.downstream;
        """,
        encoding="utf-8",
    )

    write_upstream(1)
    write_downstream(10)

    with ExitStack() as stack:

        def make_ctx(mode: VirtualEnvironmentMode) -> Context:
            ctx = Context(
                paths=tmp_path,
                config=Config(
                    gateways={
                        "": GatewayConfig(connection=DuckDBConnectionConfig(database=str(db_path)))
                    },
                    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
                    virtual_environment_mode=mode,
                ),
            )
            stack.callback(ctx.close)
            return ctx

        # 1. Deploy prod in FULL VDE mode
        ctx_full = make_ctx(VirtualEnvironmentMode.FULL)
        assert isinstance(ctx_full.engine_adapter, CloningDuckDBEngineAdapter)
        ctx_full.plan("prod", auto_apply=True, no_prompts=True)
        prod_objs_initial = {o.name: o.type for o in ctx_full.engine_adapter.get_data_objects("db")}
        assert prod_objs_initial == {
            "seed_m": DataObjectType.VIEW,
            "full_m": DataObjectType.VIEW,
            "upstream": DataObjectType.VIEW,
            "downstream": DataObjectType.VIEW,
            "v_model": DataObjectType.VIEW,
        }
        ctx_full.close()

        # 2. Switch config to DEV_ONLY, edit downstream & apply plan("dev") #1
        write_downstream(20)
        ctx_dev = make_ctx(VirtualEnvironmentMode.DEV_ONLY)
        assert isinstance(ctx_dev.engine_adapter, CloningDuckDBEngineAdapter)
        plan_dev1 = ctx_dev.plan_builder("dev").build()
        ctx_dev.apply(plan_dev1)
        assert (
            plan_dev1.context_diff.snapshots[_snap_id(ctx_dev, "db.downstream")].is_forward_only
            is False
        )
        assert ctx_dev.engine_adapter.cloned_tables == []

        # 3. Edit downstream again & apply plan("dev") #2 (reproduces #5887 [DELTA_CLONE_UNSUPPORTED_SOURCE] crash on HEAD)
        write_downstream(30)
        ctx_dev.load()
        plan_dev2 = ctx_dev.plan_builder("dev").build()
        ctx_dev.apply(plan_dev2)
        assert (
            plan_dev2.context_diff.snapshots[_snap_id(ctx_dev, "db.downstream")].is_forward_only
            is False
        )
        assert ctx_dev.engine_adapter.cloned_tables == []

        # 4. Edit upstream (making downstream indirectly_modified) & apply plan("dev") #3
        write_upstream(2)
        ctx_dev.load()
        plan_dev3 = ctx_dev.plan_builder("dev").build()
        ctx_dev.apply(plan_dev3)
        upstream_sid = _snap_id(ctx_dev, "db.upstream")
        downstream_sid = _snap_id(ctx_dev, "db.downstream")
        assert downstream_sid in plan_dev3.indirectly_modified[upstream_sid]
        assert plan_dev3.context_diff.snapshots[upstream_sid].is_forward_only is False
        assert plan_dev3.context_diff.snapshots[downstream_sid].is_forward_only is False
        assert ctx_dev.engine_adapter.cloned_tables == []

        # 5. Edit downstream again & apply plan("dev2", create_from="dev")
        write_downstream(40)
        ctx_dev.load()
        plan_dev2_branch = ctx_dev.plan_builder("dev2", create_from="dev").build()
        ctx_dev.apply(plan_dev2_branch)
        assert (
            plan_dev2_branch.context_diff.snapshots[
                _snap_id(ctx_dev, "db.downstream")
            ].is_forward_only
            is False
        )
        assert ctx_dev.engine_adapter.cloned_tables == []
        assert ctx_dev.engine_adapter.fetchall("SELECT DISTINCT id, val FROM db__dev2.v_model") == [
            (2, 40)
        ]

        # 6. plan("dev_sel", select_models=["db.downstream"]) keeps unselected FULL snapshots from prod as non-forward-only
        write_downstream(50)
        ctx_dev.load()
        plan_dev_sel = ctx_dev.plan_builder("dev_sel", select_models=["db.downstream"]).build()
        ctx_dev.apply(plan_dev_sel)
        unselected_models = [
            s
            for s in plan_dev_sel.context_diff.snapshots.values()
            if s.is_model and s.name != '"warehouse"."db"."downstream"'
        ]
        assert unselected_models
        for unselected_snap in unselected_models:
            assert unselected_snap.virtual_environment_mode == VirtualEnvironmentMode.FULL
            assert unselected_snap.is_forward_only is False

        # 7. Apply plan("prod") to migrate prod from FULL -> DEV_ONLY
        plan_prod = ctx_dev.plan_builder("prod").build()
        assert plan_prod.requires_backfill
        ctx_dev.apply(plan_prod)

        prod_objs_migrated = {o.name: o.type for o in ctx_dev.engine_adapter.get_data_objects("db")}
        assert prod_objs_migrated == {
            "seed_m": DataObjectType.TABLE,
            "full_m": DataObjectType.TABLE,
            "upstream": DataObjectType.TABLE,
            "downstream": DataObjectType.TABLE,
            "v_model": DataObjectType.VIEW,
        }
        assert _scalar(ctx_dev, "SELECT COUNT(*) FROM db.seed_m") == 2
        assert _scalar(ctx_dev, "SELECT COUNT(*) FROM db.full_m") == 2
        assert ctx_dev.engine_adapter.fetchall(
            "SELECT id, val, ds FROM db.v_model ORDER BY ds"
        ) == [
            (2, 50, to_date("2023-01-05")),
            (2, 50, to_date("2023-01-06")),
            (2, 50, to_date("2023-01-07")),
        ]

        # 8. Edit downstream & apply plan("dev") #4 -> now clones prod's physical table db.downstream!
        ctx_dev.engine_adapter.cloned_tables.clear()
        write_downstream(60)
        ctx_dev.load()
        plan_dev4 = ctx_dev.plan_builder("dev").build()
        assert (
            plan_dev4.context_diff.snapshots[_snap_id(ctx_dev, "db.downstream")].is_forward_only
            is True
        )
        ctx_dev.apply(plan_dev4)
        assert len(ctx_dev.engine_adapter.cloned_tables) == 1
        cloned_target, cloned_source = ctx_dev.engine_adapter.cloned_tables[0]
        assert exp.to_table(cloned_source).name == "downstream"
        assert exp.to_table(cloned_source).db == "db"

        # 9. Invalidate dev environments & run delete_expired_snapshots(ignore_ttl=True)
        for env_name in ("dev", "dev2", "dev_sel"):
            ctx_dev.invalidate_environment(env_name, sync=True)
        failures = delete_expired_snapshots(
            ctx_dev.state_sync,
            ctx_dev.snapshot_evaluator,
            current_ts=now_timestamp(),
            ignore_ttl=True,
        )
        assert failures == []
        assert [
            o
            for o in ctx_dev.engine_adapter.get_data_objects("sqlmesh__db")
            if not o.name.endswith("__dev")
        ] == []
        assert ctx_dev.engine_adapter.get_data_object(cloned_target) is None
        assert _scalar(ctx_dev, "SELECT COUNT(*) FROM db.v_model") == 3
        assert _scalar(ctx_dev, "SELECT COUNT(*) FROM db.full_m") == 2
        assert _scalar(ctx_dev, "SELECT COUNT(*) FROM db.seed_m") == 2


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_vde_mode_transition_dev_only_to_full_with_cloning_and_janitor(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("sqlmesh.core.engine_adapter.DuckDBEngineAdapter", CloningDuckDBEngineAdapter)

    seeds_dir = tmp_path / "seeds"
    models_dir = tmp_path / "models"
    seeds_dir.mkdir()
    models_dir.mkdir()
    db_path = tmp_path / "warehouse.db"

    (seeds_dir / "seed_data.csv").write_text("id,label\n1,alpha\n2,beta\n", encoding="utf-8")
    (models_dir / "seed_m.sql").write_text(
        """
        MODEL (
            name db.seed_m,
            kind SEED (
                path '../seeds/seed_data.csv'
            )
        );
        """,
        encoding="utf-8",
    )
    (models_dir / "full_m.sql").write_text(
        """
        MODEL (
            name db.full_m,
            kind FULL
        );
        SELECT id, label FROM db.seed_m;
        """,
        encoding="utf-8",
    )
    (models_dir / "upstream.sql").write_text(
        """
        MODEL (
            name db.upstream,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                batch_size 1
            ),
            start '2023-01-05'
        );
        SELECT 1::INT AS id, @start_ds::DATE AS ds;
        """,
        encoding="utf-8",
    )

    def write_downstream(val: int) -> None:
        (models_dir / "downstream.sql").write_text(
            f"""
            MODEL (
                name db.downstream,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true
                ),
                start '2023-01-05'
            );
            SELECT id, {val}::INT AS val, ds FROM db.upstream WHERE ds BETWEEN @start_ds AND @end_ds;
            """,
            encoding="utf-8",
        )

    def write_view(tag: int) -> None:
        (models_dir / "v_model.sql").write_text(
            f"""
            MODEL (
                name db.v_model,
                kind VIEW
            );
            SELECT id, val, {tag}::INT AS tag, ds FROM db.downstream;
            """,
            encoding="utf-8",
        )

    write_downstream(10)
    write_view(1)

    with ExitStack() as stack:

        def make_ctx(mode: VirtualEnvironmentMode) -> Context:
            ctx = Context(
                paths=tmp_path,
                config=Config(
                    gateways={
                        "": GatewayConfig(connection=DuckDBConnectionConfig(database=str(db_path)))
                    },
                    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
                    virtual_environment_mode=mode,
                ),
            )
            stack.callback(ctx.close)
            return ctx

        # 1. Start in DEV_ONLY mode and deploy prod
        ctx_dev_only = make_ctx(VirtualEnvironmentMode.DEV_ONLY)
        ctx_dev_only.plan("prod", auto_apply=True, no_prompts=True)
        assert {o.name: o.type for o in ctx_dev_only.engine_adapter.get_data_objects("db")} == {
            "seed_m": DataObjectType.TABLE,
            "full_m": DataObjectType.TABLE,
            "upstream": DataObjectType.TABLE,
            "downstream": DataObjectType.TABLE,
            "v_model": DataObjectType.VIEW,
        }
        ctx_dev_only.close()

        # 2. Switch config to FULL, edit models, and run plan("dev") #1 and plan("dev") #2
        write_downstream(20)
        write_view(2)
        ctx_full = make_ctx(VirtualEnvironmentMode.FULL)
        plan_dev1 = ctx_full.plan_builder("dev").build()
        dev1_downstream_snap = plan_dev1.context_diff.snapshots[_snap_id(ctx_full, "db.downstream")]
        assert dev1_downstream_snap.is_forward_only is False
        dev1_downstream_dev_table = dev1_downstream_snap.table_name(is_deployable=False)
        ctx_full.apply(plan_dev1)

        write_downstream(30)
        ctx_full.load()
        plan_dev2 = ctx_full.plan_builder("dev").build()
        assert (
            plan_dev2.context_diff.snapshots[_snap_id(ctx_full, "db.downstream")].is_forward_only
            is False
        )
        ctx_full.apply(plan_dev2)

        # 3. Apply plan("prod") to migrate prod from DEV_ONLY -> FULL
        plan_prod = ctx_full.plan_builder("prod").build()
        assert plan_prod.requires_backfill
        ctx_full.apply(plan_prod)

        assert {o.name: o.type for o in ctx_full.engine_adapter.get_data_objects("db")} == {
            "seed_m": DataObjectType.VIEW,
            "full_m": DataObjectType.VIEW,
            "upstream": DataObjectType.VIEW,
            "downstream": DataObjectType.VIEW,
            "v_model": DataObjectType.VIEW,
        }
        assert (
            len(
                [
                    o
                    for o in ctx_full.engine_adapter.get_data_objects("sqlmesh__db")
                    if not o.name.endswith("__dev")
                ]
            )
            == 5
        )

        # 4. Invalidate dev and run delete_expired_snapshots(ignore_ttl=True)
        ctx_full.invalidate_environment("dev", sync=True)
        failures = delete_expired_snapshots(
            ctx_full.state_sync,
            ctx_full.snapshot_evaluator,
            current_ts=now_timestamp(),
            ignore_ttl=True,
        )
        assert failures == []
        assert ctx_full.engine_adapter.get_data_object(dev1_downstream_dev_table) is None
        assert (
            len(
                [
                    o
                    for o in ctx_full.engine_adapter.get_data_objects("sqlmesh__db")
                    if not o.name.endswith("__dev")
                ]
            )
            == 5
        )
        assert ctx_full.engine_adapter.fetchall(
            "SELECT id, val, tag, ds FROM db.v_model ORDER BY ds"
        ) == [
            (1, 30, 2, to_date("2023-01-05")),
            (1, 30, 2, to_date("2023-01-06")),
            (1, 30, 2, to_date("2023-01-07")),
        ]
        assert _scalar(ctx_full, "SELECT COUNT(*) FROM db.downstream") == 3


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_dev_only_view_to_incremental_with_cloning(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("sqlmesh.core.engine_adapter.DuckDBEngineAdapter", CloningDuckDBEngineAdapter)

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    db_path = tmp_path / "warehouse.db"

    (models_dir / "m.sql").write_text(
        """
        MODEL (
            name db.m,
            kind VIEW
        );
        SELECT 1::INT AS id, DATE '2023-01-05' AS ds;
        """,
        encoding="utf-8",
    )

    with ExitStack() as stack:
        ctx = Context(
            paths=tmp_path,
            config=Config(
                gateways={
                    "": GatewayConfig(connection=DuckDBConnectionConfig(database=str(db_path)))
                },
                model_defaults=ModelDefaultsConfig(dialect="duckdb"),
                virtual_environment_mode=VirtualEnvironmentMode.DEV_ONLY,
            ),
        )
        stack.callback(ctx.close)
        assert isinstance(ctx.engine_adapter, CloningDuckDBEngineAdapter)

        # 1. Deploy db.m as VIEW in prod
        ctx.plan("prod", auto_apply=True, no_prompts=True)
        assert _obj_type(ctx, "db.m") == DataObjectType.VIEW

        # 2. Change db.m to INCREMENTAL_BY_TIME_RANGE in plan("dev") #1
        (models_dir / "m.sql").write_text(
            """
            MODEL (
                name db.m,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1
                ),
                start '2023-01-05'
            );
            SELECT 1::INT AS id, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )
        ctx.load()
        plan_dev1 = ctx.plan_builder("dev").build()
        ctx.apply(plan_dev1)
        assert ctx.engine_adapter.cloned_tables == []

        # 3. Make follow-up SQL edit to db.m in plan("dev") #2 -> must NOT clone prod's VIEW!
        (models_dir / "m.sql").write_text(
            """
            MODEL (
                name db.m,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1
                ),
                start '2023-01-05'
            );
            SELECT 2::INT AS id, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )
        ctx.load()
        plan_dev2 = ctx.plan_builder("dev").build()
        ctx.apply(plan_dev2)
        assert ctx.engine_adapter.cloned_tables == []
        assert plan_dev2.context_diff.snapshots[_snap_id(ctx, "db.m")].is_forward_only is False

        # 4. Deploy to prod -> migrates db.m cleanly from VIEW to TABLE
        plan_prod = ctx.plan_builder("prod").build()
        assert plan_prod.requires_backfill
        ctx.apply(plan_prod)
        assert _obj_type(ctx, "db.m") == DataObjectType.TABLE
        assert ctx.engine_adapter.fetchall("SELECT id, ds FROM db.m ORDER BY ds") == [
            (2, to_date("2023-01-05")),
            (2, to_date("2023-01-06")),
            (2, to_date("2023-01-07")),
        ]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_vde_mode_round_trip_dev_only_to_full_to_dev_only_without_janitor(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("sqlmesh.core.engine_adapter.DuckDBEngineAdapter", CloningDuckDBEngineAdapter)

    seeds_dir = tmp_path / "seeds"
    models_dir = tmp_path / "models"
    seeds_dir.mkdir()
    models_dir.mkdir()
    db_path = tmp_path / "warehouse.db"

    (seeds_dir / "seed_data.csv").write_text("id,label\n1,alpha\n2,beta\n", encoding="utf-8")
    (models_dir / "seed_m.sql").write_text(
        """
        MODEL (
            name db.seed_m,
            kind SEED (
                path '../seeds/seed_data.csv'
            )
        );
        """,
        encoding="utf-8",
    )
    (models_dir / "full_m.sql").write_text(
        """
        MODEL (
            name db.full_m,
            kind FULL
        );
        SELECT id, label FROM db.seed_m;
        """,
        encoding="utf-8",
    )
    (models_dir / "upstream.sql").write_text(
        """
        MODEL (
            name db.upstream,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds,
                batch_size 1
            ),
            start '2023-01-05'
        );
        SELECT 1::INT AS id, @start_ds::DATE AS ds;
        """,
        encoding="utf-8",
    )

    def write_downstream(val: int) -> None:
        (models_dir / "downstream.sql").write_text(
            f"""
            MODEL (
                name db.downstream,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1
                ),
                start '2023-01-05'
            );
            SELECT id, {val}::INT AS val, ds FROM db.upstream WHERE ds BETWEEN @start_ds AND @end_ds;
            """,
            encoding="utf-8",
        )

    write_downstream(10)

    with ExitStack() as stack:

        def make_ctx(mode: VirtualEnvironmentMode) -> Context:
            ctx = Context(
                paths=tmp_path,
                config=Config(
                    gateways={
                        "": GatewayConfig(connection=DuckDBConnectionConfig(database=str(db_path)))
                    },
                    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
                    virtual_environment_mode=mode,
                ),
            )
            stack.callback(ctx.close)
            return ctx

        # 1. Deploy prod in DEV_ONLY mode -> populates version='novde' intervals in _intervals
        ctx_dev_only_1 = make_ctx(VirtualEnvironmentMode.DEV_ONLY)
        ctx_dev_only_1.plan("prod", auto_apply=True, no_prompts=True)
        assert _scalar(ctx_dev_only_1, "SELECT COUNT(*) FROM db.upstream") == 3
        assert _scalar(ctx_dev_only_1, "SELECT COUNT(*) FROM db.downstream") == 3
        assert _scalar(ctx_dev_only_1, "SELECT COUNT(*) FROM db.full_m") == 2
        ctx_dev_only_1.close()

        # 2. Migrate prod to FULL WITHOUT running Janitor -> drops physical db.* tables & creates VIEWs
        ctx_full_1 = make_ctx(VirtualEnvironmentMode.FULL)
        ctx_full_1.plan("prod", auto_apply=True, no_prompts=True)
        assert {o.name: o.type for o in ctx_full_1.engine_adapter.get_data_objects("db")} == {
            "seed_m": DataObjectType.VIEW,
            "full_m": DataObjectType.VIEW,
            "upstream": DataObjectType.VIEW,
            "downstream": DataObjectType.VIEW,
        }
        ctx_full_1.close()

        # 3. Migrate back to DEV_ONLY:
        #    - Edit downstream SQL (new snapshot s_3, previewed in dev first so s_3 is in _snapshots)
        #    - Leave upstream, full_m, and seed_m unedited (reverts to original s_1 snapshot IDs!)
        write_downstream(99)
        ctx_dev_only_2 = make_ctx(VirtualEnvironmentMode.DEV_ONLY)
        plan_dev = ctx_dev_only_2.plan_builder("dev").build()
        ctx_dev_only_2.apply(plan_dev)

        plan_prod = ctx_dev_only_2.plan_builder("prod").build()
        assert plan_prod.requires_backfill
        ctx_dev_only_2.apply(plan_prod)

        assert {o.name: o.type for o in ctx_dev_only_2.engine_adapter.get_data_objects("db")} == {
            "seed_m": DataObjectType.TABLE,
            "full_m": DataObjectType.TABLE,
            "upstream": DataObjectType.TABLE,
            "downstream": DataObjectType.TABLE,
        }
        assert _scalar(ctx_dev_only_2, "SELECT COUNT(*) FROM db.seed_m") == 2
        assert _scalar(ctx_dev_only_2, "SELECT COUNT(*) FROM db.full_m") == 2
        assert _scalar(ctx_dev_only_2, "SELECT COUNT(*) FROM db.upstream") == 3
        assert ctx_dev_only_2.engine_adapter.fetchall(
            "SELECT id, val, ds FROM db.downstream ORDER BY ds"
        ) == [
            (1, 99, to_date("2023-01-05")),
            (1, 99, to_date("2023-01-06")),
            (1, 99, to_date("2023-01-07")),
        ]
        ctx_dev_only_2.close()

        # 4. Migrate prod to FULL again, then migrate back to DEV_ONLY with skip_backfill=True
        #    followed by ctx.run() -> stale 'novde' intervals must be purged so run() backfills!
        ctx_full_2 = make_ctx(VirtualEnvironmentMode.FULL)
        ctx_full_2.plan("prod", auto_apply=True, no_prompts=True)
        ctx_full_2.close()

        ctx_dev_only_3 = make_ctx(VirtualEnvironmentMode.DEV_ONLY)
        plan_skip_backfill = ctx_dev_only_3.plan_builder("prod", skip_backfill=True).build()
        ctx_dev_only_3.apply(plan_skip_backfill)
        assert _scalar(ctx_dev_only_3, "SELECT COUNT(*) FROM db.upstream") == 0
        assert _scalar(ctx_dev_only_3, "SELECT COUNT(*) FROM db.downstream") == 0

        ctx_dev_only_3.run(skip_janitor=True)
        assert _scalar(ctx_dev_only_3, "SELECT COUNT(*) FROM db.full_m") == 2
        assert _scalar(ctx_dev_only_3, "SELECT COUNT(*) FROM db.upstream") == 3
        assert _scalar(ctx_dev_only_3, "SELECT COUNT(*) FROM db.downstream") == 3


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_destructive_change_checks_with_vde_mode_transitions_and_per_model_overrides(
    tmp_path: Path,
) -> None:
    with ExitStack() as stack:

        def make_ctx(proj_dir: Path, db_file: Path, mode: VirtualEnvironmentMode) -> Context:
            ctx = Context(
                paths=proj_dir,
                config=Config(
                    gateways={
                        "": GatewayConfig(connection=DuckDBConnectionConfig(database=str(db_file)))
                    },
                    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
                    virtual_environment_mode=mode,
                ),
            )
            stack.callback(ctx.close)
            return ctx

        # 1. FULL -> DEV_ONLY transition while dropping a column on an INCREMENTAL model with
        #    on_destructive_change=error is a full rebuild (not forward-only) -> must NOT raise PlanError!
        proj_transition = tmp_path / "proj_transition"
        models_transition = proj_transition / "models"
        models_transition.mkdir(parents=True)
        db_transition = proj_transition / "warehouse.db"

        (models_transition / "inc_m.sql").write_text(
            """
            MODEL (
                name db.inc_m,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1,
                    on_destructive_change error
                ),
                start '2023-01-05'
            );
            SELECT 1::INT AS id, 2::INT AS dropped_col, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )

        ctx_full = make_ctx(proj_transition, db_transition, VirtualEnvironmentMode.FULL)
        ctx_full.plan("prod", auto_apply=True, no_prompts=True)
        ctx_full.close()

        (models_transition / "inc_m.sql").write_text(
            """
            MODEL (
                name db.inc_m,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1,
                    on_destructive_change error
                ),
                start '2023-01-05'
            );
            SELECT 1::INT AS id, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )

        ctx_dev_only = make_ctx(proj_transition, db_transition, VirtualEnvironmentMode.DEV_ONLY)
        plan_dev = ctx_dev_only.plan_builder("dev").build()
        ctx_dev_only.apply(plan_dev)
        plan_prod = ctx_dev_only.plan_builder("prod").build()
        ctx_dev_only.apply(plan_prod)
        assert list(ctx_dev_only.engine_adapter.columns("db.inc_m")) == ["id", "ds"]
        ctx_dev_only.close()

        # 2. Per-model DEV_ONLY incremental model inside a FULL project MUST enforce on_destructive_change=error!
        proj_per_model = tmp_path / "proj_per_model"
        models_per_model = proj_per_model / "models"
        models_per_model.mkdir(parents=True)
        db_per_model = proj_per_model / "warehouse.db"

        (models_per_model / "per_model_dev_only.sql").write_text(
            """
            MODEL (
                name db.per_model_dev_only,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1,
                    on_destructive_change error
                ),
                virtual_environment_mode 'dev_only',
                start '2023-01-05'
            );
            SELECT 1::INT AS id, 2::INT AS dropped_col, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )

        ctx_mixed = make_ctx(proj_per_model, db_per_model, VirtualEnvironmentMode.FULL)
        ctx_mixed.plan("prod", auto_apply=True, no_prompts=True)

        (models_per_model / "per_model_dev_only.sql").write_text(
            """
            MODEL (
                name db.per_model_dev_only,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    batch_size 1,
                    on_destructive_change error
                ),
                virtual_environment_mode 'dev_only',
                start '2023-01-05'
            );
            SELECT 1::INT AS id, @start_ds::DATE AS ds;
            """,
            encoding="utf-8",
        )
        ctx_mixed.load()

        with pytest.raises(
            PlanError, match="Plan requires a destructive change to a forward-only model"
        ):
            ctx_mixed.plan_builder("dev").build()

        with pytest.raises(
            PlanError, match="Plan requires a destructive change to a forward-only model"
        ):
            ctx_mixed.plan_builder("prod").build()
