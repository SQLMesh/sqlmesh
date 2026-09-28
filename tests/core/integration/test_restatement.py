from __future__ import annotations

import typing as t
import pandas as pd  # noqa: TID253
import pytest
from pathlib import Path
from sqlmesh.core.console import (
    MarkdownConsole,
    set_console,
    get_console,
    CaptureTerminalConsole,
)
import time_machine
from sqlglot import exp
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import time
import queue

from sqlmesh.core import constants as c
from sqlmesh.core.config import (
    Config,
    GatewayConfig,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
)
from sqlmesh.core.context import Context
from sqlmesh.core.model import (
    IncrementalByTimeRangeKind,
    IncrementalUnmanagedKind,
    SqlModel,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotId,
)
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import (
    ConflictingPlanError,
)
from tests.core.integration.utils import add_projection_to_model

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_restatement_plan_ignores_changes(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    restated_snapshot = context.get_snapshot("sushi.top_waiters")

    # Simulate a change.
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    plan = context.plan_builder(restate_models=["sushi.top_waiters"]).build()
    assert plan.snapshots != context.snapshots

    assert not plan.directly_modified
    assert not plan.has_changes
    assert not plan.new_snapshots
    assert plan.requires_backfill
    assert plan.restatements == {
        restated_snapshot.snapshot_id: (to_timestamp("2023-01-01"), to_timestamp("2023-01-09"))
    }
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=restated_snapshot.snapshot_id,
            intervals=[
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

    context.apply(plan)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_prod_restatement_with_unbackfilled_dev_version(init_and_plan_context: t.Callable):
    """
    Scenario:
        Prod is built. A breaking change is planned to `dev` with `--skip-backfill`,
        so `dev` holds a different snapshot version with no interval rows. Prod is
        then restated.
    Outcome:
        RestatementStage tries to clear `dev` intervals for that other version, finds
        none in `_intervals`, and no-ops instead of crashing on an empty insert. Prod
        restatement still applies; the un-backfilled `dev` snapshot still has no
        intervals.
    """
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    prod_snapshot_id = context.get_snapshot("sushi.waiter_revenue_by_day").snapshot_id

    context.upsert_model(
        add_projection_to_model(t.cast(SqlModel, context.get_model("sushi.waiter_revenue_by_day")))
    )
    context.plan("dev", skip_backfill=True, auto_apply=True, no_prompts=True)

    dev_snapshot_id = context.get_snapshot("sushi.waiter_revenue_by_day").snapshot_id
    assert dev_snapshot_id != prod_snapshot_id
    assert not context.state_sync.get_snapshots([dev_snapshot_id])[dev_snapshot_id].intervals

    context.plan(
        restate_models=["sushi.waiter_revenue_by_day"],
        start="2023-01-07",
        end="2023-01-08",
        auto_apply=True,
        no_prompts=True,
    )

    assert not context.state_sync.get_snapshots([dev_snapshot_id])[dev_snapshot_id].intervals


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_restatement_plan_across_environments_snapshot_with_shared_version(
    init_and_plan_context: t.Callable,
):
    context, _ = init_and_plan_context("examples/sushi")

    # Change kind to incremental unmanaged
    model = context.get_model("sushi.waiter_revenue_by_day")
    previous_kind = model.kind.copy(update={"forward_only": True})
    assert isinstance(previous_kind, IncrementalByTimeRangeKind)

    model = model.copy(
        update={
            "kind": IncrementalUnmanagedKind(),
            "physical_version": "pinned_version_12345",
            "partitioned_by_": [exp.column("event_date")],
        }
    )
    context.upsert_model(model)
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Make some change and deploy it to both dev and prod environments
    model = add_projection_to_model(t.cast(SqlModel, model))
    context.upsert_model(model)
    context.plan("dev_a", auto_apply=True, no_prompts=True)
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Change the kind back to incremental by time range and deploy to prod
    model = model.copy(update={"kind": previous_kind})
    context.upsert_model(model)
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Restate the model and verify that the interval hasn't been expanded because of the old snapshot
    # with the same version
    context.plan(
        restate_models=["sushi.waiter_revenue_by_day"],
        start="2023-01-06",
        end="2023-01-08",
        auto_apply=True,
        no_prompts=True,
    )

    assert (
        context.fetchdf(
            "SELECT COUNT(*) AS cnt FROM sushi.waiter_revenue_by_day WHERE one IS NOT NULL AND event_date < '2023-01-06'"
        )["cnt"][0]
        == 0
    )
    plan = context.plan_builder("prod").build()
    assert not plan.missing_intervals


def test_restatement_plan_hourly_with_downstream_daily_restates_correct_intervals(tmp_path: Path):
    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@hourly'
    );

    select account_id, ts from test.external_table;
    """

    model_b = """
    MODEL (
        name test.b,
        kind FULL,
        cron '@daily'
    );

    select account_id, ts from test.a;
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    for path, defn in {"a.sql": model_a, "b.sql": model_b}.items():
        with open(models_dir / path, "w") as f:
            f.write(defn)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-01 01:30:00",
                "2024-01-01 02:30:00",
                "2024-01-02 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply
    ctx.plan(auto_apply=True, no_prompts=True)

    def _dates_in_table(table_name: str) -> t.List[str]:
        return [
            str(r[0]) for r in engine_adapter.fetchall(f"select ts from {table_name} order by ts")
        ]

    # verify initial state
    for tbl in ["test.a", "test.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 01:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ]

    # restate A
    engine_adapter.execute("delete from test.external_table where ts = '2024-01-01 01:30:00'")
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-01 01:00:00",
        end="2024-01-01 02:00:00",
        auto_apply=True,
        no_prompts=True,
    )

    # verify result
    for tbl in ["test.a", "test.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ], f"Table {tbl} wasnt cleared"

    # Put some data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 01:30:00",
                "2024-01-01 23:30:00",
                "2024-01-02 03:30:00",
                "2024-01-03 12:30:00",
            ],
        }
    )
    engine_adapter.replace_query(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # Restate A across a day boundary with the expectation that two day intervals in B are affected
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-01 02:00:00",
        end="2024-01-02 04:00:00",
        auto_apply=True,
        no_prompts=True,
    )

    for tbl in ["test.a", "test.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",  # present already
            # "2024-01-01 02:30:00", #removed in last restatement
            "2024-01-01 23:30:00",  # added in last restatement
            "2024-01-02 03:30:00",  # added in last restatement
        ], f"Table {tbl} wasnt cleared"


def test_restatement_plan_respects_disable_restatements(tmp_path: Path):
    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01',
        cron '@daily'
    );

    select account_id, ts from test.external_table;
    """

    model_b = """
    MODEL (
        name test.b,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts",
            disable_restatement true,
        ),
        start '2024-01-01',
        cron '@daily'
    );

    select account_id, ts from test.a;
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    for path, defn in {"a.sql": model_a, "b.sql": model_b}.items():
        with open(models_dir / path, "w") as f:
            f.write(defn)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-01 01:30:00",
                "2024-01-01 02:30:00",
                "2024-01-02 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply
    ctx.plan(auto_apply=True, no_prompts=True)

    def _dates_in_table(table_name: str) -> t.List[str]:
        return [
            str(r[0]) for r in engine_adapter.fetchall(f"select ts from {table_name} order by ts")
        ]

    def get_snapshot_intervals(snapshot_id):
        return list(ctx.state_sync.get_snapshots([snapshot_id]).values())[0].intervals

    # verify initial state
    for tbl in ["test.a", "test.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 01:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ]

    # restate A and expect b to be ignored
    starting_b_intervals = get_snapshot_intervals(ctx.snapshots['"memory"."test"."b"'].snapshot_id)
    engine_adapter.execute("delete from test.external_table where ts = '2024-01-01 01:30:00'")
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-01",
        end="2024-01-02",
        auto_apply=True,
        no_prompts=True,
    )

    # verify A was changed and not b
    assert _dates_in_table("test.a") == [
        "2024-01-01 00:30:00",
        "2024-01-01 02:30:00",
        "2024-01-02 00:30:00",
    ]
    assert _dates_in_table("test.b") == [
        "2024-01-01 00:30:00",
        "2024-01-01 01:30:00",
        "2024-01-01 02:30:00",
        "2024-01-02 00:30:00",
    ]

    # Verify B intervals were not touched
    b_intervals = get_snapshot_intervals(ctx.snapshots['"memory"."test"."b"'].snapshot_id)
    assert starting_b_intervals == b_intervals


def test_restatement_plan_clears_correct_intervals_across_environments(tmp_path: Path):
    model1 = """
    MODEL (
        name test.incremental_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "date"
        ),
        start '2024-01-01',
        cron '@daily'
    );

    select account_id, date from test.external_table;
    """

    model2 = """
    MODEL (
        name test.downstream_of_incremental,
        kind FULL
    );

    select account_id, date from test.incremental_model;
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    with open(models_dir / "model1.sql", "w") as f:
        f.write(model1)

    with open(models_dir / "model2.sql", "w") as f:
        f.write(model2)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004, 1005],
            "name": ["foo", "bar", "baz", "bing", "bong"],
            "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "name": exp.DataType.build("varchar"),
        "date": exp.DataType.build("date"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # first, create the prod models
    ctx.plan(auto_apply=True, no_prompts=True)
    assert engine_adapter.fetchone("select count(*) from test.incremental_model") == (5,)
    assert engine_adapter.fetchone("select count(*) from test.downstream_of_incremental") == (5,)
    assert not engine_adapter.table_exists("test__dev.incremental_model")

    # then, make a dev version
    model1 = """
    MODEL (
        name test.incremental_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "date"
        ),
        start '2024-01-01',
        cron '@daily'
    );

    select 1 as account_id, date from test.external_table;
    """
    with open(models_dir / "model1.sql", "w") as f:
        f.write(model1)
    ctx.load()

    ctx.plan(environment="dev", auto_apply=True, no_prompts=True)
    assert engine_adapter.table_exists("test__dev.incremental_model")
    assert engine_adapter.fetchone("select count(*) from test__dev.incremental_model") == (5,)

    # drop some source data so when we restate the interval it essentially clears it which is easy to verify
    engine_adapter.execute("delete from test.external_table where date = '2024-01-01'")
    assert engine_adapter.fetchone("select count(*) from test.external_table") == (4,)

    # now, restate intervals in dev and verify prod is NOT affected
    ctx.plan(
        environment="dev",
        start="2024-01-01",
        end="2024-01-02",
        restate_models=["test.incremental_model"],
        auto_apply=True,
        no_prompts=True,
    )
    assert engine_adapter.fetchone("select count(*) from test.incremental_model") == (5,)
    assert engine_adapter.fetchone(
        "select count(*) from test.incremental_model where date = '2024-01-01'"
    ) == (1,)
    assert engine_adapter.fetchone("select count(*) from test__dev.incremental_model") == (4,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-01'"
    ) == (0,)

    # prod still should not be affected by a run because the restatement only happened in dev
    ctx.run()
    assert engine_adapter.fetchone("select count(*) from test.incremental_model") == (5,)
    assert engine_adapter.fetchone(
        "select count(*) from test.incremental_model where date = '2024-01-01'"
    ) == (1,)

    # drop another interval from the source data
    engine_adapter.execute("delete from test.external_table where date = '2024-01-02'")

    # now, restate intervals in prod and verify that dev IS affected
    ctx.plan(
        start="2024-01-01",
        end="2024-01-03",
        restate_models=["test.incremental_model"],
        auto_apply=True,
        no_prompts=True,
    )
    assert engine_adapter.fetchone("select count(*) from test.incremental_model") == (3,)
    assert engine_adapter.fetchone(
        "select count(*) from test.incremental_model where date = '2024-01-01'"
    ) == (0,)
    assert engine_adapter.fetchone(
        "select count(*) from test.incremental_model where date = '2024-01-02'"
    ) == (0,)
    assert engine_adapter.fetchone(
        "select count(*) from test.incremental_model where date = '2024-01-03'"
    ) == (1,)

    # dev not affected yet until `sqlmesh run` is run
    assert engine_adapter.fetchone("select count(*) from test__dev.incremental_model") == (4,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-01'"
    ) == (0,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-02'"
    ) == (1,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-03'"
    ) == (1,)

    # the restatement plan for prod should have cleared dev intervals too, which means this `sqlmesh run` re-runs 2024-01-01 and 2024-01-02
    ctx.run(environment="dev")
    assert engine_adapter.fetchone("select count(*) from test__dev.incremental_model") == (3,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-01'"
    ) == (0,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-02'"
    ) == (0,)
    assert engine_adapter.fetchone(
        "select count(*) from test__dev.incremental_model where date = '2024-01-03'"
    ) == (1,)

    # the downstream full model should always reflect whatever the incremental model is showing
    assert engine_adapter.fetchone("select count(*) from test.downstream_of_incremental") == (3,)
    assert engine_adapter.fetchone("select count(*) from test__dev.downstream_of_incremental") == (
        3,
    )


def test_prod_restatement_plan_clears_correct_intervals_in_derived_dev_tables(tmp_path: Path):
    """
    Scenario:
        I have models A[hourly] <- B[daily] <- C in prod
        I create dev and add 2 new models D and E so that my dev DAG looks like A <- B <- C <- D[daily] <- E
        I prod, I restate *one hour* of A
    Outcome:
        D and E should be restated in dev despite not being a part of prod
        since B and D are daily, the whole day should be restated even though only 1hr of the upstream model was restated
    """

    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@hourly'
    );

    select account_id, ts from test.external_table;
    """

    def _derived_full_model_def(name: str, upstream: str) -> str:
        return f"""
        MODEL (
            name test.{name},
            kind FULL
        );

        select account_id, ts from test.{upstream};
        """

    def _derived_incremental_model_def(name: str, upstream: str) -> str:
        return f"""
        MODEL (
            name test.{name},
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ts
            ),
            cron '@daily'
        );

        select account_id, ts from test.{upstream} where ts between @start_ts and @end_ts;
        """

    model_b = _derived_incremental_model_def("b", upstream="a")
    model_c = _derived_full_model_def("c", upstream="b")

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    for path, defn in {"a.sql": model_a, "b.sql": model_b, "c.sql": model_c}.items():
        with open(models_dir / path, "w") as f:
            f.write(defn)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-01 01:30:00",
                "2024-01-01 02:30:00",
                "2024-01-02 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply A, B, C in prod
    ctx.plan(auto_apply=True, no_prompts=True)

    # add D[daily], E in dev
    model_d = _derived_incremental_model_def("d", upstream="c")
    model_e = _derived_full_model_def("e", upstream="d")

    for path, defn in {
        "d.sql": model_d,
        "e.sql": model_e,
    }.items():
        with open(models_dir / path, "w") as f:
            f.write(defn)

    # plan + apply dev
    ctx.load()
    ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

    def _dates_in_table(table_name: str) -> t.List[str]:
        return [
            str(r[0]) for r in engine_adapter.fetchall(f"select ts from {table_name} order by ts")
        ]

    # verify initial state
    for tbl in ["test.a", "test.b", "test.c", "test__dev.d", "test__dev.e"]:
        assert engine_adapter.table_exists(tbl)
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 01:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ]

    for tbl in ["test.d", "test.e"]:
        assert not engine_adapter.table_exists(tbl)

    # restate A in prod
    engine_adapter.execute("delete from test.external_table where ts = '2024-01-01 01:30:00'")
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-01 01:00:00",
        end="2024-01-01 02:00:00",
        auto_apply=True,
        no_prompts=True,
    )

    # verify result
    for tbl in ["test.a", "test.b", "test.c"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ], f"Table {tbl} wasnt cleared"

    # dev shouldnt have been affected yet
    for tbl in ["test__dev.d", "test__dev.e"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 01:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ], f"Table {tbl} was prematurely cleared"

    # run dev to trigger the processing of the prod restatement
    ctx.run(environment="dev")

    # data should now be cleared from dev
    # note that D is a daily model, so clearing an hour interval from A should have triggered the full day in D
    for tbl in ["test__dev.d", "test__dev.e"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ], f"Table {tbl} wasnt cleared"


def test_prod_restatement_plan_clears_unaligned_intervals_in_derived_dev_tables(tmp_path: Path):
    """
    Scenario:
        I have a model A[hourly] in prod
        I create dev and add a model B[daily]
        I prod, I restate *one hour* of A

    Outcome:
        The whole day for B should be restated. The restatement plan for prod has no hints about B's cadence because
        B only exists in dev and there are no other downstream models in prod that would cause the restatement intervals
        to be widened.

        Therefore, this test checks that SQLMesh does the right thing when an interval is partially cleared
    """

    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@hourly'
    );

    select account_id, ts from test.external_table;
    """

    model_b = """
        MODEL (
            name test.b,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ts
            ),
            cron '@daily'
        );

        select account_id, ts from test.a where ts between @start_ts and @end_ts;
        """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    with open(models_dir / "a.sql", "w") as f:
        f.write(model_a)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-01 01:30:00",
                "2024-01-01 02:30:00",
                "2024-01-02 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply A[hourly] in prod
    ctx.plan(auto_apply=True, no_prompts=True)

    # add B[daily] in dev
    with open(models_dir / "b.sql", "w") as f:
        f.write(model_b)

    # plan + apply dev
    ctx.load()
    ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

    def _dates_in_table(table_name: str) -> t.List[str]:
        return [
            str(r[0]) for r in engine_adapter.fetchall(f"select ts from {table_name} order by ts")
        ]

    # verify initial state
    for tbl in ["test.a", "test__dev.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 01:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ]

    # restate A in prod
    engine_adapter.execute("delete from test.external_table where ts = '2024-01-01 01:30:00'")
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-01 01:00:00",
        end="2024-01-01 02:00:00",
        auto_apply=True,
        no_prompts=True,
    )

    # verify result
    assert _dates_in_table("test.a") == [
        "2024-01-01 00:30:00",
        "2024-01-01 02:30:00",
        "2024-01-02 00:30:00",
    ]

    # dev shouldnt have been affected yet
    assert _dates_in_table("test__dev.b") == [
        "2024-01-01 00:30:00",
        "2024-01-01 01:30:00",
        "2024-01-01 02:30:00",
        "2024-01-02 00:30:00",
    ]

    # mess with A independently of SQLMesh to prove a whole day gets restated for B instead of just 1hr
    snapshot_table_name = ctx.table_name("test.a", "dev")
    engine_adapter.execute(
        f"delete from {snapshot_table_name} where cast(ts as date) == '2024-01-01'"
    )
    engine_adapter.execute(
        f"insert into {snapshot_table_name} (account_id, ts) values (1007, '2024-01-02 01:30:00')"
    )

    assert _dates_in_table("test.a") == ["2024-01-02 00:30:00", "2024-01-02 01:30:00"]

    # run dev to trigger the processing of the prod restatement
    ctx.run(environment="dev")

    # B should now have no data for 2024-01-01
    # To prove a single day was restated vs the whole model, it also shouldnt have the '2024-01-02 01:30:00' record
    assert _dates_in_table("test__dev.b") == ["2024-01-02 00:30:00"]


def test_prod_restatement_plan_causes_dev_intervals_to_be_processed_in_next_dev_plan(
    tmp_path: Path,
):
    """
    Scenario:
        I have a model A[hourly] in prod
        I create dev and add a model B[daily]
        I prod, I restate *one hour* of A
        In dev, I run a normal plan instead of a cadence run

    Outcome:
        The whole day for B should be restated as part of a normal plan
    """

    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@hourly'
    );

    select account_id, ts from test.external_table;
    """

    model_b = """
        MODEL (
            name test.b,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ts
            ),
            cron '@daily'
        );

        select account_id, ts from test.a where ts between @start_ts and @end_ts;
        """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    with open(models_dir / "a.sql", "w") as f:
        f.write(model_a)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-01 01:30:00",
                "2024-01-01 02:30:00",
                "2024-01-02 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply A[hourly] in prod
    ctx.plan(auto_apply=True, no_prompts=True)

    # add B[daily] in dev
    with open(models_dir / "b.sql", "w") as f:
        f.write(model_b)

    # plan + apply dev
    ctx.load()
    ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

    def _dates_in_table(table_name: str) -> t.List[str]:
        return [
            str(r[0]) for r in engine_adapter.fetchall(f"select ts from {table_name} order by ts")
        ]

    # verify initial state
    for tbl in ["test.a", "test__dev.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 01:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ]

    # restate A in prod
    engine_adapter.execute("delete from test.external_table where ts = '2024-01-01 01:30:00'")
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-01 01:00:00",
        end="2024-01-01 02:00:00",
        auto_apply=True,
        no_prompts=True,
    )

    # verify result
    assert _dates_in_table("test.a") == [
        "2024-01-01 00:30:00",
        "2024-01-01 02:30:00",
        "2024-01-02 00:30:00",
    ]

    # dev shouldnt have been affected yet
    assert _dates_in_table("test__dev.b") == [
        "2024-01-01 00:30:00",
        "2024-01-01 01:30:00",
        "2024-01-01 02:30:00",
        "2024-01-02 00:30:00",
    ]

    # plan dev which should trigger the missing intervals to get repopulated
    ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

    # dev should have the restated data
    for tbl in ["test.a", "test__dev.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-01 02:30:00",
            "2024-01-02 00:30:00",
        ]


def test_prod_restatement_plan_causes_dev_intervals_to_be_widened_on_full_restatement_only_model(
    tmp_path,
):
    """
    Scenario:
        I have am INCREMENTAL_BY_TIME_RANGE model A[daily] in prod
        I create dev and add a INCREMENTAL_BY_UNIQUE_KEY model B (which supports full restatement only)
        I prod, I restate one day of A which should cause intervals in dev to be cleared (but not processed)
        In dev, I run a plan

    Outcome:
        In the dev plan, the entire model for B should be rebuilt because it does not support partial restatement
    """

    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@daily'
    );

    select account_id, ts from test.external_table where ts between @start_ts and @end_ts;
    """

    model_b = """
        MODEL (
            name test.b,
            kind INCREMENTAL_BY_UNIQUE_KEY (
                unique_key (account_id, ts)
            ),
            cron '@daily'
        );

        select account_id, ts from test.a where ts between @start_ts and @end_ts;
        """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    with open(models_dir / "a.sql", "w") as f:
        f.write(model_a)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-02 01:30:00",
                "2024-01-03 02:30:00",
                "2024-01-04 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply A[daily] in prod
    ctx.plan(auto_apply=True)

    # add B[daily] in dev
    with open(models_dir / "b.sql", "w") as f:
        f.write(model_b)

    # plan + apply dev
    ctx.load()
    ctx.plan(environment="dev", auto_apply=True)

    def _dates_in_table(table_name: str) -> t.List[str]:
        return [
            str(r[0]) for r in engine_adapter.fetchall(f"select ts from {table_name} order by ts")
        ]

    # verify initial state
    for tbl in ["test.a", "test__dev.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-02 01:30:00",
            "2024-01-03 02:30:00",
            "2024-01-04 00:30:00",
        ]

    # restate A in prod
    engine_adapter.execute("delete from test.external_table where ts = '2024-01-02 01:30:00'")
    ctx.plan(
        restate_models=["test.a"],
        start="2024-01-02 00:00:00",
        end="2024-01-03 00:00:00",
        auto_apply=True,
        no_prompts=True,
    )

    # verify result
    assert _dates_in_table("test.a") == [
        "2024-01-01 00:30:00",
        "2024-01-03 02:30:00",
        "2024-01-04 00:30:00",
    ]

    # dev shouldnt have been affected yet
    assert _dates_in_table("test__dev.b") == [
        "2024-01-01 00:30:00",
        "2024-01-02 01:30:00",
        "2024-01-03 02:30:00",
        "2024-01-04 00:30:00",
    ]

    # plan dev which should trigger the missing intervals to get repopulated
    ctx.plan(environment="dev", auto_apply=True)

    # dev should have fully refreshed
    # this is proven by the fact that INCREMENTAL_BY_UNIQUE_KEY cant propagate deletes, so if the
    # model was not fully rebuilt, the deleted record would still be present
    for tbl in ["test.a", "test__dev.b"]:
        assert _dates_in_table(tbl) == [
            "2024-01-01 00:30:00",
            "2024-01-03 02:30:00",
            "2024-01-04 00:30:00",
        ]


def test_prod_restatement_plan_missing_model_in_dev(
    tmp_path: Path,
):
    """
    Scenario:
        I have a model B in prod but only model A in dev
        I restate B in prod

    Outcome:
        The A model should be ignore and the plan shouldn't fail
    """

    model_a = """
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@hourly'
    );

    select account_id, ts from test.external_table;
    """

    model_b = """
        MODEL (
            name test.b,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ts
            ),
            cron '@daily'
        );

        select account_id, ts from test.external_table where ts between @start_ts and @end_ts;
        """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    with open(models_dir / "a.sql", "w") as f:
        f.write(model_a)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    ctx = Context(paths=[tmp_path], config=config)

    engine_adapter = ctx.engine_adapter
    engine_adapter.create_schema("test")

    # source data
    df = pd.DataFrame(
        {
            "account_id": [1001, 1002, 1003, 1004],
            "ts": [
                "2024-01-01 00:30:00",
                "2024-01-01 01:30:00",
                "2024-01-01 02:30:00",
                "2024-01-02 00:30:00",
            ],
        }
    )
    columns_to_types = {
        "account_id": exp.DataType.build("int"),
        "ts": exp.DataType.build("timestamp"),
    }
    external_table = exp.table_(table="external_table", db="test", quoted=True)
    engine_adapter.create_table(table_name=external_table, target_columns_to_types=columns_to_types)
    engine_adapter.insert_append(
        table_name=external_table, query_or_df=df, target_columns_to_types=columns_to_types
    )

    # plan + apply A[hourly] in dev
    ctx.plan("dev", auto_apply=True, no_prompts=True)

    # add B[daily] in prod and remove A
    with open(models_dir / "b.sql", "w") as f:
        f.write(model_b)
    Path(models_dir / "a.sql").unlink()

    # plan + apply dev
    ctx.load()
    ctx.plan(auto_apply=True, no_prompts=True)

    # restate B in prod
    ctx.plan(
        restate_models=["test.b"],
        start="2024-01-01",
        end="2024-01-02",
        auto_apply=True,
        no_prompts=True,
    )


def test_prod_restatement_plan_includes_related_unpromoted_snapshots(tmp_path: Path):
    """
    Scenario:
        - I have models A <- B in prod
        - I have models A <- B <- C in dev
        - Both B and C have gone through a few iterations in dev so multiple snapshot versions exist
          for them but not all of them are promoted / active
        - I restate A in prod

    Outcome:
        - Intervals should be cleared for all of the versions of B and C, regardless
          of if they are active in any particular environment, in case they ever get made
          active
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    (models_dir / "a.sql").write_text("""
    MODEL (
        name test.a,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@daily'
    );

    select 1 as a, now() as ts;
    """)

    (models_dir / "b.sql").write_text("""
    MODEL (
        name test.b,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@daily'
    );

    select a, ts from test.a
    """)

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"))
    ctx = Context(paths=[tmp_path], config=config)

    def _all_snapshots() -> t.Dict[SnapshotId, Snapshot]:
        all_snapshot_ids = [
            SnapshotId(name=name, identifier=identifier)
            for (name, identifier) in ctx.state_sync.state_sync.engine_adapter.fetchall(  # type: ignore
                "select name, identifier from sqlmesh._snapshots"
            )
        ]
        return ctx.state_sync.get_snapshots(all_snapshot_ids)

    # plan + apply prod
    ctx.plan(environment="prod", auto_apply=True)
    assert len(_all_snapshots()) == 2

    # create dev with new version of B
    (models_dir / "b.sql").write_text("""
    MODEL (
        name test.b,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@daily'
    );

    select a, ts, 'b dev 1' as change from test.a
    """)

    ctx.load()
    ctx.plan(environment="dev", auto_apply=True)
    assert len(_all_snapshots()) == 3

    # update B (new version) and create C
    (models_dir / "b.sql").write_text("""
    MODEL (
        name test.b,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column "ts"
        ),
        start '2024-01-01 00:00:00',
        cron '@daily'
    );

    select a, ts, 'b dev 2' as change from test.a
    """)

    (models_dir / "c.sql").write_text("""
    MODEL (
        name test.c,
        kind FULL,
        cron '@daily'
    );

    select *, 'c initial' as val from test.b
    """)

    ctx.load()
    ctx.plan(environment="dev", auto_apply=True)
    assert len(_all_snapshots()) == 5

    # update C (new version), create D (unrelated)
    (models_dir / "c.sql").write_text("""
    MODEL (
        name test.c,
        kind FULL,
        cron '@daily'
    );

    select *, 'c updated' as val from test.b
    """)

    (models_dir / "d.sql").write_text("""
    MODEL (
        name test.d,
        cron '@daily'
    );

    select 1 as unrelated
    """)

    ctx.load()
    ctx.plan(environment="dev", auto_apply=True)
    all_snapshots_prior_to_restatement = _all_snapshots()
    assert len(all_snapshots_prior_to_restatement) == 7

    def _snapshot_instances(lst: t.Dict[SnapshotId, Snapshot], name_match: str) -> t.List[Snapshot]:
        return [s for s_id, s in lst.items() if name_match in s_id.name]

    # verify initial state

    # 1 instance of A (prod)
    assert len(_snapshot_instances(all_snapshots_prior_to_restatement, '"a"')) == 1

    # 3 instances of B (original in prod + 2 updates in dev)
    assert len(_snapshot_instances(all_snapshots_prior_to_restatement, '"b"')) == 3

    # 2 instances of C (initial + update in dev)
    assert len(_snapshot_instances(all_snapshots_prior_to_restatement, '"c"')) == 2

    # 1 instance of D (initial - dev)
    assert len(_snapshot_instances(all_snapshots_prior_to_restatement, '"d"')) == 1

    # restate A in prod
    ctx.plan(environment="prod", restate_models=['"memory"."test"."a"'], auto_apply=True)

    all_snapshots_after_restatement = _all_snapshots()

    # All versions of B and C in dev should have had intervals cleared
    # D in dev should not be touched and A + B in prod shoud also not be touched
    a = _snapshot_instances(all_snapshots_after_restatement, '"a"')
    assert len(a) == 1

    b = _snapshot_instances(all_snapshots_after_restatement, '"b"')
    # the 1 B instance in prod should be populated and 2 in dev (1 active) should be cleared
    assert len(b) == 3
    assert len([s for s in b if not s.intervals]) == 2

    c = _snapshot_instances(all_snapshots_after_restatement, '"c"')
    # the 2 instances of C in dev (1 active) should be cleared
    assert len(c) == 2
    assert len([s for s in c if not s.intervals]) == 2

    d = _snapshot_instances(all_snapshots_after_restatement, '"d"')
    # D should not be touched since it's in no way downstream of A in prod
    assert len(d) == 1
    assert d[0].intervals


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_dev_restatement_of_prod_model(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    context.plan("dev", auto_apply=True, no_prompts=True, skip_tests=True)

    restatement_plan = context.plan_builder("dev", restate_models=["*"]).build()
    assert set(restatement_plan.restatements) == {
        context.get_snapshot("sushi.waiter_revenue_by_day").snapshot_id,
        context.get_snapshot("sushi.top_waiters").snapshot_id,
    }


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_restatement_of_full_model_with_start(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    restatement_plan = context.plan(
        restate_models=["sushi.customers"],
        start="2023-01-07",
        auto_apply=True,
        no_prompts=True,
    )

    sushi_customer_interval = restatement_plan.restatements[
        context.get_snapshot("sushi.customers").snapshot_id
    ]
    assert sushi_customer_interval == (to_timestamp("2023-01-01"), to_timestamp("2023-01-09"))
    waiter_by_day_interval = restatement_plan.restatements[
        context.get_snapshot("sushi.waiter_as_customer_by_day").snapshot_id
    ]
    assert waiter_by_day_interval == (to_timestamp("2023-01-07"), to_timestamp("2023-01-08"))


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_restatement_should_not_override_environment_statements(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")
    context.config.before_all = ["SELECT 'test_before_all';", *context.config.before_all]
    context.load()

    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)

    prod_env_statements = context.state_reader.get_environment_statements(c.PROD)
    assert prod_env_statements[0].before_all[0] == "SELECT 'test_before_all';"

    context.plan(
        restate_models=["sushi.waiter_revenue_by_day"],
        start="2023-01-07",
        auto_apply=True,
        no_prompts=True,
    )

    prod_env_statements = context.state_reader.get_environment_statements(c.PROD)
    assert prod_env_statements[0].before_all[0] == "SELECT 'test_before_all';"


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_restatement_shouldnt_backfill_beyond_prod_intervals(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    model = context.get_model("sushi.top_waiters")
    context.upsert_model(SqlModel.parse_obj({**model.dict(), "cron": "@hourly"}))

    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)
    context.run()

    with time_machine.travel("2023-01-09 02:00:00 UTC"):
        # It's time to backfill the waiter_revenue_by_day model but it hasn't run yet
        restatement_plan = context.plan(
            restate_models=["sushi.waiter_revenue_by_day"],
            no_prompts=True,
            skip_tests=True,
        )
        intervals_by_id = {i.snapshot_id: i for i in restatement_plan.missing_intervals}
        # Make sure the intervals don't go beyond the prod intervals
        assert intervals_by_id[context.get_snapshot("sushi.top_waiters").snapshot_id].intervals[-1][
            1
        ] == to_timestamp("2023-01-08 15:00:00 UTC")
        assert intervals_by_id[
            context.get_snapshot("sushi.waiter_revenue_by_day").snapshot_id
        ].intervals[-1][1] == to_timestamp("2023-01-08 00:00:00 UTC")


def test_restatement_plan_interval_external_visibility(tmp_path: Path):
    """
    Scenario:
        - `prod` environment exists, models A <- B
        - `dev` environment created, models A <- B(dev) <- C (dev)
        - Restatement plan is triggered against `prod` for model A
        - During restatement, a new dev environment `dev_2` is created with a new version of B(dev_2)

    Outcome:
        - At no point are the prod_intervals considered "missing" from state for A
        - The intervals for B(dev) and C(dev) are cleared
        - The intervals for B(dev_2) are also cleared even though the environment didnt exist at the time the plan was started,
          because they are based on the data from a partially restated version of A
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    lock_file_path = tmp_path / "test.lock"  # python model blocks while this file is present

    evaluation_lock_file_path = (
        tmp_path / "evaluation.lock"
    )  # python model creates this file if it's in the wait loop and deletes it once done

    # Note: to make execution block so we can test stuff, we use a Python model that blocks until it no longer detects the presence of a file
    (models_dir / "model_a.py").write_text(f"""
from sqlmesh.core.model import model
from sqlmesh.core.macros import MacroEvaluator

@model(
    "test.model_a",
    is_sql=True,
    kind="FULL"
)
def entrypoint(evaluator: MacroEvaluator) -> str:
    from pathlib import Path
    import time

    if evaluator.runtime_stage == 'evaluating':
        while True:
            if Path("{str(lock_file_path)}").exists():
                Path("{str(evaluation_lock_file_path)}").touch()
                print("lock exists; sleeping")
                time.sleep(2)
            else:
                Path("{str(evaluation_lock_file_path)}").unlink(missing_ok=True)
                break

    return "select 'model_a' as m"
""")

    (models_dir / "model_b.sql").write_text("""
    MODEL (
        name test.model_b,
        kind FULL
    );

    select a.m as m, 'model_b' as mb from test.model_a as a
    """)

    config = Config(
        gateways={
            "": GatewayConfig(
                connection=DuckDBConnectionConfig(database=str(tmp_path / "db.db")),
                state_connection=DuckDBConnectionConfig(database=str(tmp_path / "state.db")),
            )
        },
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    )
    ctx = Context(paths=[tmp_path], config=config)

    ctx.plan(environment="prod", auto_apply=True)

    assert len(ctx.snapshots) == 2
    assert all(s.intervals for s in ctx.snapshots.values())

    prod_model_a_snapshot_id = ctx.snapshots['"db"."test"."model_a"'].snapshot_id
    prod_model_b_snapshot_id = ctx.snapshots['"db"."test"."model_b"'].snapshot_id

    # dev models
    # new version of B
    (models_dir / "model_b.sql").write_text("""
    MODEL (
        name test.model_b,
        kind FULL
    );

    select a.m as m, 'model_b' as mb, 'dev' as dev_version from test.model_a as a
    """)

    # add C
    (models_dir / "model_c.sql").write_text("""
    MODEL (
        name test.model_c,
        kind FULL
    );

    select b.*, 'model_c' as mc from test.model_b as b
    """)

    ctx.load()
    ctx.plan(environment="dev", auto_apply=True)

    dev_model_b_snapshot_id = ctx.snapshots['"db"."test"."model_b"'].snapshot_id
    dev_model_c_snapshot_id = ctx.snapshots['"db"."test"."model_c"'].snapshot_id

    assert dev_model_b_snapshot_id != prod_model_b_snapshot_id

    # now, we restate A in prod but touch the lockfile so it hangs during evaluation
    # we also have to do it in its own thread due to the hang
    lock_file_path.touch()

    def _run_restatement_plan(tmp_path: Path, config: Config, q: queue.Queue):
        q.put("thread_started")

        # give this thread its own Context object to prevent segfaulting the Python interpreter
        restatement_ctx = Context(paths=[tmp_path], config=config)

        # dev2 not present before the restatement plan starts
        assert restatement_ctx.state_sync.get_environment("dev2") is None

        q.put("plan_started")
        plan = restatement_ctx.plan(
            environment="prod", restate_models=['"db"."test"."model_a"'], auto_apply=True
        )
        q.put("plan_completed")

        # dev2 was created during the restatement plan
        assert restatement_ctx.state_sync.get_environment("dev2") is not None

        return plan

    executor = ThreadPoolExecutor()
    q: queue.Queue = queue.Queue()
    restatement_plan_future = executor.submit(_run_restatement_plan, tmp_path, config, q)
    assert q.get() == "thread_started"

    try:
        if e := restatement_plan_future.exception(timeout=1):
            # abort early if the plan thread threw an exception
            raise e
    except TimeoutError:
        # that's ok, we dont actually expect the plan to have finished in 1 second
        pass

    # while that restatement is running, we can simulate another process and check that it sees no empty intervals
    assert q.get() == "plan_started"

    # dont check for potentially missing intervals until the plan is in the evaluation loop
    attempts = 0
    while not evaluation_lock_file_path.exists():
        time.sleep(2)
        attempts += 1
        if attempts > 10:
            raise ValueError("Gave up waiting for evaluation loop")

    ctx.clear_caches()  # get rid of the file cache so that data is re-fetched from state
    prod_models_from_state = ctx.state_sync.get_snapshots(
        snapshot_ids=[prod_model_a_snapshot_id, prod_model_b_snapshot_id]
    )

    # prod intervals should be present still
    assert all(m.intervals for m in prod_models_from_state.values())

    # so should dev intervals since prod restatement is still running
    assert all(m.intervals for m in ctx.snapshots.values())

    # now, lets create a new dev environment "dev2", while the prod restatement plan is still running,
    # that changes model_b while still being based on the original version of model_a
    (models_dir / "model_b.sql").write_text("""
    MODEL (
        name test.model_b,
        kind FULL
    );

    select a.m as m, 'model_b' as mb, 'dev2' as dev_version from test.model_a as a
    """)
    ctx.load()
    ctx.plan(environment="dev2", auto_apply=True)

    dev2_model_b_snapshot_id = ctx.snapshots['"db"."test"."model_b"'].snapshot_id
    assert dev2_model_b_snapshot_id != dev_model_b_snapshot_id
    assert dev2_model_b_snapshot_id != prod_model_b_snapshot_id

    # as at this point, everything still has intervals
    ctx.clear_caches()
    assert all(
        s.intervals
        for s in ctx.state_sync.get_snapshots(
            snapshot_ids=[
                prod_model_a_snapshot_id,
                prod_model_b_snapshot_id,
                dev_model_b_snapshot_id,
                dev_model_c_snapshot_id,
                dev2_model_b_snapshot_id,
            ]
        ).values()
    )

    # now, we finally let that restatement plan complete
    # first, verify it's still blocked where it should be
    assert not restatement_plan_future.done()

    lock_file_path.unlink()  # remove lock file, plan should be able to proceed now

    if e := restatement_plan_future.exception():  # blocks until future complete
        raise e

    assert restatement_plan_future.result()
    assert q.get() == "plan_completed"

    ctx.clear_caches()

    # check that intervals in prod are present
    assert all(
        s.intervals
        for s in ctx.state_sync.get_snapshots(
            snapshot_ids=[
                prod_model_a_snapshot_id,
                prod_model_b_snapshot_id,
            ]
        ).values()
    )

    # check that intervals in dev have been cleared, including the dev2 env that
    # was created after the restatement plan started
    assert all(
        not s.intervals
        for s in ctx.state_sync.get_snapshots(
            snapshot_ids=[
                dev_model_b_snapshot_id,
                dev_model_c_snapshot_id,
                dev2_model_b_snapshot_id,
            ]
        ).values()
    )

    executor.shutdown()


def test_restatement_plan_detects_prod_deployment_during_restatement(tmp_path: Path):
    """
    Scenario:
        - `prod` environment exists, model A
        - `dev` environment created, model A(dev)
        - Restatement plan is triggered against `prod` for model A
        - During restatement, someone else deploys A(dev) to prod, replacing the model that is currently being restated.

    Outcome:
        - The deployment plan for dev -> prod should succeed in deploying the new version of A
        - The prod restatement plan should fail with a ConflictingPlanError and warn about the model that got updated while undergoing restatement
        - The new version of A should have no intervals cleared. The user needs to rerun the restatement if the intervals should still be cleared
    """
    orig_console = get_console()
    console = CaptureTerminalConsole()
    set_console(console)

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    lock_file_path = tmp_path / "test.lock"  # python model blocks while this file is present

    evaluation_lock_file_path = (
        tmp_path / "evaluation.lock"
    )  # python model creates this file if it's in the wait loop and deletes it once done

    # Note: to make execution block so we can test stuff, we use a Python model that blocks until it no longer detects the presence of a file
    (models_dir / "model_a.py").write_text(f"""
from sqlmesh.core.model import model
from sqlmesh.core.macros import MacroEvaluator

@model(
    "test.model_a",
    is_sql=True,
    kind="FULL"
)
def entrypoint(evaluator: MacroEvaluator) -> str:
    from pathlib import Path
    import time

    if evaluator.runtime_stage == 'evaluating':
        while True:
            if Path("{str(lock_file_path)}").exists():
                Path("{str(evaluation_lock_file_path)}").touch()
                print("lock exists; sleeping")
                time.sleep(2)
            else:
                Path("{str(evaluation_lock_file_path)}").unlink(missing_ok=True)
                break

    return "select 'model_a' as m"
""")

    config = Config(
        gateways={
            "": GatewayConfig(
                connection=DuckDBConnectionConfig(database=str(tmp_path / "db.db")),
                state_connection=DuckDBConnectionConfig(database=str(tmp_path / "state.db")),
            )
        },
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    )
    ctx = Context(paths=[tmp_path], config=config)

    # create prod
    ctx.plan(environment="prod", auto_apply=True)
    original_prod = ctx.state_sync.get_environment("prod")
    assert original_prod

    # update model_a for dev
    (models_dir / "model_a.py").unlink()
    (models_dir / "model_a.sql").write_text("""
    MODEL (
        name test.model_a,
        kind FULL
    );

    select 1 as changed
    """)

    # create dev
    ctx.load()
    plan = ctx.plan(environment="dev", auto_apply=True)
    assert len(plan.modified_snapshots) == 1
    new_model_a_snapshot_id = list(plan.modified_snapshots)[0]

    # now, trigger a prod restatement plan in a different thread and block it to simulate a long restatement
    thread_console = None

    def _run_restatement_plan(tmp_path: Path, config: Config, q: queue.Queue):
        nonlocal thread_console
        q.put("thread_started")

        # Give this thread its own markdown console to avoid Rich LiveError
        thread_console = MarkdownConsole()
        set_console(thread_console)

        # give this thread its own Context object to prevent segfaulting the Python interpreter
        restatement_ctx = Context(paths=[tmp_path], config=config)

        # ensure dev is present before the restatement plan starts
        assert restatement_ctx.state_sync.get_environment("dev") is not None

        q.put("plan_started")
        expected_error = None
        try:
            restatement_ctx.plan(
                environment="prod", restate_models=['"db"."test"."model_a"'], auto_apply=True
            )
        except ConflictingPlanError as e:
            expected_error = e

        q.put("plan_completed")
        return expected_error

    executor = ThreadPoolExecutor()
    q: queue.Queue = queue.Queue()
    lock_file_path.touch()

    restatement_plan_future = executor.submit(_run_restatement_plan, tmp_path, config, q)
    restatement_plan_future.add_done_callback(lambda _: executor.shutdown())

    assert q.get() == "thread_started"

    try:
        if e := restatement_plan_future.exception(timeout=1):
            # abort early if the plan thread threw an exception
            raise e
    except TimeoutError:
        # that's ok, we dont actually expect the plan to have finished in 1 second
        pass

    assert q.get() == "plan_started"

    # ok, now the prod restatement plan is running, let's deploy dev to prod
    ctx.plan(environment="prod", auto_apply=True)

    new_prod = ctx.state_sync.get_environment("prod")
    assert new_prod
    assert new_prod.plan_id != original_prod.plan_id
    assert new_prod.previous_plan_id == original_prod.plan_id

    # new prod is deployed but restatement plan is still running
    assert not restatement_plan_future.done()

    # allow restatement plan to complete
    lock_file_path.unlink()

    plan_error = restatement_plan_future.result()
    assert isinstance(plan_error, ConflictingPlanError)
    assert "please re-apply your plan" in repr(plan_error).lower()

    output = " ".join(re.split("\\s+", thread_console.captured_output, flags=re.UNICODE))  # type: ignore
    assert (
        f"The following models had new versions deployed while data was being restated: └── test.model_a"
        in output
    )

    # check that no intervals have been cleared from the model_a currently in prod
    model_a = ctx.state_sync.get_snapshots(snapshot_ids=[new_model_a_snapshot_id])[
        new_model_a_snapshot_id
    ]
    assert isinstance(model_a.node, SqlModel)
    assert model_a.node.render_query_or_raise().sql() == 'SELECT 1 AS "changed"'
    assert len(model_a.intervals)

    set_console(orig_console)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_restatement_plan_outside_parent_date_range(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    context.upsert_model("sushi.items", start="2023-01-06")
    context.upsert_model("sushi.orders", start="2023-01-06")
    # One of the parents should derive the start from its own parents for the issue
    # to reproduce
    context.upsert_model("sushi.order_items", start=None)
    context.upsert_model("sushi.waiter_revenue_by_day", start="2023-01-01", audits=[])

    context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)

    restated_snapshot = context.get_snapshot("sushi.waiter_revenue_by_day")
    downstream_snapshot = context.get_snapshot("sushi.top_waiters")

    plan = context.plan_builder(
        restate_models=["sushi.waiter_revenue_by_day"],
        start="2023-01-01",
        end="2023-01-01",
        min_intervals=0,
    ).build()
    assert plan.snapshots != context.snapshots

    assert plan.requires_backfill
    assert plan.restatements == {
        restated_snapshot.snapshot_id: (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
        downstream_snapshot.snapshot_id: (to_timestamp("2023-01-01"), to_timestamp("2023-01-09")),
    }
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=downstream_snapshot.snapshot_id,
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
            snapshot_id=restated_snapshot.snapshot_id,
            intervals=[
                (to_timestamp("2023-01-01"), to_timestamp("2023-01-02")),
            ],
        ),
    ]

    context.apply(plan)
