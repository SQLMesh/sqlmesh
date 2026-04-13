import logging
import pathlib
import typing as t
import re
from datetime import date, timedelta, datetime
from tempfile import TemporaryDirectory
from unittest.mock import PropertyMock, call, patch

import time_machine
import pytest
import pandas as pd  # noqa: TID253
from pathlib import Path
from pytest_mock.plugin import MockerFixture
from sqlglot import ParseError, exp, parse_one, Dialect
from sqlglot.errors import SchemaError

import sqlmesh.core.constants
from sqlmesh.cli.project_init import init_example_project
from sqlmesh.core.console import TerminalConsole
from sqlmesh.core import dialect as d, constants as c
from sqlmesh.core.config import (
    load_configs,
    AutoCategorizationMode,
    CategorizerConfig,
    Config,
    DuckDBConnectionConfig,
    EnvironmentSuffixTarget,
    GatewayConfig,
    LinterConfig,
    ModelDefaultsConfig,
    PlanConfig,
    SnowflakeConnectionConfig,
)
from sqlmesh.core.context import Context
from sqlmesh.core.console import create_console, get_console
from sqlmesh.core.dialect import parse, schema_
from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter
from sqlmesh.core.environment import Environment, EnvironmentNamingInfo, EnvironmentStatements
from sqlmesh.core.plan.definition import Plan
from sqlmesh.core.macros import MacroEvaluator, RuntimeStage
from sqlmesh.core.model import load_sql_based_model, model, SqlModel, Model
from sqlmesh.core.model.common import ParsableSql
from sqlmesh.core.model.cache import OptimizedQueryCache
from sqlmesh.core.renderer import render_statements
from sqlmesh.core.model.kind import ModelKindName
from sqlmesh.core.state_sync.cache import CachingStateSync
from sqlmesh.core.state_sync.db import EngineAdapterStateSync
from sqlmesh.utils.connection_pool import SingletonConnectionPool, ThreadLocalSharedConnectionPool
from sqlmesh.utils.date import (
    make_inclusive_end,
    now,
    to_date,
    to_datetime,
    to_timestamp,
    yesterday_ds,
)
from sqlmesh.utils.errors import (
    ConfigError,
    SQLMeshError,
    LinterError,
    PlanError,
    NoChangesPlanError,
)
from sqlmesh.utils.metaprogramming import Executable
from sqlmesh.utils.windows import IS_WINDOWS, fix_windows_path
from tests.utils.test_helpers import use_terminal_console
from tests.utils.test_filesystem import create_temp_file


def test_global_config(copy_to_temp_path: t.Callable):
    context = Context(paths=copy_to_temp_path("examples/sushi"))
    assert context.config.dialect == "duckdb"
    assert context.config.time_column_format == "%Y-%m-%d"


def test_named_config(copy_to_temp_path: t.Callable):
    context = Context(paths=copy_to_temp_path("examples/sushi"), config="test_config")
    assert len(context.config.gateways) == 1


def test_invalid_named_config(copy_to_temp_path: t.Callable):
    with pytest.raises(
        ConfigError,
        match="Config 'blah' was not found.",
    ):
        Context(paths=copy_to_temp_path("examples/sushi"), config="blah")


def test_missing_named_config(copy_to_temp_path: t.Callable):
    with pytest.raises(ConfigError, match=r"Config 'imaginary_config' was not found."):
        Context(paths=copy_to_temp_path("examples/sushi"), config="imaginary_config")


def test_config_parameter(copy_to_temp_path: t.Callable):
    config = Config(model_defaults=ModelDefaultsConfig(dialect="presto"), project="test_project")
    context = Context(paths=copy_to_temp_path("examples/sushi"), config=config)
    assert context.config.dialect == "presto"
    assert context.config.project == "test_project"


def test_generate_table_name_in_dialect(mocker: MockerFixture):
    context = Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="bigquery")))
    mocker.patch(
        "sqlmesh.core.context.GenericContext._model_tables",
        PropertyMock(return_value={'"project-id"."dataset"."table"': '"project-id".dataset.table'}),
    )
    assert (
        context.resolve_table('"project-id"."dataset"."table"') == "`project-id`.`dataset`.`table`"
    )


def test_config_not_found(copy_to_temp_path: t.Callable):
    with pytest.raises(
        ConfigError,
        match=r".*config could not be found.*",
    ):
        Context(paths="nonexistent/directory", config="config")


def test_custom_macros(sushi_context):
    assert "add_one" in sushi_context._macros


def test_dag(sushi_context):
    assert set(sushi_context.dag.upstream('"memory"."sushi"."customer_revenue_by_day"')) == {
        '"memory"."sushi"."items"',
        '"memory"."sushi"."orders"',
        '"memory"."sushi"."order_items"',
    }


@pytest.mark.slow
def test_render_sql_model(sushi_context, assert_exp_eq, copy_to_temp_path: t.Callable):
    assert_exp_eq(
        sushi_context.render(
            "sushi.waiter_revenue_by_day",
            start=date(2021, 1, 1),
            end=date(2021, 1, 1),
            expand=True,
        ),
        """
        SELECT
          CAST("o"."waiter_id" AS INT) AS "waiter_id", /* Waiter id */
          CAST(SUM("oi"."quantity" * "i"."price") AS DOUBLE) AS "revenue", /* Revenue from orders taken by this waiter */
          CAST("o"."event_date" AS DATE) AS "event_date" /* Date */
        FROM (
          SELECT
            CAST(NULL AS INT) AS "id",
            CAST(NULL AS INT) AS "customer_id",
            CAST(NULL AS INT) AS "waiter_id",
            CAST(NULL AS INT) AS "start_ts",
            CAST(NULL AS INT) AS "end_ts",
            CAST(NULL AS DATE) AS "event_date"
          FROM (VALUES
            (1)) AS "t"("dummy")
        ) AS "o"
        LEFT JOIN (
          SELECT
            CAST(NULL AS INT) AS "id",
            CAST(NULL AS INT) AS "order_id",
            CAST(NULL AS INT) AS "item_id",
            CAST(NULL AS INT) AS "quantity",
            CAST(NULL AS DATE) AS "event_date"
          FROM (VALUES
            (1)) AS "t"("dummy")
        ) AS "oi"
          ON "o"."event_date" = "oi"."event_date" AND "o"."id" = "oi"."order_id"
        LEFT JOIN (
          SELECT
            CAST(NULL AS INT) AS "id",
            CAST(NULL AS TEXT) AS "name",
            CAST(NULL AS DOUBLE) AS "price",
            CAST(NULL AS DATE) AS "event_date"
          FROM (VALUES
            (1)) AS "t"("dummy")
        ) AS "i"
          ON "i"."event_date" = "oi"."event_date" AND "i"."id" = "oi"."item_id"
        WHERE
          "o"."event_date" <= CAST('2021-01-01' AS DATE)
          AND "o"."event_date" >= CAST('2021-01-01' AS DATE)
        GROUP BY
          "o"."waiter_id",
          "o"."event_date"
        """,
    )

    # unpushed render should not expand
    unpushed = Context(paths=copy_to_temp_path("examples/sushi"))
    assert_exp_eq(
        unpushed.render("sushi.waiter_revenue_by_day"),
        """
        SELECT
          CAST("o"."waiter_id" AS INT) AS "waiter_id", /* Waiter id */
          CAST(SUM("oi"."quantity" * "i"."price") AS DOUBLE) AS "revenue", /* Revenue from orders taken by this waiter */
          CAST("o"."event_date" AS DATE) AS "event_date" /* Date */
        FROM "memory"."sushi"."orders" AS "o"
        LEFT JOIN "memory"."sushi"."order_items" AS "oi"
          ON "o"."event_date" = "oi"."event_date" AND "o"."id" = "oi"."order_id"
        LEFT JOIN "memory"."sushi"."items" AS "i"
          ON "i"."event_date" = "oi"."event_date" AND "i"."id" = "oi"."item_id"
        WHERE
          "o"."event_date" <= CAST('1970-01-01' AS DATE) AND "o"."event_date" >= CAST('1970-01-01' AS DATE)
        GROUP BY
          "o"."waiter_id",
          "o"."event_date"
        """,
    )


@pytest.mark.slow
def test_render_non_deployable_parent(sushi_context, assert_exp_eq, copy_to_temp_path: t.Callable):
    model = sushi_context.get_model("sushi.waiter_revenue_by_day")
    forward_only_kind = model.kind.copy(update={"forward_only": True})
    model = model.copy(update={"kind": forward_only_kind, "stamp": "trigger forward-only change"})
    sushi_context.upsert_model(model)
    sushi_context.plan("dev", no_prompts=True, auto_apply=True)

    expected_table_name = parse_one(
        sushi_context.get_snapshot("sushi.waiter_revenue_by_day").table_name(is_deployable=False),
        into=exp.Table,
    ).this.this

    assert_exp_eq(
        sushi_context.render(
            "sushi.top_waiters",
            start=date(2021, 1, 1),
            end=date(2021, 1, 1),
        ),
        f"""
        WITH "test_macros" AS (
          SELECT
            2 AS "lit_two",
            "waiter_revenue_by_day"."revenue" * 2.0 AS "sql_exp",
            CAST("waiter_revenue_by_day"."revenue" AS TEXT) AS "sql_lit"
          FROM "memory"."sqlmesh__sushi"."{expected_table_name}" AS "waiter_revenue_by_day" /* memory.sushi.waiter_revenue_by_day */
        )
        SELECT
          CAST("waiter_revenue_by_day"."waiter_id" AS INT) AS "waiter_id",
          CAST("waiter_revenue_by_day"."revenue" AS DOUBLE) AS "revenue"
        FROM "memory"."sqlmesh__sushi"."{expected_table_name}" AS "waiter_revenue_by_day" /* memory.sushi.waiter_revenue_by_day */
        WHERE
          "waiter_revenue_by_day"."event_date" = (
            SELECT
              MAX("waiter_revenue_by_day"."event_date") AS "_col_0"
            FROM "memory"."sqlmesh__sushi"."{expected_table_name}" AS "waiter_revenue_by_day" /* memory.sushi.waiter_revenue_by_day */
          )
        ORDER BY
          "revenue" DESC
        LIMIT 10
        """,
    )


@pytest.mark.slow
def test_render_seed_model(sushi_context, assert_exp_eq):
    assert_exp_eq(
        sushi_context.render(
            "sushi.waiter_names",
            start=date(2021, 1, 1),
            end=date(2021, 1, 1),
        ),
        """
        SELECT
          CAST(id AS BIGINT) AS id,
          CAST(name AS TEXT) AS name
        FROM (VALUES
          (0, 'Toby'),
          (1, 'Tyson'),
          (2, 'Ryan'),
          (3, 'George'),
          (4, 'Chris')) AS t(id, name)
        """,
    )


@pytest.mark.slow
def test_diff(sushi_context: Context, mocker: MockerFixture):
    mock_console = mocker.Mock()
    sushi_context.console = mock_console
    yesterday = yesterday_ds()
    success = sushi_context.run(start=yesterday, end=yesterday)

    sushi_context.upsert_model("sushi.customers", query=parse_one("select 1 as customer_id"))
    sushi_context.diff("test")
    assert mock_console.show_environment_difference_summary.called
    assert mock_console.show_model_difference_summary.called
    assert success


@pytest.mark.slow
def test_evaluate_limit():
    context = Context(config=Config())

    context.upsert_model(
        load_sql_based_model(
            parse(
                """
        MODEL(name with_limit, kind FULL);
        SELECT t.v as v FROM (VALUES (1), (2), (3), (4), (5)) AS t(v) LIMIT 1 + 2"""
            )
        )
    )

    assert context.evaluate("with_limit", "2020-01-01", "2020-01-02", "2020-01-02").size == 3
    assert context.evaluate("with_limit", "2020-01-01", "2020-01-02", "2020-01-02", 4).size == 3
    assert context.evaluate("with_limit", "2020-01-01", "2020-01-02", "2020-01-02", 2).size == 2

    context.upsert_model(
        load_sql_based_model(
            parse(
                """
        MODEL(name without_limit, kind FULL);
        SELECT t.v as v FROM (VALUES (1), (2), (3), (4), (5)) AS t(v)"""
            )
        )
    )

    assert context.evaluate("without_limit", "2020-01-01", "2020-01-02", "2020-01-02").size == 5
    assert context.evaluate("without_limit", "2020-01-01", "2020-01-02", "2020-01-02", 4).size == 4
    assert context.evaluate("without_limit", "2020-01-01", "2020-01-02", "2020-01-02", 2).size == 2


def test_gateway_specific_adapters(copy_to_temp_path, mocker):
    path = copy_to_temp_path("examples/sushi")
    ctx = Context(paths=path, config="isolated_systems_config", gateway="prod")
    assert len(ctx.engine_adapters) == 3
    assert ctx.engine_adapter == ctx.engine_adapters["prod"]
    assert ctx._get_engine_adapter("dev") == ctx.engine_adapters["dev"]

    ctx = Context(paths=path, config="isolated_systems_config")
    assert len(ctx.engine_adapters) == 3
    assert ctx.engine_adapter == ctx.engine_adapters["dev"]

    ctx = Context(paths=path, config="isolated_systems_config")
    assert len(ctx.engine_adapters) == 3
    assert ctx.engine_adapter == ctx._get_engine_adapter()
    assert ctx._get_engine_adapter("test") == ctx.engine_adapters["test"]


def test_multiple_gateways(tmp_path: Path):
    db_path = str(tmp_path / "db.db")
    gateways = {
        "staging": GatewayConfig(connection=DuckDBConnectionConfig(database=db_path)),
        "final": GatewayConfig(connection=DuckDBConnectionConfig(database=db_path)),
    }

    config = Config(gateways=gateways, default_gateway="final")
    context = Context(config=config)

    gateway_model = load_sql_based_model(
        parse(
            """
    MODEL(name staging.stg_model, start '2024-01-01',kind FULL, gateway staging);
    SELECT t.v as v FROM (VALUES (1), (2), (3), (4), (5)) AS t(v)"""
        ),
        default_catalog="db",
    )

    assert gateway_model.gateway == "staging"
    context.upsert_model(gateway_model)
    assert context.evaluate("staging.stg_model", "2020-01-01", "2020-01-02", "2020-01-02").size == 5

    default_model = load_sql_based_model(
        parse(
            """
    MODEL(name main.final_model, start '2024-01-01',kind FULL);
    SELECT v FROM staging.stg_model"""
        ),
        default_catalog="db",
    )

    assert not default_model.gateway
    context.upsert_model(default_model)

    context.plan(
        execution_time="2024-01-02",
        auto_apply=True,
        no_prompts=True,
    )

    sorted_snapshots = sorted(context.snapshots.values())

    physical_schemas = [snapshot.physical_schema for snapshot in sorted_snapshots]
    assert physical_schemas == ["sqlmesh__main", "sqlmesh__staging"]

    view_schemas = [snapshot.qualified_view_name.schema_name for snapshot in sorted_snapshots]
    assert view_schemas == ["main", "staging"]

    assert (
        str(context.fetchdf("select * from staging.stg_model"))
        == "   v\n0  1\n1  2\n2  3\n3  4\n4  5"
    )
    assert str(context.fetchdf("select * from final_model")) == "   v\n0  1\n1  2\n2  3\n3  4\n4  5"

    assert (
        context.snapshots['"db"."main"."final_model"'].parents[0].name
        == '"db"."staging"."stg_model"'
    )
    assert context.dag._sorted == ['"db"."staging"."stg_model"', '"db"."main"."final_model"']


def test_plan_execution_time():
    context = Context(config=Config())
    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.x,
                    start '2024-01-01',
                    kind FULL
                );

                SELECT @execution_date AS execution_date
                """
            )
        )
    )

    context.plan(
        "dev",
        execution_time="2024-01-02",
        auto_apply=True,
        no_prompts=True,
    )
    assert (
        str(list(context.fetchdf("select * from db__dev.x")["execution_date"])[0])
        == "2024-01-02 00:00:00"
    )


def test_plan_execution_time_start_end():
    context = Context(config=Config())
    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.x,
                    start '2020-01-01',
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column ds
                    ),
                    cron '@daily'
                );

                SELECT id, ds FROM (VALUES
                    ('1', '2020-01-01'),
                    ('2', '2021-01-01'),
                    ('3', '2022-01-01'),
                    ('4', '2023-01-01'),
                    ('5', '2024-01-01')
                ) data(id, ds)
                WHERE ds BETWEEN @start_ds AND @end_ds
                """
            )
        )
    )

    # prod plan - no fixed execution time so it defaults to now() and reads all the data
    prod_plan = context.plan(auto_apply=True)

    assert len(prod_plan.new_snapshots) == 1

    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.x,
                    start '2020-01-01',
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column ds
                    ),
                    cron '@daily'
                );

                SELECT id, ds, 'changed' as a FROM (VALUES
                    ('1', '2020-01-01'),
                    ('2', '2021-01-01'),
                    ('3', '2022-01-01'),
                    ('4', '2023-01-01'),
                    ('5', '2024-01-01')
                ) data(id, ds)
                WHERE ds BETWEEN @start_ds AND @end_ds
                """
            )
        )
    )

    # dev plan with an execution time in the past and no explicit start/end specified
    # the plan end should be bounded to it and not exceed it even though in prod the last interval (used as a default end)
    # is newer than the execution time
    dev_plan = context.plan("dev", execution_time="2020-01-05")

    assert to_datetime(dev_plan.start) == to_datetime(
        "2020-01-01"
    )  # default start is the earliest prod interval
    assert to_datetime(dev_plan.execution_time) == to_datetime("2020-01-05")
    assert to_datetime(dev_plan.end) == to_datetime(
        "2020-01-05"
    )  # end should not be greater than execution_time

    # same as above but with a relative start
    dev_plan = context.plan("dev", start="1 day ago", execution_time="2020-01-05")

    assert to_datetime(dev_plan.start) == to_datetime(
        "2020-01-04"
    )  # start relative to execution_time
    assert to_datetime(dev_plan.execution_time) == to_datetime("2020-01-05")
    assert to_datetime(dev_plan.end) == to_datetime(
        "2020-01-05"
    )  # end should not be greater than execution_time

    # same as above but with a relative start and a relative end
    dev_plan = context.plan("dev", start="2 days ago", execution_time="2020-01-05", end="1 day ago")

    assert to_datetime(dev_plan.start) == to_datetime(
        "2020-01-03"
    )  # start relative to execution_time
    assert to_datetime(dev_plan.execution_time) == to_datetime("2020-01-05")
    assert to_datetime(dev_plan.end) == to_datetime("2020-01-04")  # end relative to execution_time


def test_override_builtin_audit_blocking_mode():
    context = Context(config=Config())
    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.x,
                    kind FULL,
                    audits (
                        not_null(columns := [c], blocking := false),
                        unique_values(columns := [c]),
                    )
                );

                SELECT NULL AS c
                """
            )
        )
    )

    with patch.object(context.console, "log_warning") as mock_logger:
        plan = context.plan(auto_apply=True, no_prompts=True)
        new_snapshot = next(iter(plan.context_diff.new_snapshots.values()))

        assert (
            mock_logger.call_args_list[0][0][0] == "\ndb.x: 'not_null' audit error: 1 row failed."
        )

    # Even though there are two builtin audits referenced in the above definition, we only
    # store the one that overrides `blocking` in the snapshot; the other one isn't needed
    audits_with_args = new_snapshot.model.audits_with_args
    assert len(audits_with_args) == 2
    audit, args = audits_with_args[0]
    assert audit.name == "not_null"
    assert list(args) == ["columns", "blocking"]

    context = Context(config=Config())
    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.x,
                    kind FULL,
                    audits (
                        not_null_non_blocking(columns := [c], blocking := true)
                    )
                );

                SELECT NULL AS c
                """
            )
        )
    )

    with pytest.raises(SQLMeshError):
        context.plan(auto_apply=True, no_prompts=True)


def test_python_model_empty_df_raises(sushi_context, capsys):
    sushi_context.console = create_console()

    @model(
        "memory.sushi.test_model",
        columns={"col": "int"},
        kind=dict(name=ModelKindName.FULL),
    )
    def entrypoint(context, **kwargs):
        yield pd.DataFrame({"col": []})

    test_model = model.get_registry()["memory.sushi.test_model"].model(
        module_path=Path("."), path=Path(".")
    )
    sushi_context.upsert_model(test_model)

    capsys.readouterr()
    with pytest.raises(SQLMeshError):
        sushi_context.plan(no_prompts=True, auto_apply=True)

    assert (
        "Cannot construct source query from an empty DataFrame. This error is commonly related to Python models that produce no data. For such models, consider yielding from an empty generator if the resulting set is empty, i.e. use"
    ) in capsys.readouterr().out.replace("\n", "")


def test_env_and_default_schema_normalization(mocker: MockerFixture):
    from sqlglot.dialects import DuckDB
    from sqlglot.dialects.dialect import NormalizationStrategy

    mocker.patch.object(DuckDB, "NORMALIZATION_STRATEGY", NormalizationStrategy.UPPERCASE)

    context = Context(config=Config())
    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name x,
                    kind FULL
                );

                SELECT 1 AS c
                """
            )
        )
    )
    context.plan("dev", auto_apply=True, no_prompts=True)
    assert list(context.fetchdf('select c from "DEFAULT__DEV"."X"')["c"])[0] == 1


def test_jinja_macro_undefined_variable_error(tmp_path: pathlib.Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    macros_dir = tmp_path / "macros"
    macros_dir.mkdir(parents=True)

    macro_file = macros_dir / "my_macros.sql"
    macro_file.write_text("""
{%- macro generate_select(table_name) -%}
  {%- if target.name == 'production' -%}
    {%- set results = run_query('SELECT 1') -%}
  {%- endif -%}
  SELECT {{ results.columns[0].values()[0] }} FROM {{ table_name }}
{%- endmacro -%}
""")

    model_file = models_dir / "my_model.sql"
    model_file.write_text("""
MODEL (
    name my_schema.my_model,
    kind FULL
);

JINJA_QUERY_BEGIN;
{{ generate_select('users') }}
JINJA_END;
""")

    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
model_defaults:
  dialect: duckdb
""")

    with pytest.raises(ConfigError) as exc_info:
        Context(paths=str(tmp_path))

    error_message = str(exc_info.value)
    assert "Failed to load model" in error_message
    assert "Could not render jinja for" in error_message
    assert "Undefined macro/variable: 'target' in macro: 'generate_select'" in error_message


def test_clear_caches(tmp_path: pathlib.Path):
    models_dir = tmp_path / "models"

    cache_dir = models_dir / sqlmesh.core.constants.CACHE
    cache_dir.mkdir(parents=True)
    model_cache_file = cache_dir / "test.txt"
    model_cache_file.write_text("test")

    assert model_cache_file.exists()
    assert len(list(cache_dir.iterdir())) == 1

    context = Context(config=Config(), paths=str(models_dir))
    context.clear_caches()

    assert not cache_dir.exists()
    assert models_dir.exists()

    # Ensure that we don't initialize a CachingStateSync only to clear its (empty) caches
    assert context._state_sync is None

    # Test clearing caches when cache directory doesn't exist
    # This should not raise an exception
    context.clear_caches()
    assert not cache_dir.exists()


def test_clear_caches_with_long_base_path(tmp_path: pathlib.Path):
    base_path = tmp_path / ("abcde" * 50)
    assert (
        len(str(base_path.absolute())) > 260
    )  # Paths longer than 260 chars trigger problems on Windows

    default_cache_dir = base_path / c.CACHE
    custom_cache_dir = base_path / ".test_cache"

    # note: we create the Context here so it doesnt get passed any "fixed" paths
    ctx = Context(config=Config(cache_dir=str(custom_cache_dir)), paths=base_path)

    if IS_WINDOWS:
        # fix these so we can use them in this test
        default_cache_dir = fix_windows_path(default_cache_dir)
        custom_cache_dir = fix_windows_path(custom_cache_dir)

    default_cache_dir.mkdir(parents=True)
    custom_cache_dir.mkdir(parents=True)

    default_cache_file = default_cache_dir / "cache.txt"
    custom_cache_file = custom_cache_dir / "cache.txt"

    default_cache_file.write_text("test")
    custom_cache_file.write_text("test")

    assert default_cache_file.exists()
    assert custom_cache_file.exists()
    assert default_cache_dir.exists()
    assert custom_cache_dir.exists()

    ctx.clear_caches()

    assert not default_cache_file.exists()
    assert not custom_cache_file.exists()
    assert not default_cache_dir.exists()
    assert not custom_cache_dir.exists()


def test_cache_path_configurations(tmp_path: pathlib.Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    config_file = project_dir / "config.yaml"

    # Test relative path
    config_file.write_text("model_defaults:\n  dialect: duckdb\ncache_dir: .my_cache")
    context = Context(paths=str(project_dir))
    assert context.cache_dir == project_dir / ".my_cache"

    # Test absolute path
    abs_cache = tmp_path / "abs_cache"
    config_file.write_text(f"model_defaults:\n  dialect: duckdb\ncache_dir: {abs_cache}")
    context = Context(paths=str(project_dir))
    assert context.cache_dir == abs_cache

    # Test default
    config_file.write_text("model_defaults:\n  dialect: duckdb")
    context = Context(paths=str(project_dir))
    assert context.cache_dir == project_dir / ".cache"


def test_plan_apply_populates_cache(copy_to_temp_path, mocker):
    sushi_paths = copy_to_temp_path("examples/sushi")
    sushi_path = sushi_paths[0]
    custom_cache_dir = sushi_path.parent / "custom_cache"

    # Modify the existing config.py to add cache_dir to test_config
    config_py_path = sushi_path / "config.py"
    with open(config_py_path, "r") as f:
        config_content = f.read()

    # Add cache_dir to the test_config definition
    config_content += f"""test_config_cache_dir = Config(
    gateways={{"in_memory": GatewayConfig(connection=DuckDBConnectionConfig())}},
    default_gateway="in_memory",
    plan=PlanConfig(
        auto_categorize_changes=CategorizerConfig(
            sql=AutoCategorizationMode.SEMI, python=AutoCategorizationMode.OFF
        )
    ),
    model_defaults=model_defaults,
    cache_dir="{custom_cache_dir.as_posix()}",
    before_all=before_all,
)"""

    with open(config_py_path, "w") as f:
        f.write(config_content)

    # Create context with the test config
    context = Context(paths=sushi_path, config="test_config_cache_dir")
    custom_cache_dir = context.cache_dir
    assert "custom_cache" in str(custom_cache_dir)
    assert (custom_cache_dir / "optimized_query").exists()
    assert (custom_cache_dir / "model_definition").exists()
    assert not (custom_cache_dir / "snapshot").exists()

    # Clear the cache
    context.clear_caches()
    assert not custom_cache_dir.exists()

    plan = context.plan("dev", create_from="prod", skip_tests=True)
    context.apply(plan)

    # Cache directory should now exist again
    assert custom_cache_dir.exists()
    assert any(custom_cache_dir.iterdir())

    # Since the cache has been deleted post loading here only snapshot should exist
    assert (custom_cache_dir / "snapshot").exists()
    assert not (custom_cache_dir / "optimized_query").exists()
    assert not (custom_cache_dir / "model_definition").exists()

    # New context should load same models and create the cache for optimized_query and model_definition
    initial_model_count = len(context.models)
    context2 = Context(paths=context.path, config="test_config_cache_dir")
    cached_model_count = len(context2.models)

    assert initial_model_count == cached_model_count > 0
    assert (custom_cache_dir / "optimized_query").exists()
    assert (custom_cache_dir / "model_definition").exists()
    assert (custom_cache_dir / "snapshot").exists()

    # Clear caches should remove the custom cache directory
    context.clear_caches()
    assert not custom_cache_dir.exists()


def test_ignore_files(mocker: MockerFixture, tmp_path: pathlib.Path):
    mocker.patch.object(
        sqlmesh.core.constants,
        "IGNORE_PATTERNS",
        [".ipynb_checkpoints/*.sql"],
    )

    models_dir = pathlib.Path("models")
    macros_dir = pathlib.Path("macros")

    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "ignore", "ignore_model.sql"),
        "MODEL(name ignore.ignore_model); SELECT 1 AS cola",
    )
    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "ignore", "inner_ignore", "inner_ignore_model.sql"),
        "MODEL(name ignore.inner_ignore_model); SELECT 1 AS cola",
    )
    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "macro_ignore.py"),
        """
from sqlmesh.core.macros import macro

@macro()
def test():
    return "test"
""",
    )
    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, ".ipynb_checkpoints", "ignore_model2.sql"),
        "MODEL(name ignore_model2); SELECT cola::bigint AS cola FROM db.other_table",
    )
    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "db", "actual_test.sql"),
        "MODEL(name db.actual_test, kind full); SELECT 1 AS cola",
    )
    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "macro_file.py"),
        """
from sqlmesh.core.macros import macro

@macro()
def test():
    return "test"
""",
    )
    config = Config(
        ignore_patterns=["models/ignore/**/*.sql", "macro_ignore.py", ".ipynb_checkpoints/*"]
    )
    context = Context(paths=tmp_path, config=config)

    assert ['"memory"."db"."actual_test"'] == list(context.models)
    assert "test" in context._macros


@pytest.mark.slow
def test_plan_apply(sushi_context_pre_scheduling) -> None:
    logger = logging.getLogger("sqlmesh.core.renderer")
    with patch.object(logger, "warning") as mock_logger:
        sushi_context_pre_scheduling.plan(
            "dev",
            auto_apply=True,
            no_prompts=True,
            include_unmodified=True,
        )
        mock_logger.assert_not_called()
    assert sushi_context_pre_scheduling.state_reader.get_environment("dev")


def test_project_config_person_config_overrides(tmp_path: pathlib.Path):
    with TemporaryDirectory() as td:
        home_path = pathlib.Path(td)
        create_temp_file(
            home_path,
            pathlib.Path("config.yaml"),
            """
gateways:
    snowflake:
        connection:
            type: snowflake
            password: XYZ
            user: ABC
""",
        )
        project_config = create_temp_file(
            tmp_path,
            pathlib.Path("config.yaml"),
            """
gateways:
    snowflake:
        connection:
            type: snowflake
            account: abc123
            user: CDE

model_defaults:
    dialect: snowflake
""",
        )
        with pytest.raises(
            ConfigError,
            match="User and password must be provided if using default authentication",
        ):
            load_configs("config", Config, paths=project_config.parent)
        loaded_configs: t.Dict[pathlib.Path, Config] = load_configs(
            "config", Config, paths=project_config.parent, sqlmesh_path=home_path
        )
        assert len(loaded_configs) == 1
        snowflake_connection = list(loaded_configs.values())[0].gateways["snowflake"].connection  # type: ignore
        assert isinstance(snowflake_connection, SnowflakeConnectionConfig)
        assert snowflake_connection.account == "abc123"
        assert snowflake_connection.user == "ABC"
        assert snowflake_connection.password == "XYZ"
        assert snowflake_connection.application == "Tobiko_SQLMesh"


@pytest.mark.slow
def test_physical_schema_override(copy_to_temp_path: t.Callable) -> None:
    def get_schemas(context: Context):
        return {
            snapshot.physical_schema for snapshot in context.snapshots.values() if snapshot.is_model
        }

    def get_view_schemas(context: Context):
        return {
            snapshot.qualified_view_name.schema_name
            for snapshot in context.snapshots.values()
            if snapshot.is_model
        }

    def get_sushi_fingerprints(context: Context):
        return {
            snapshot.fingerprint.to_identifier()
            for snapshot in context.snapshots.values()
            if snapshot.is_model and snapshot.model.schema_name == "sushi"
        }

    project_path = copy_to_temp_path("examples/sushi")
    no_mapping_context = Context(paths=project_path)
    assert no_mapping_context.config.physical_schema_mapping == {}
    assert get_schemas(no_mapping_context) == {"sqlmesh__sushi", "sqlmesh__raw"}
    assert get_view_schemas(no_mapping_context) == {"sushi", "raw"}
    no_mapping_fingerprints = get_sushi_fingerprints(no_mapping_context)
    context = Context(paths=project_path, config="map_config")
    assert context.config.physical_schema_mapping == {re.compile("^sushi$"): "company_internal"}
    assert get_schemas(context) == {"company_internal", "sqlmesh__raw"}
    assert get_view_schemas(context) == {"sushi", "raw"}
    sushi_fingerprints = get_sushi_fingerprints(context)
    assert (
        len(sushi_fingerprints)
        == len(no_mapping_fingerprints)
        == len(no_mapping_fingerprints - sushi_fingerprints)
    )


@pytest.mark.slow
def test_physical_schema_mapping(tmp_path: pathlib.Path) -> None:
    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "a.sql"),
        "MODEL(name foo_staging.model_a); SELECT 1;",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "b.sql"),
        "MODEL(name testone.model_b); SELECT 1;",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "c.sql"),
        "MODEL(name untouched.model_c); SELECT 1;",
    )

    ctx = Context(
        config=Config(
            model_defaults=ModelDefaultsConfig(dialect="duckdb"),
            physical_schema_mapping={
                # anything ending with 'staging' becomes 'overridden_staging'
                "^.*staging$": "overridden_staging",
                # anything starting with 'test' becomes 'testing'
                "^test.*": "testing",
            },
        ),
        paths=tmp_path,
    )

    ctx.load()

    physical_schemas = [snapshot.physical_schema for snapshot in sorted(ctx.snapshots.values())]

    view_schemas = [
        snapshot.qualified_view_name.schema_name for snapshot in sorted(ctx.snapshots.values())
    ]

    assert len(physical_schemas) == len(view_schemas) == 3
    assert physical_schemas == ["overridden_staging", "testing", "sqlmesh__untouched"]
    assert view_schemas == ["foo_staging", "testone", "untouched"]


@pytest.mark.slow
def test_janitor(sushi_context, mocker: MockerFixture) -> None:
    adapter_mock = mocker.MagicMock()
    adapter_mock.dialect = "duckdb"
    state_sync_mock = mocker.MagicMock()

    environments = [
        Environment(
            name="test_environment1",
            suffix_target=EnvironmentSuffixTarget.TABLE,
            snapshots=[x.table_info for x in sushi_context.snapshots.values()],
            start_at="2022-01-01",
            end_at="2022-01-01",
            plan_id="test_plan_id",
            previous_plan_id="test_plan_id",
        ),
        Environment(
            name="test_environment2",
            suffix_target=EnvironmentSuffixTarget.SCHEMA,
            snapshots=[x.table_info for x in sushi_context.snapshots.values()],
            start_at="2022-01-01",
            end_at="2022-01-01",
            plan_id="test_plan_id",
            previous_plan_id="test_plan_id",
        ),
    ]

    state_sync_mock.get_expired_environments.return_value = [env.summary for env in environments]
    state_sync_mock.get_environment = lambda name: next(
        env for env in environments if env.name == name
    )

    sushi_context._engine_adapter = adapter_mock
    sushi_context.engine_adapters = {sushi_context.config.default_gateway: adapter_mock}
    sushi_context._state_sync = state_sync_mock
    state_sync_mock.get_expired_snapshots.return_value = None

    sushi_context._run_janitor()
    # Assert that the schemas are dropped just twice for the schema based environment
    # Make sure that external model schemas/tables are not dropped
    adapter_mock.drop_schema.assert_has_calls(
        [
            call(
                schema_("sushi__test_environment2", "memory"),
                cascade=True,
                ignore_if_not_exists=True,
            ),
        ]
    )
    # Assert that the views are dropped for each snapshot just once and make sure that the name used is the
    # view name with the environment as a suffix
    assert adapter_mock.drop_view.call_count == 16
    adapter_mock.drop_view.assert_has_calls(
        [
            call(
                "memory.sushi.waiter_as_customer_by_day__test_environment1",
                ignore_if_not_exists=True,
            ),
        ]
    )


@pytest.mark.slow
def test_plan_default_end(sushi_context_pre_scheduling: Context):
    prod_plan_builder = sushi_context_pre_scheduling.plan_builder("prod")
    # Simulate that the prod is 3 days behind.
    plan_end = to_date(now()) - timedelta(days=3)
    prod_plan_builder._end = plan_end
    prod_plan_builder.apply()

    dev_plan = sushi_context_pre_scheduling.plan(
        "test_env", no_prompts=True, include_unmodified=True, skip_backfill=True, auto_apply=True
    )
    assert dev_plan.end is not None
    assert to_date(make_inclusive_end(dev_plan.end)) == plan_end

    forward_only_dev_plan = sushi_context_pre_scheduling.plan_builder(
        "test_env_forward_only", include_unmodified=True, forward_only=True
    ).build()
    assert forward_only_dev_plan.end is not None
    assert to_date(make_inclusive_end(forward_only_dev_plan.end)) == plan_end
    assert to_timestamp(forward_only_dev_plan.start) == to_timestamp(plan_end)


@pytest.mark.slow
def test_plan_start_ahead_of_end(copy_to_temp_path):
    path = copy_to_temp_path("examples/sushi")
    with time_machine.travel("2024-01-02 00:00:00 UTC"):
        context = Context(paths=path, gateway="duckdb_persistent")
        context.plan("prod", no_prompts=True, auto_apply=True)
        assert all(
            i == to_timestamp("2024-01-02")
            for i in context.state_sync.max_interval_end_per_model("prod").values()
        )
        context.close()
    with time_machine.travel("2024-01-03 00:00:00 UTC"):
        context = Context(paths=path, gateway="duckdb_persistent")
        expression = d.parse(
            """
                MODEL(
            name sushi.hourly,
            KIND FULL,
            cron '@hourly',
            start '2024-01-02 12:00:00',
        );

        SELECT 1"""
        )
        model = load_sql_based_model(expression, default_catalog=context.default_catalog)
        context.upsert_model(model)
        context.plan("prod", no_prompts=True, auto_apply=True)
        # Since the new start is ahead of the latest end loaded for prod, the table is deployed as empty
        # This isn't considered a gap since prod has not loaded these intervals yet
        # As a results the max interval end is unchanged and the table is empty
        assert all(
            i == to_timestamp("2024-01-02")
            for i in context.state_sync.max_interval_end_per_model("prod").values()
        )
        assert context.engine_adapter.fetchone("SELECT COUNT(*) FROM sushi.hourly")[0] == 0
        context.close()


@pytest.mark.slow
def test_plan_seed_model_excluded_from_default_end(copy_to_temp_path: t.Callable):
    path = copy_to_temp_path("examples/sushi")
    with time_machine.travel("2024-06-01 00:00:00 UTC"):
        context = Context(paths=path, gateway="duckdb_persistent")
        context.plan("prod", no_prompts=True, auto_apply=True)
        max_ends = context.state_sync.max_interval_end_per_model("prod")
        seed_fqns = [k for k in max_ends if "waiter_names" in k]
        assert len(seed_fqns) == 1
        assert max_ends[seed_fqns[0]] == to_timestamp("2024-06-01")
        context.close()

    with time_machine.travel("2026-03-01 00:00:00 UTC"):
        context = Context(paths=path, gateway="duckdb_persistent")

        # a model that depends on this seed but has no interval in prod yet so only the seed would contribute to max_interval_end_per_model
        context.upsert_model(
            load_sql_based_model(
                parse(
                    """
                    MODEL(
                        name sushi.waiter_summary,
                        kind INCREMENTAL_BY_TIME_RANGE (
                            time_column ds
                        ),
                        start '2025-01-01',
                        cron '@daily'
                    );

                    SELECT
                        id,
                        name,
                        @start_ds AS ds
                    FROM
                        sushi.waiter_names
                    WHERE
                        @start_ds BETWEEN @start_ds AND @end_ds
                    """
                ),
                default_catalog=context.default_catalog,
            )
        )

        # the seed's interval end would still be 2024-06-01
        max_ends = context.state_sync.max_interval_end_per_model("prod")
        seed_fqns = [k for k in max_ends if "waiter_names" in k]
        assert len(seed_fqns) == 1
        assert max_ends[seed_fqns[0]] == to_timestamp("2024-06-01")

        # the plan start date 2025-01-01 is after the seeds end date but shouldnt cause the plan to fail
        plan = context.plan(
            "dev", start="2025-01-01", no_prompts=True, select_models=["*waiter_summary"]
        )

        # the end should fall back to execution_time rather than seeds end
        assert plan.models_to_backfill == {
            '"duckdb"."sushi"."waiter_names"',
            '"duckdb"."sushi"."waiter_summary"',
        }
        assert plan.provided_end is None
        assert plan.provided_start == "2025-01-01"
        assert to_timestamp(plan.end) == to_timestamp("2026-03-01")
        assert to_timestamp(plan.start) == to_timestamp("2025-01-01")
        context.close()


@pytest.mark.slow
def test_schema_error_no_default(sushi_context_pre_scheduling) -> None:
    context = sushi_context_pre_scheduling

    with pytest.raises(SchemaError):
        context.upsert_model(
            load_sql_based_model(
                parse(
                    """
            MODEL(name c);
            SELECT x FROM a
            """
                )
            )
        )


@pytest.mark.slow
def test_unrestorable_snapshot(sushi_context: Context) -> None:
    model_v1 = load_sql_based_model(
        parse(
            """
        MODEL(name sushi.test_unrestorable);
        SELECT 1 AS one;
        """
        ),
        default_catalog=sushi_context.default_catalog,
        dialect=sushi_context.default_dialect,
    )
    model_v2 = load_sql_based_model(
        parse(
            """
        MODEL(name sushi.test_unrestorable);
        SELECT 2 AS two;
        """
        ),
        default_catalog=sushi_context.default_catalog,
        dialect=sushi_context.default_dialect,
    )

    sushi_context.upsert_model(model_v1)
    sushi_context.plan(auto_apply=True, no_prompts=True)
    model_v1_old_snapshot = sushi_context.get_snapshot(
        "sushi.test_unrestorable", raise_if_missing=True
    )

    sushi_context.upsert_model(model_v2)
    sushi_context.plan(
        auto_apply=True,
        no_prompts=True,
        forward_only=True,
        allow_destructive_models=["memory.sushi.test_unrestorable"],
        categorizer_config=CategorizerConfig.all_full(),
    )

    sushi_context.upsert_model(model_v1)
    sushi_context.plan(
        auto_apply=True,
        no_prompts=True,
        forward_only=True,
        allow_destructive_models=["memory.sushi.test_unrestorable"],
        categorizer_config=CategorizerConfig.all_full(),
    )
    model_v1_new_snapshot = sushi_context.get_snapshot(
        "memory.sushi.test_unrestorable", raise_if_missing=True
    )

    assert (
        model_v1_new_snapshot.node.stamp
        == f"revert to {model_v1_old_snapshot.snapshot_id.identifier}"
    )
    assert model_v1_old_snapshot.snapshot_id != model_v1_new_snapshot.snapshot_id
    assert model_v1_old_snapshot.fingerprint != model_v1_new_snapshot.fingerprint


def test_default_catalog_connections(copy_to_temp_path: t.Callable):
    with patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.default_catalog",
        PropertyMock(return_value=None),
    ):
        context = Context(paths=copy_to_temp_path("examples/sushi"))
        assert context.default_catalog is None

    # Verify that providing a catalog gets set as default catalog
    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="presto"),
        default_connection=DuckDBConnectionConfig(catalogs={"catalog": ":memory:"}),
    )
    context = Context(paths=copy_to_temp_path("examples/sushi"), config=config)
    assert context.default_catalog == "catalog"


def test_load_external_models(copy_to_temp_path):
    path = copy_to_temp_path("examples/sushi")

    context = Context(paths=path)

    external_model_names = [
        m.name for m in context.models.values() if m.kind.name == ModelKindName.EXTERNAL
    ]

    assert len(external_model_names) > 0

    # from default external_models.yaml in root dir
    assert "raw.demographics" in external_model_names

    # from external_models/model1.yaml
    assert "raw.model1" in external_model_names

    # from external_models/model2.yaml
    assert "raw.model2" in external_model_names

    # from external_models/prod.yaml, should not show unless --gateway=prod
    assert "prod_raw.model1" not in external_model_names

    # get physical table names of external models using table
    assert context.resolve_table("raw.model1") == '"memory"."raw"."model1"'
    assert context.resolve_table("raw.demographics") == '"memory"."raw"."demographics"'
    assert context.resolve_table("raw.model2") == '"memory"."raw"."model2"'

    with patch.object(context.console, "log_warning") as mock_logger:
        context.table("raw.model1") == '"memory"."raw"."model1"'

        assert mock_logger.mock_calls == [
            call(
                "The SQLMesh context's `table` method is deprecated and will be removed "
                "in a future release. Please use the `resolve_table` method instead."
            )
        ]


def test_load_gateway_specific_external_models(copy_to_temp_path):
    path = copy_to_temp_path("examples/sushi")

    def _get_external_model_names(gateway=None):
        context = Context(paths=path, config="isolated_systems_config", gateway=gateway)

        external_model_names = [
            m.name for m in context.models.values() if m.kind.name == ModelKindName.EXTERNAL
        ]

        assert len(external_model_names) > 0

        return external_model_names

    # default gateway is dev, prod model should not show
    assert "prod_raw.model1" not in _get_external_model_names()

    # gateway explicitly set to prod; prod model should now show
    assert "prod_raw.model1" in _get_external_model_names(gateway="prod")

    # test uppercase gateway name should match lowercase external model definition
    assert "prod_raw.model1" in _get_external_model_names(gateway="PROD")

    # test mixed case gateway name should also work
    assert "prod_raw.model1" in _get_external_model_names(gateway="Prod")


def test_disabled_model(copy_to_temp_path):
    path = copy_to_temp_path("examples/sushi")

    context = Context(paths=path)

    assert (path[0] / "models" / "disabled.sql").exists()
    assert not context.get_model("sushi.disabled")

    assert (path[0] / "models" / "disabled.py").exists()
    assert not context.get_model("sushi.disabled_py")


def test_disabled_model_python_macro(sushi_context):
    @model(
        "memory.sushi.disabled_model_2",
        columns={"col": "int"},
        enabled="@IF(@gateway = 'dev', True, False)",
    )
    def entrypoint(context, **kwargs):
        yield pd.DataFrame({"col": []})

    test_model = model.get_registry()["memory.sushi.disabled_model_2"].model(
        module_path=Path("."), path=Path("."), variables={"gateway": "prod"}
    )
    assert not test_model.enabled

    with pytest.raises(
        SQLMeshError,
        match="The disabled model 'memory.sushi.disabled_model_2' cannot be upserted",
    ):
        sushi_context.upsert_model(test_model)


def test_get_model_mixed_dialects(copy_to_temp_path):
    path = copy_to_temp_path("examples/sushi")

    context = Context(paths=path)
    expression = d.parse(
        """
    MODEL(
        name sushi.snowflake_dialect,
        dialect snowflake,
    );

    SELECT 1"""
    )
    model = load_sql_based_model(expression, default_catalog=context.default_catalog)
    context.upsert_model(model)

    assert context.get_model("sushi.snowflake_dialect").dict() == model.dict()


def test_override_dialect_normalization_strategy():
    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb,normalization_strategy=lowercase")
    )

    # This has the side-effect of mutating DuckDB globally to override its normalization strategy
    Context(config=config)

    from sqlglot.dialects import DuckDB
    from sqlglot.dialects.dialect import NormalizationStrategy

    assert DuckDB.NORMALIZATION_STRATEGY == NormalizationStrategy.LOWERCASE

    # The above change is applied globally so we revert it to avoid breaking other tests
    DuckDB.NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_INSENSITIVE


def test_different_gateway_normalization_strategy(tmp_path: pathlib.Path):
    config = Config(
        gateways={
            "duckdb": GatewayConfig(
                connection=DuckDBConnectionConfig(database="db.db"),
                model_defaults=ModelDefaultsConfig(
                    dialect="snowflake, normalization_strategy=case_insensitive"
                ),
            )
        },
        model_defaults=ModelDefaultsConfig(dialect="snowflake"),
        default_gateway="duckdb",
    )

    from sqlglot.dialects import Snowflake
    from sqlglot.dialects.dialect import NormalizationStrategy

    assert Snowflake.NORMALIZATION_STRATEGY == NormalizationStrategy.UPPERCASE

    ctx = Context(paths=tmp_path, config=config, gateway="duckdb")

    dialect = Dialect.get_or_raise(ctx.config.dialect)

    assert dialect == "snowflake"
    assert Snowflake.NORMALIZATION_STRATEGY == NormalizationStrategy.CASE_INSENSITIVE

    Snowflake.NORMALIZATION_STRATEGY = NormalizationStrategy.UPPERCASE


def test_access_self_columns_to_types_in_macro(tmp_path: pathlib.Path):
    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "test.sql"),
        "MODEL(name test); SELECT 1 AS c; @post_statement()",
    )
    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("macros"), "post_statement.py"),
        """
from sqlglot import exp
from sqlmesh.core.macros import macro

@macro()
def post_statement(evaluator):
    if evaluator.runtime_stage != 'loading':
        assert evaluator.columns_to_types("test") == {"c": exp.DataType.build("int")}
    return None
""",
    )

    context = Context(paths=tmp_path, config=Config())
    context.plan(auto_apply=True, no_prompts=True)


def test_wildcard(copy_to_temp_path: t.Callable):
    parent_path = copy_to_temp_path("examples/multi")[0]

    context = Context(paths=f"{parent_path}/*", gateway="memory")
    assert len(context.models) == 5


def test_duckdb_state_connection_automatic_multithreaded_mode(tmp_path):
    single_threaded_config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_gateway="duckdb",
        gateways={
            "duckdb": GatewayConfig(
                connection=DuckDBConnectionConfig(concurrent_tasks=1),
                state_connection=DuckDBConnectionConfig(concurrent_tasks=1),
            )
        },
    )

    # main connection 4 concurrent tasks, state connection 1 concurrent task,
    # context should adjust concurrent tasks on state connection to match main connection
    multi_threaded_config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_gateway="duckdb",
        gateways={
            "duckdb": GatewayConfig(
                connection=DuckDBConnectionConfig(concurrent_tasks=4),
                state_connection=DuckDBConnectionConfig(concurrent_tasks=1),
            )
        },
    )

    context = Context(paths=[tmp_path], config=single_threaded_config)
    assert isinstance(context.state_sync, CachingStateSync)
    state_sync = context.state_sync.state_sync
    assert isinstance(state_sync, EngineAdapterStateSync)
    assert isinstance(state_sync.engine_adapter, DuckDBEngineAdapter)
    assert isinstance(state_sync.engine_adapter._connection_pool, SingletonConnectionPool)

    context = Context(paths=[tmp_path], config=multi_threaded_config)
    assert isinstance(context.state_sync, CachingStateSync)
    state_sync = context.state_sync.state_sync
    assert isinstance(state_sync, EngineAdapterStateSync)
    assert isinstance(state_sync.engine_adapter, DuckDBEngineAdapter)
    assert isinstance(state_sync.engine_adapter._connection_pool, ThreadLocalSharedConnectionPool)


def test_requirements(copy_to_temp_path: t.Callable):
    from sqlmesh.utils.metaprogramming import Executable

    context_path = copy_to_temp_path("examples/sushi")[0]

    with open(context_path / c.REQUIREMENTS, "w") as f:
        # Add pandas and test_package and exclude ruamel.yaml
        f.write("pandas==2.2.2\ntest_package==1.0.0\n^ruamel.yaml\n^ruamel.yaml.clib")

    context = Context(paths=context_path)

    model = context.get_model("sushi.items")
    model.python_env["ruamel"] = Executable(payload="import ruamel", kind="import")
    model.python_env["Image"] = Executable(
        payload="from ipywidgets.widgets.widget_media import Image", kind="import"
    )

    environment = context.plan(
        "dev", no_prompts=True, skip_tests=True, skip_backfill=True, auto_apply=True
    ).environment
    requirements = {"ipywidgets", "numpy", "pandas", "test_package"}
    if IS_WINDOWS:
        requirements.add("pendulum")
    assert environment.requirements["pandas"] == "2.2.2"
    assert set(environment.requirements) == requirements

    context._requirements = {"numpy": "2.1.2", "pandas": "2.2.1"}
    context._excluded_requirements = {"ipywidgets", "ruamel.yaml", "ruamel.yaml.clib"}
    diff = context.plan_builder("dev", skip_tests=True, skip_backfill=True).build().context_diff
    assert set(diff.previous_requirements) == requirements
    reqs = {"numpy", "pandas"}
    if IS_WINDOWS:
        reqs.add("pendulum")
    assert set(diff.requirements) == reqs


def test_deactivate_automatic_requirement_inference(copy_to_temp_path: t.Callable):
    context_path = copy_to_temp_path("examples/sushi")[0]
    config = next(iter(load_configs("config", Config, paths=context_path).values()))

    config.infer_python_dependencies = False
    context = Context(paths=context_path, config=config)
    environment = context.plan(
        "dev", no_prompts=True, skip_tests=True, skip_backfill=True, auto_apply=True
    ).environment

    assert environment.requirements == {"pandas": "2.2.2"}


@pytest.mark.slow
def test_rendered_diff():
    ctx = Context(config=Config())

    ctx.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name test,
                );

                CREATE TABLE IF NOT EXISTS foo AS (SELECT @OR(FALSE, TRUE));

                SELECT 4 + 2;

                CREATE TABLE IF NOT EXISTS foo2 AS (SELECT @AND(TRUE, FALSE));

                ON_VIRTUAL_UPDATE_BEGIN;
                DROP VIEW @this_model
                ON_VIRTUAL_UPDATE_END;

                """
            )
        )
    )

    ctx.plan("dev", auto_apply=True, no_prompts=True)

    # Alter the model's query and pre/post/virtual statements to cause the diff
    ctx.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name test,
                );

                CREATE TABLE IF NOT EXISTS foo AS (SELECT @AND(TRUE, NULL));

                SELECT 5 + 2;

                CREATE TABLE IF NOT EXISTS foo2 AS (SELECT @OR(TRUE, NULL));

                ON_VIRTUAL_UPDATE_BEGIN;
                DROP VIEW IF EXISTS @this_model
                ON_VIRTUAL_UPDATE_END;
                """
            )
        )
    )

    plan = ctx.plan("dev", auto_apply=True, no_prompts=True, diff_rendered=True)

    assert plan.context_diff.text_diff('"test"') == (
        "--- \n\n"
        "+++ \n\n"
        "@@ -4,15 +4,15 @@\n\n"
        ' CREATE TABLE IF NOT EXISTS "foo" AS\n'
        " (\n"
        "   SELECT\n"
        "-    FALSE OR TRUE\n"
        "+    TRUE\n"
        " )\n"
        " SELECT\n"
        '-  6 AS "_col_0"\n'
        '+  7 AS "_col_0"\n'
        ' CREATE TABLE IF NOT EXISTS "foo2" AS\n'
        " (\n"
        "   SELECT\n"
        "-    TRUE AND FALSE\n"
        "+    TRUE\n"
        " )\n"
        " ON_VIRTUAL_UPDATE_BEGIN;\n"
        '-DROP VIEW "test";\n'
        '+DROP VIEW IF EXISTS "test";\n'
        " ON_VIRTUAL_UPDATE_END;"
    )


def test_plan_enable_preview_default(sushi_context: Context, sushi_dbt_context: Context):
    assert sushi_context._plan_preview_enabled
    assert not sushi_dbt_context._plan_preview_enabled

    sushi_dbt_context.engine_adapter.SUPPORTS_CLONING = True
    assert sushi_dbt_context._plan_preview_enabled


@pytest.mark.slow
def test_raw_code_handling(sushi_test_dbt_context: Context):
    model = sushi_test_dbt_context.models['"memory"."sushi"."model_with_raw_code"']
    assert "raw_code" not in model.jinja_macros.global_objs["model"]  # type: ignore

    # logging "pre-hook" (in dbt_projects.yml) + the actual pre-hook in the model file
    assert len(model.pre_statements) == 2

    original_file_path = model.jinja_macros.global_objs["model"]["original_file_path"]  # type: ignore
    model_file_path = sushi_test_dbt_context.path / original_file_path

    raw_code_length = len(model_file_path.read_text()) - 1

    hook = model.render_pre_statements()[0]
    assert (
        hook.sql()
        == f'''CREATE TABLE IF NOT EXISTS "t" AS SELECT 'Length is {raw_code_length}' AS "length_col"'''
    )


@pytest.mark.slow
def test_dbt_models_are_not_validated(sushi_test_dbt_context: Context):
    model = sushi_test_dbt_context.models['"memory"."sushi"."non_validated_model"']

    assert model.render_query_or_raise().sql(comments=False) == 'SELECT 1 AS "c", 2 AS "c"'
    assert sushi_test_dbt_context.fetchdf(
        'SELECT * FROM "memory"."sushi"."non_validated_model"'
    ).to_dict() == {"c": {0: 1}, "c_1": {0: 2}}

    # Write a new incremental model file that should fail validation
    models_dir = sushi_test_dbt_context.path / "models"
    incremental_model_path = models_dir / "invalid_incremental.sql"
    incremental_model_content = """{{
  config(
    materialized='incremental',
    incremental_strategy='delete+insert',
  )
}}

SELECT
  1 AS c"""

    incremental_model_path.write_text(incremental_model_content)

    # Reload the context - this should raise a validation error for the incremental model
    with pytest.raises(
        ConfigError,
        match="Unmanaged incremental models with insert / overwrite enabled must specify the partitioned_by field",
    ):
        Context(paths=sushi_test_dbt_context.path, config="test_config")


def test_catalog_name_needs_to_be_quoted():
    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(catalogs={'"foo--bar"': ":memory:"}),
    )
    context = Context(config=config)
    parsed_model = parse("MODEL(name db.x, kind FULL); SELECT 1 AS c")
    context.upsert_model(load_sql_based_model(parsed_model, default_catalog='"foo--bar"'))
    context.plan(auto_apply=True, no_prompts=True)
    assert context.fetchdf('select * from "foo--bar".db.x').to_dict() == {"c": {0: 1}}


def test_plan_runs_audits_on_dev_previews(sushi_context: Context, capsys, caplog):
    sushi_context.console = create_console()

    test_model = """
    MODEL (
        name sushi.test_audit_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column event_date,
            forward_only true
        ),
        audits (
            number_of_rows(threshold := 10),
            not_null(columns := id),
            at_least_one_non_blocking(column := waiter_id)
        )
    );

    SELECT * FROM sushi.orders WHERE event_date BETWEEN @start_ts AND @end_ts
    """

    sushi_context.upsert_model(
        load_sql_based_model(parse(test_model), default_catalog=sushi_context.default_catalog)
    )
    plan = sushi_context.plan(auto_apply=True)

    assert plan.new_snapshots[0].name == '"memory"."sushi"."test_audit_model"'
    assert plan.deployability_index.is_deployable(plan.new_snapshots[0])

    # now, we mutate the model and run a plan in dev to create a dev preview
    test_model = """
    MODEL (
        name sushi.test_audit_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column event_date,
            forward_only true
        ),
        audits (
            not_null(columns := new_col),
            at_least_one_non_blocking(column := new_col)
        )
    );

    SELECT *, null as new_col FROM sushi.orders WHERE event_date BETWEEN @start_ts AND @end_ts
    """

    sushi_context.upsert_model(
        load_sql_based_model(parse(test_model), default_catalog=sushi_context.default_catalog)
    )

    capsys.readouterr()  # clear output buffer
    plan = sushi_context.plan(environment="dev", auto_apply=True)

    assert len(plan.new_snapshots) == 1
    dev_preview = plan.new_snapshots[0]
    assert dev_preview.name == '"memory"."sushi"."test_audit_model"'
    assert dev_preview.is_forward_only
    assert not plan.deployability_index.is_deployable(
        dev_preview
    )  # if something is not deployable to prod, then its by definiton a dev preview

    # we only see audit results if they fail
    stdout = capsys.readouterr().out
    log = caplog.text
    assert "'not_null' audit error:" in log
    assert "'at_least_one_non_blocking' audit error:" in log
    assert "Virtual layer updated" in stdout


def test_environment_statements(tmp_path: pathlib.Path):
    models_dir = pathlib.Path("models")
    macros_dir = pathlib.Path("macros")
    dialect = "postgres"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=dialect),
        before_all=[
            "CREATE TABLE IF NOT EXISTS analytic_stats (physical_table VARCHAR, evaluation_time VARCHAR)"
        ],
        after_all=[
            "@grant_schema_usage()",
            "@grant_usage_role(@schemas, 'admin')",
            "@grant_select_privileges()",
        ],
    )

    expression = """
MODEL(
  name db.test_after_model,
  kind full
);

SELECT 1 AS col_a;
    """

    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "db", "test_after_model.sql"),
        expression,
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "grant_select_privileges.py"),
        """
from sqlmesh.core.macros import macro
@macro()
def grant_select_privileges(evaluator):
    if evaluator.this_env and evaluator.views:
        return [
            f"GRANT SELECT ON VIEW {view_name} /* sqlglot.meta replace=false */ TO ROLE admin_role;"
            for view_name in evaluator.views
        ]
""",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "grant_schema_file.py"),
        """
from sqlmesh import macro

@macro()
def grant_schema_usage(evaluator):
    if evaluator._environment_naming_info:
        schemas = {
            snapshot.qualified_view_name.schema_for_environment(
                evaluator._environment_naming_info
            )
            for snapshot in evaluator._snapshots.values()
            if snapshot.is_model
        }
        return [
            f"GRANT USAGE ON SCHEMA {schema} TO user_role;"
            for schema in schemas
        ]
""",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "grant_usage_file.py"),
        """
from sqlmesh import macro

@macro()
def grant_usage_role(evaluator, schemas, role):
    if evaluator._environment_naming_info:
        return [
            f"GRANT USAGE ON SCHEMA {schema} TO {role};"
            for schema in schemas
        ]
""",
    )

    context = Context(paths=tmp_path, config=config)
    snapshots = {s.name: s for s in context.snapshots.values()}

    environment_statements = context._environment_statements[0]
    before_all = environment_statements.before_all
    after_all = environment_statements.after_all
    python_env = environment_statements.python_env

    assert isinstance(python_env["grant_schema_usage"], Executable)
    assert isinstance(python_env["grant_usage_role"], Executable)
    assert isinstance(python_env["grant_select_privileges"], Executable)

    before_all_rendered = render_statements(
        statements=before_all,
        dialect=dialect,
        python_env=python_env,
        runtime_stage=RuntimeStage.BEFORE_ALL,
    )

    assert before_all_rendered == [
        "CREATE TABLE IF NOT EXISTS analytic_stats (physical_table VARCHAR, evaluation_time VARCHAR)"
    ]

    after_all_rendered = render_statements(
        statements=after_all,
        dialect=dialect,
        python_env=python_env,
        snapshots=snapshots,
        environment_naming_info=EnvironmentNamingInfo(name="prod"),
        runtime_stage=RuntimeStage.AFTER_ALL,
    )

    assert sorted(after_all_rendered) == sorted(
        [
            "GRANT USAGE ON SCHEMA db TO user_role",
            'GRANT USAGE ON SCHEMA "db" TO "admin"',
            "GRANT SELECT ON VIEW memory.db.test_after_model /* sqlglot.meta replace=false */ TO ROLE admin_role",
        ]
    )

    after_all_rendered_dev = render_statements(
        statements=after_all,
        dialect=dialect,
        python_env=python_env,
        snapshots=snapshots,
        environment_naming_info=EnvironmentNamingInfo(name="dev"),
        runtime_stage=RuntimeStage.AFTER_ALL,
    )

    assert sorted(after_all_rendered_dev) == sorted(
        [
            "GRANT USAGE ON SCHEMA db__dev TO user_role",
            'GRANT USAGE ON SCHEMA "db__dev" TO "admin"',
            "GRANT SELECT ON VIEW memory.db__dev.test_after_model /* sqlglot.meta replace=false */ TO ROLE admin_role",
        ]
    )


def test_plan_environment_statements(tmp_path: pathlib.Path):
    models_dir = pathlib.Path("models")
    macros_dir = pathlib.Path("macros")
    dialect = "duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=dialect),
        before_all=["@create_stats_table()", "@access_adapter()"],
        after_all=["CREATE TABLE IF NOT EXISTS after_table AS SELECT @some_var"],
        variables={"some_var": 5},
    )

    model_file = """
MODEL(
  name db.test_stats_model,
  kind full,
);

@IF(
  @runtime_stage IN ('evaluating', 'creating'),
  SET VARIABLE stats_model_start = now()
);

SELECT 1 AS cola;

@IF(
  @runtime_stage IN ('evaluating', 'creating'),
  INSERT INTO analytic_stats (physical_table, evaluation_start, evaluation_end, evaluation_time)
  VALUES (@resolve_template('@{schema_name}.@{table_name}'), getvariable('stats_model_start'), now(), now() - getvariable('stats_model_start'))
);

    """

    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "db", "test_stats_model.sql"),
        model_file,
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "create_stats_table.py"),
        """
from sqlmesh.core.macros import macro

@macro()
def create_stats_table(evaluator):
    return "CREATE TABLE IF NOT EXISTS analytic_stats (physical_table VARCHAR, evaluation_start VARCHAR,  evaluation_end VARCHAR, evaluation_time VARCHAR)"
""",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "access_adapter.py"),
        """
from sqlmesh.core.macros import macro

@macro()
def access_adapter(evaluator):
    if evaluator.runtime_stage == 'before_all':
        engine_adapter = evaluator.engine_adapter
        for i in range(10):
            try:
                sql_inside_macro = f"CREATE TABLE IF NOT EXISTS db_connect AS SELECT {i} as 'access_attempt'"
                engine_adapter.execute(sql_inside_macro)
                return None
            except Exception as e:
                sleep(10)
        raise Exception(f"Failed to connect to the database")
    """,
    )

    context = Context(paths=tmp_path, config=config)

    assert context._environment_statements[0].before_all == [
        "@create_stats_table()",
        "@access_adapter()",
    ]

    assert context._environment_statements[0].after_all == [
        "CREATE TABLE IF NOT EXISTS after_table AS SELECT @some_var"
    ]
    assert context._environment_statements[0].python_env["create_stats_table"]

    context.plan(auto_apply=True, no_prompts=True)

    model = context.get_model("db.test_stats_model")
    snapshot = context.get_snapshot("db.test_stats_model")
    assert snapshot and snapshot.version

    assert (
        model.pre_statements[0].sql()
        == "@IF(@runtime_stage IN ('evaluating', 'creating'), SET stats_model_start = NOW())"
    )
    assert (
        model.post_statements[0].sql()
        == "@IF(@runtime_stage IN ('evaluating', 'creating'), INSERT INTO analytic_stats (physical_table, evaluation_start, evaluation_end, evaluation_time) VALUES (@resolve_template('@{schema_name}.@{table_name}'), GETVARIABLE('stats_model_start'), NOW(), NOW() - GETVARIABLE('stats_model_start')))"
    )

    stats_table = context.fetchdf("select * from memory.analytic_stats").to_dict()
    assert stats_table.keys() == {
        "physical_table",
        "evaluation_start",
        "evaluation_end",
        "evaluation_time",
    }
    assert (
        stats_table["physical_table"][0] == f"sqlmesh__db.db__test_stats_model__{snapshot.version}"
    )

    assert context.fetchdf("select * from memory.after_table").to_dict()["5"][0] == 5

    state_table = context.state_reader.get_environment_statements(c.PROD)
    assert state_table[0].before_all == context._environment_statements[0].before_all
    assert state_table[0].after_all == context._environment_statements[0].after_all
    assert state_table[0].python_env == context._environment_statements[0].python_env

    # This table will be created inside the macro by accessing the engine_adapter directly
    inside_macro_execute = context.fetchdf("select * from memory.db_connect").to_dict()
    assert (attempt_column := inside_macro_execute.get("access_attempt"))
    assert isinstance(attempt_column, dict) and attempt_column[0] < 10


def test_environment_statements_dialect(tmp_path: Path):
    before_all = [
        "EXPORT DATA OPTIONS (URI='gs://path*.csv.gz', FORMAT='CSV') AS SELECT * FROM all_rows"
    ]
    after_all = ["@IF(@this_env = 'prod', CREATE TABLE IF NOT EXISTS after_t AS SELECT 1)"]
    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="bigquery"),
        before_all=before_all,
        after_all=after_all,
    )
    ctx = Context(paths=[tmp_path], config=config)
    assert ctx._environment_statements == [
        EnvironmentStatements(before_all=before_all, after_all=after_all, python_env={})
    ]

    # Without the correct dialect this statement should error out instead
    with pytest.raises(ParseError, match=r"Invalid expression / Unexpected token*"):
        config.model_defaults.dialect = "duckdb"
        ctx = Context(paths=[tmp_path], config=config)


@pytest.mark.slow
@use_terminal_console
def test_model_linting(tmp_path: pathlib.Path, sushi_context) -> None:
    def assert_cached_violations_exist(cache: OptimizedQueryCache, model: Model):
        model = t.cast(SqlModel, model)
        cache_entry = cache._file_cache.get(cache._entry_name(model))
        assert cache_entry is not None
        assert cache_entry.optimized_rendered_query is not None
        assert cache_entry.renderer_violations is not None

    cfg = LinterConfig(enabled=True, rules="ALL")
    ctx = Context(
        config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"), linter=cfg),
        paths=tmp_path,
    )

    config_err = "Linter detected errors in the code. Please fix them before proceeding."

    # Case: Ensure load DOES NOT work if linter is enabled
    for query in ("SELECT * FROM tbl", "SELECT t.* FROM tbl"):
        with pytest.raises(LinterError, match=config_err):
            ctx.upsert_model(load_sql_based_model(d.parse(f"MODEL (name test); {query}")))
            ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

    error_model = load_sql_based_model(d.parse("MODEL (name test); SELECT * FROM (SELECT 1)"))
    with pytest.raises(LinterError, match=config_err):
        ctx.upsert_model(error_model)
        ctx.plan_builder("dev")

    # Case: Ensure error violations are cached if the model did not pass linting
    cache = OptimizedQueryCache(ctx.cache_dir)

    assert_cached_violations_exist(cache, error_model)

    # Case: Ensure NoSelectStar only raises for top-level SELECTs, new model shouldn't raise
    # and thus should also be cached
    model2 = load_sql_based_model(
        d.parse(
            "MODEL (name test2, audits (at_least_one(column := col))); SELECT col FROM (SELECT * FROM tbl)"
        )
    )
    ctx.upsert_model(model2)

    model2 = t.cast(SqlModel, model2)
    assert cache._file_cache.exists(cache._entry_name(model2))

    # Case: Ensure warning violations are found again even if the optimized query is cached
    ctx.config.linter = LinterConfig(enabled=True, warn_rules="ALL")
    ctx.load()

    for i in range(3):
        with patch.object(get_console(), "log_warning") as mock_logger:
            if i > 1:
                # Model's violations have been cached from the previous upserts
                assert_cached_violations_exist(cache, model2)

            ctx.upsert_model(error_model)
            ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

            assert (
                """noselectstar - Query should not contain SELECT * on its outer most projections"""
                in mock_logger.call_args[0][0]
            )

            # Model's violations have been cached after the former upsert
            assert_cached_violations_exist(cache, model2)

    # Case: Ensure load WORKS if linter is enabled but the rules are not
    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "test.sql"),
        "MODEL(name test); SELECT * FROM (SELECT 1 AS col);",
    )

    ignore_or_warn_cfgs = [
        LinterConfig(enabled=True, warn_rules=["noselectstar"]),
        LinterConfig(enabled=True, ignored_rules=["noselectstar"]),
    ]
    for cfg in ignore_or_warn_cfgs:
        ctx.config.linter = cfg
        ctx.load()

    # Case: Ensure load DOES NOT work if LinterConfig has overlapping rules
    with pytest.raises(
        ConfigError,
        match=r"Rules cannot simultaneously warn and raise an error: \[noselectstar\]",
    ):
        ctx.config.linter = LinterConfig(
            enabled=True, rules=["noselectstar"], warn_rules=["noselectstar"]
        )
        ctx.load()

    # Case: Ensure model attribute overrides global config
    ctx.config.linter = LinterConfig(enabled=True, rules=["noselectstar"])

    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "test.sql"),
        "MODEL(name test, ignored_rules ['ALL']); SELECT * FROM (SELECT 1 AS col);",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(pathlib.Path("models"), "test2.sql"),
        "MODEL(name test2, audits (at_least_one(column := col)), ignored_rules ['noselectstar']); SELECT * FROM (SELECT 1 AS col);",
    )

    ctx.plan(environment="dev", auto_apply=True, no_prompts=True)

    # Case: Ensure we can load & use the user-defined rules
    sushi_context.config.linter = LinterConfig(enabled=True, rules=["aLl"])
    sushi_context.load()
    sushi_context.upsert_model(
        load_sql_based_model(
            d.parse("MODEL (name sushi.test); SELECT col FROM (SELECT * FROM tbl)"),
            default_catalog="memory",
        )
    )

    with pytest.raises(LinterError, match=config_err):
        sushi_context.plan_builder(environment="dev")

    # Case: Ensure the Linter also picks up Python model violations
    @model(name="memory.sushi.model3", is_sql=True, kind="full", dialect="snowflake")
    def model3_entrypoint(evaluator: MacroEvaluator) -> str:
        return "select * from model1"

    model3 = model.get_registry()["memory.sushi.model3"].model(
        module_path=Path("."), path=Path(".")
    )

    @model(name="memory.sushi.model4", columns={"col": "int"})
    def model4_entrypoint(context, **kwargs):
        yield pd.DataFrame({"col": []})

    model4 = model.get_registry()["memory.sushi.model4"].model(
        module_path=Path("."), path=Path(".")
    )

    for python_model in (model3, model4):
        with pytest.raises(LinterError, match=config_err):
            sushi_context.upsert_model(python_model)
            sushi_context.plan(environment="dev", auto_apply=True, no_prompts=True)


def test_plan_selector_expression_no_match(sushi_context: Context) -> None:
    with pytest.raises(
        PlanError,
        match="Selector did not return any models. Please check your model selection and try again.",
    ):
        sushi_context.plan("dev", select_models=["*missing*"])

    with pytest.raises(
        PlanError,
        match="Selector did not return any models. Please check your model selection and try again.",
    ):
        sushi_context.plan("dev", backfill_models=["*missing*"])

    with pytest.raises(
        PlanError,
        match="Selector did not return any models. Please check your model selection and try again.",
    ):
        sushi_context.plan("prod", restate_models=["*missing*"])


def test_plan_select_model_deleted_model(sushi_context: Context) -> None:
    """Selecting a model that has been deleted locally but still exists in the deployed
    environment should produce a valid plan with the deletion, not raise PlanError."""
    # Pick a leaf model that can be safely deleted without breaking other models' rendering.
    model_name = "sushi.top_waiters"
    snapshot = sushi_context.get_snapshot(model_name)
    assert snapshot is not None

    # Delete the model file from disk.
    model = sushi_context.get_model(model_name)
    assert model._path is not None and model._path.exists()
    model._path.unlink()

    # Reload the context so it no longer knows about the deleted model.
    sushi_context.load()
    assert model_name not in [m for m in sushi_context.models]

    # Planning with select_models for the deleted model should succeed (not raise PlanError).
    plan = sushi_context.plan("prod", select_models=[model_name], no_prompts=True)
    assert plan is not None

    # The deleted model should appear in removed_snapshots.
    removed_names = {s.name for s in plan.context_diff.removed_snapshots.values()}
    assert snapshot.name in removed_names


def test_plan_select_model_deleted_model_still_rejects_nonexistent(
    sushi_context: Context,
) -> None:
    """A model that neither exists locally nor in the deployed environment should still
    raise PlanError."""
    with pytest.raises(
        PlanError,
        match="Selector did not return any models. Please check your model selection and try again.",
    ):
        sushi_context.plan("prod", select_models=["sushi.completely_nonexistent"])


def test_plan_on_virtual_update_this_model_in_macro(tmp_path: pathlib.Path):
    models_dir = pathlib.Path("models")
    macros_dir = pathlib.Path("macros")
    dialect = "duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=dialect),
    )

    model_file = """
MODEL(
  name db.test_view_macro_this_model,
  kind full,
);


SELECT 1 AS cola;

ON_VIRTUAL_UPDATE_BEGIN;
CREATE OR REPLACE TABLE log_schema AS SELECT @resolve_template('@{schema_name}') as my_schema;
@create_log_view(@this_model);
ON_VIRTUAL_UPDATE_END;

    """

    create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "db", "test_view_macro_this_model.sql"),
        model_file,
    )

    create_temp_file(
        tmp_path,
        pathlib.Path(macros_dir, "create_log_view.py"),
        """
from sqlmesh.core.macros import macro

@macro()
def create_log_view(evaluator, view_name):
    return f"CREATE OR REPLACE TABLE log_view AS SELECT '{view_name}' as fqn_this_model,  '{evaluator.this_model}' as evaluator_this_model;"
""",
    )

    context = Context(paths=tmp_path, config=config)
    context.plan(environment="dev", auto_apply=True, no_prompts=True)

    model = context.get_model("db.test_view_macro_this_model")
    assert (
        model.on_virtual_update[0].sql(dialect=dialect)
        == "CREATE OR REPLACE TABLE log_schema AS SELECT @resolve_template('@{schema_name}') AS my_schema"
    )
    assert model.on_virtual_update[1].sql(dialect=dialect) == "@create_log_view(@this_model)"

    snapshot = context.get_snapshot("db.test_view_macro_this_model")
    assert snapshot and snapshot.version

    log_view = context.fetchdf("select * from log_view").to_dict()
    log_schema = context.fetchdf("select * from log_schema").to_dict()

    # Validate that within macro for this_model we resolve to the environment-specific view
    assert (
        log_view["fqn_this_model"][0]
        == '"db__dev"."test_view_macro_this_model" /* memory.db.test_view_macro_this_model */'
    )

    # Validate that from the macro evaluator this_model we get the environment-specific fqn
    assert log_view["evaluator_this_model"][0] == '"db__dev"."test_view_macro_this_model"'

    # Validate the schema is retrieved using resolve_template for the environment-specific schema
    assert log_schema["my_schema"][0] == "db__dev"


def test_plan_audit_intervals(tmp_path: pathlib.Path, caplog):
    ctx = Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    )

    ctx.upsert_model(
        load_sql_based_model(
            parse(
                """
            MODEL (
                name sqlmesh_audit.date_example,
                kind INCREMENTAL_BY_TIME_RANGE(
                    time_column(date_id, '%Y-%m-%d')
                ),
                cron '@daily',
                partitioned_by (date_id),
                audits [unique_combination_of_columns(columns=(date_id))]
            );

            WITH sample_table AS (
            SELECT
                DATE('2025-02-01') as date_id,
            )
            SELECT date_id FROM sample_table WHERE date_id BETWEEN @start_ds AND @end_ds
            """
            )
        )
    )

    ctx.upsert_model(
        load_sql_based_model(
            parse(
                """
            MODEL (
                name sqlmesh_audit.timestamp_example,
                kind INCREMENTAL_BY_TIME_RANGE(
                    time_column(timestamp_id, '%Y-%m-%d %H:%M:%S')
                ),
                cron '@daily',
                partitioned_by (timestamp_id),
                audits [unique_combination_of_columns(columns=(timestamp_id))]
            );

            WITH sample_table AS (
            SELECT
                TIMESTAMP('2025-02-01') as timestamp_id,
            )
            SELECT timestamp_id FROM sample_table WHERE timestamp_id BETWEEN @start_ts AND @end_ts
            """
            )
        )
    )

    plan = ctx.plan(
        environment="dev", auto_apply=True, no_prompts=True, start="2025-02-01", end="2025-02-01"
    )
    assert plan.missing_intervals

    date_snapshot = next(s for s in plan.new_snapshots if "date_example" in s.name)
    timestamp_snapshot = next(s for s in plan.new_snapshots if "timestamp_example" in s.name)

    # Case 1: The timestamp audit should be in the inclusive range ['2025-02-01 00:00:00', '2025-02-01 23:59:59.999999']
    assert (
        f"""SELECT COUNT(*) FROM (SELECT "timestamp_id" AS "timestamp_id" FROM (SELECT * FROM "sqlmesh__sqlmesh_audit"."sqlmesh_audit__timestamp_example__{timestamp_snapshot.version}" AS "sqlmesh_audit__timestamp_example__{timestamp_snapshot.version}" WHERE "timestamp_id" BETWEEN CAST('2025-02-01 00:00:00' AS TIMESTAMP) AND CAST('2025-02-01 23:59:59.999999' AS TIMESTAMP)) AS "_0" WHERE TRUE GROUP BY "timestamp_id" HAVING COUNT(*) > 1) AS "audit\""""
        in caplog.text
    )

    # Case 2: The date audit should be in the inclusive range ['2025-02-01', '2025-02-01']
    assert (
        f"""SELECT COUNT(*) FROM (SELECT "date_id" AS "date_id" FROM (SELECT * FROM "sqlmesh__sqlmesh_audit"."sqlmesh_audit__date_example__{date_snapshot.version}" AS "sqlmesh_audit__date_example__{date_snapshot.version}" WHERE "date_id" BETWEEN CAST('2025-02-01' AS DATE) AND CAST('2025-02-01' AS DATE)) AS "_0" WHERE TRUE GROUP BY "date_id" HAVING COUNT(*) > 1) AS "audit\""""
        in caplog.text
    )


def test_check_intervals(sushi_context, mocker):
    with pytest.raises(
        SQLMeshError,
        match="Environment 'dev' was not found",
    ):
        sushi_context.check_intervals(environment="dev", no_signals=False, select_models=[])

    spy = mocker.spy(sqlmesh.core.snapshot.definition, "check_ready_intervals")
    intervals = sushi_context.check_intervals(environment=None, no_signals=False, select_models=[])

    min_intervals = 19
    assert spy.call_count == 2
    assert len(intervals) >= min_intervals

    for i in intervals.values():
        assert not i.intervals

    spy.reset_mock()
    intervals = sushi_context.check_intervals(environment=None, no_signals=True, select_models=[])
    assert spy.call_count == 0
    assert len(intervals) >= min_intervals

    intervals = sushi_context.check_intervals(
        environment=None, no_signals=False, select_models=["*waiter_as_customer*"]
    )
    assert len(intervals) == 1

    intervals = sushi_context.check_intervals(
        environment=None, no_signals=False, select_models=["*waiter_as_customer*"], end="next week"
    )
    assert tuple(intervals.values())[0].intervals


def test_audit():
    context = Context(config=Config())

    parsed_model = parse(
        """
        MODEL (
          name dummy,
          audits (
            not_null_non_blocking(columns=[c])
          )
        );

        SELECT NULL AS c
        """
    )
    context.upsert_model(load_sql_based_model(parsed_model))
    context.plan(no_prompts=True, auto_apply=True)

    assert context.audit(models=["dummy"], start="2020-01-01", end="2020-01-01") is False

    parsed_model = parse(
        """
        MODEL (
          name dummy,
          audits (
            not_null_non_blocking(columns=[c])
          )
        );

        SELECT 1 AS c
        """
    )
    context.upsert_model(load_sql_based_model(parsed_model))
    context.plan(no_prompts=True, auto_apply=True)

    assert context.audit(models=["dummy"], start="2020-01-01", end="2020-01-01") is True


def test_prompt_if_uncategorized_snapshot(mocker: MockerFixture, tmp_path: Path) -> None:
    init_example_project(tmp_path, engine_type="duckdb")

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        plan=PlanConfig(
            auto_categorize_changes=CategorizerConfig(
                external=AutoCategorizationMode.OFF,
                python=AutoCategorizationMode.OFF,
                sql=AutoCategorizationMode.OFF,
                seed=AutoCategorizationMode.OFF,
            ),
        ),
    )
    context = Context(paths=tmp_path, config=config)
    context.plan(no_prompts=True, auto_apply=True)

    incremental_model = context.get_model("sqlmesh_example.incremental_model")
    incremental_model_query = incremental_model.render_query()
    new_incremental_model_query = t.cast(exp.Select, incremental_model_query).select("1 AS z")
    context.upsert_model(
        "sqlmesh_example.incremental_model",
        query_=ParsableSql(sql=new_incremental_model_query.sql(dialect=incremental_model.dialect)),
    )

    mock_console = mocker.Mock()
    spy_plan = mocker.spy(mock_console, "plan")
    context.console = mock_console

    context.plan()

    calls = spy_plan.mock_calls
    assert len(calls) == 1

    # Show that the presence of uncategorized snapshots forces no_prompts to
    # False instead of respecting the default plan config value, which is True
    assert calls[0].kwargs["no_prompts"] == False
    assert context.config.plan.no_prompts == True


def test_plan_explain_skips_tests(sushi_context: Context, mocker: MockerFixture) -> None:
    sushi_context.console = TerminalConsole()
    spy = mocker.spy(sushi_context, "_run_plan_tests")
    sushi_context.plan(environment="dev", explain=True, no_prompts=True, include_unmodified=True)
    spy.assert_called_once_with(skip_tests=True)


def test_dev_environment_virtual_update_with_environment_statements(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    model_sql = """
    MODEL (
        name db.test_model,
        kind FULL
    );

    SELECT 1 as id, 'test' as name
    """

    with open(models_dir / "test_model.sql", "w") as f:
        f.write(model_sql)

    # Create initial context without environment statements
    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        gateways={"duckdb": GatewayConfig(connection=DuckDBConnectionConfig())},
    )

    context = Context(paths=tmp_path, config=config)

    # First, apply to production
    context.plan("prod", auto_apply=True, no_prompts=True)

    # Try to create dev environment without changes (should fail)
    with pytest.raises(NoChangesPlanError, match="Creating a new environment requires a change"):
        context.plan("dev", auto_apply=True, no_prompts=True)

    # Now create a new context with only new environment statements
    config_with_statements = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        gateways={"duckdb": GatewayConfig(connection=DuckDBConnectionConfig())},
        before_all=["CREATE TABLE IF NOT EXISTS audit_log (id INT, action VARCHAR(100))"],
        after_all=["INSERT INTO audit_log VALUES (1, 'environment_created')"],
    )

    context_with_statements = Context(paths=tmp_path, config=config_with_statements)

    # This should succeed because environment statements are different
    context_with_statements.plan("dev", auto_apply=True, no_prompts=True)
    env = context_with_statements.state_reader.get_environment("dev")
    assert env is not None
    assert env.name == "dev"

    # Verify the environment statements were stored
    stored_statements = context_with_statements.state_reader.get_environment_statements("dev")
    assert len(stored_statements) == 1
    assert stored_statements[0].before_all == [
        "CREATE TABLE IF NOT EXISTS audit_log (id INT, action VARCHAR(100))"
    ]
    assert stored_statements[0].after_all == [
        "INSERT INTO audit_log VALUES (1, 'environment_created')"
    ]

    # Update environment statements and plan again (should trigger another virtual update)
    config_updated_statements = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        gateways={"duckdb": GatewayConfig(connection=DuckDBConnectionConfig())},
        before_all=[
            "CREATE TABLE IF NOT EXISTS audit_log (id INT, action VARCHAR(100))",
            "CREATE TABLE IF NOT EXISTS metrics (metric_name VARCHAR(50), value INT)",
        ],
        after_all=["INSERT INTO audit_log VALUES (1, 'environment_created')"],
    )

    context_updated = Context(paths=tmp_path, config=config_updated_statements)
    context_updated.plan("dev", auto_apply=True, no_prompts=True)

    # Verify the updated statements were stored
    updated_statements = context_updated.state_reader.get_environment_statements("dev")
    assert len(updated_statements) == 1
    assert len(updated_statements[0].before_all) == 2
    assert (
        updated_statements[0].before_all[1]
        == "CREATE TABLE IF NOT EXISTS metrics (metric_name VARCHAR(50), value INT)"
    )


def test_table_diff_ignores_extra_args(sushi_context: Context):
    sushi_context.plan(environment="dev", auto_apply=True, include_unmodified=True)

    # the test fails if this call throws an exception
    sushi_context.table_diff(
        source="prod",
        target="dev",
        select_models=["sushi.customers"],
        on=["customer_id"],
        show_sample=True,
        some_tcloud_option=1_000,
    )


def test_plan_min_intervals(tmp_path: Path):
    init_example_project(tmp_path, engine_type="duckdb", dialect="duckdb")

    context = Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    )

    current_time = to_datetime("2020-02-01 00:00:01")

    # initial state of example project
    context.plan(auto_apply=True, execution_time=current_time)

    (tmp_path / "models" / "daily_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.daily_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '@daily'
    );

    select @start_ds as start_ds, @end_ds as end_ds, @start_dt as start_dt, @end_dt as end_dt;
    """)

    (tmp_path / "models" / "weekly_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.weekly_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '@weekly'
    );

    select @start_ds as start_ds, @end_ds as end_ds, @start_dt as start_dt, @end_dt as end_dt;
    """)

    (tmp_path / "models" / "monthly_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.monthly_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '@monthly'
    );

    select @start_ds as start_ds, @end_ds as end_ds, @start_dt as start_dt, @end_dt as end_dt;
    """)

    (tmp_path / "models" / "ended_daily_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.ended_daily_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      end '2020-01-18',
      cron '@daily'
    );

    select @start_ds as start_ds, @end_ds as end_ds, @start_dt as start_dt, @end_dt as end_dt;
    """)

    context.load()

    # initial state - backfill from 2020-01-01 -> now() (2020-01-02 00:00:01) on new models
    plan = context.plan(execution_time=current_time)

    assert to_datetime(plan.start) == to_datetime("2020-01-01 00:00:00")
    assert to_datetime(plan.end) == to_datetime("2020-02-01 00:00:00")
    assert to_datetime(plan.execution_time) == to_datetime("2020-02-01 00:00:01")

    def _get_missing_intervals(plan: Plan, name: str) -> t.List[t.Tuple[datetime, datetime]]:
        snapshot_id = context.get_snapshot(name, raise_if_missing=True).snapshot_id
        snapshot_intervals = next(
            si for si in plan.missing_intervals if si.snapshot_id == snapshot_id
        )
        return [(to_datetime(s), to_datetime(e)) for s, e in snapshot_intervals.merged_intervals]

    # check initial intervals - should be full time range between start and execution time
    assert len(plan.missing_intervals) == 4

    assert _get_missing_intervals(plan, "sqlmesh_example.daily_model") == [
        (to_datetime("2020-01-01 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]
    assert _get_missing_intervals(plan, "sqlmesh_example.weekly_model") == [
        (
            to_datetime("2020-01-01 00:00:00"),
            to_datetime("2020-01-26 00:00:00"),
        )  # last week in 2020-01 hasnt fully elapsed yet
    ]
    assert _get_missing_intervals(plan, "sqlmesh_example.monthly_model") == [
        (to_datetime("2020-01-01 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]
    assert _get_missing_intervals(plan, "sqlmesh_example.ended_daily_model") == [
        (to_datetime("2020-01-01 00:00:00"), to_datetime("2020-01-19 00:00:00"))
    ]

    # now, create a dev env for "1 day ago" with min_intervals=1
    plan = context.plan(
        environment="pr_env",
        start="1 day ago",
        execution_time=current_time,
        min_intervals=1,
    )

    # this should pick up last day for daily model, last week for weekly model, last month for the monthly model and the last day of "ended_daily_model" before it ended
    assert len(plan.missing_intervals) == 4

    assert _get_missing_intervals(plan, "sqlmesh_example.daily_model") == [
        (to_datetime("2020-01-31 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]
    assert _get_missing_intervals(plan, "sqlmesh_example.weekly_model") == [
        (
            to_datetime("2020-01-19 00:00:00"),  # last completed week
            to_datetime("2020-01-26 00:00:00"),
        )
    ]
    assert _get_missing_intervals(plan, "sqlmesh_example.monthly_model") == [
        (
            to_datetime("2020-01-01 00:00:00"),  # last completed month
            to_datetime("2020-02-01 00:00:00"),
        )
    ]
    assert _get_missing_intervals(plan, "sqlmesh_example.ended_daily_model") == [
        (
            to_datetime("2020-01-18 00:00:00"),  # last day before the model end date
            to_datetime("2020-01-19 00:00:00"),
        )
    ]

    # run the plan for '1 day ago' but min_intervals=1
    context.apply(plan)

    # show that the data was created (which shows that when the Plan became an EvaluatablePlan and eventually evaluated, the start date overrides didnt get dropped)
    assert context.engine_adapter.fetchall(
        "select start_dt, end_dt from sqlmesh_example__pr_env.daily_model"
    ) == [(to_datetime("2020-01-31 00:00:00"), to_datetime("2020-01-31 23:59:59.999999"))]
    assert context.engine_adapter.fetchall(
        "select start_dt, end_dt from sqlmesh_example__pr_env.weekly_model"
    ) == [
        (to_datetime("2020-01-19 00:00:00"), to_datetime("2020-01-25 23:59:59.999999")),
    ]
    assert context.engine_adapter.fetchall(
        "select start_dt, end_dt from sqlmesh_example__pr_env.monthly_model"
    ) == [
        (to_datetime("2020-01-01 00:00:00"), to_datetime("2020-01-31 23:59:59.999999")),
    ]
    assert context.engine_adapter.fetchall(
        "select start_dt, end_dt from sqlmesh_example__pr_env.ended_daily_model"
    ) == [
        (to_datetime("2020-01-18 00:00:00"), to_datetime("2020-01-18 23:59:59.999999")),
    ]


def test_plan_min_intervals_adjusted_for_downstream(tmp_path: Path):
    """
    Scenario:
        A(hourly) <- B(daily) <- C(weekly)
                  <- D(two-hourly)
        E(monthly)

    We need to ensure that :min_intervals covers at least :min_intervals of all downstream models for the dag to be valid
    In this scenario, if min_intervals=1:
        - A would need to cover at least (7 days * 24 hours) because its downstream model C is weekly. It should also be unaffected by its sibling, E
        - B would need to cover at least 7 days because its downstream model C is weekly
        - C would need to cover at least 1 week because min_intervals: 1
        - D would need to cover at least 2 hours because min_intervals: 1 and should be unaffected by C
        - E is unrelated to A, B, C and D so would need to cover 1 month satisfy min_intervals: 1.
            - It also ensures that each tree branch has a unique cumulative date, because
              if the dag is iterated purely in topological order with a global min date it would set A to to 1 month instead if 1 week
    """

    init_example_project(tmp_path, engine_type="duckdb", dialect="duckdb")

    context = Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    )

    current_time = to_datetime("2020-02-01 00:00:01")

    # initial state of example project
    context.plan(auto_apply=True, execution_time=current_time)

    (tmp_path / "models" / "hourly_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.hourly_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt,
        batch_size 1
      ),
      start '2020-01-01',
      cron '@hourly'
    );

    select @start_dt as start_dt, @end_dt as end_dt;
    """)

    (tmp_path / "models" / "two_hourly_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.two_hourly_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '0 */2 * * *'
    );

    select start_dt, end_dt from sqlmesh_example.hourly_model where start_dt between @start_dt and @end_dt;
    """)

    (tmp_path / "models" / "unrelated_monthly_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.unrelated_monthly_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '@monthly'
    );

    select @start_dt as start_dt, @end_dt as end_dt;
    """)

    (tmp_path / "models" / "daily_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.daily_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '@daily'
    );

    select start_dt, end_dt from sqlmesh_example.hourly_model where start_dt between @start_dt and @end_dt;
    """)

    (tmp_path / "models" / "weekly_model.sql").write_text("""
    MODEL (
      name sqlmesh_example.weekly_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column start_dt
      ),
      start '2020-01-01',
      cron '@weekly'
    );

    select start_dt, end_dt from sqlmesh_example.daily_model where start_dt between @start_dt and @end_dt;
    """)

    context.load()

    # create a dev env for "1 day ago" with min_intervals=1
    # this should force a weeks worth of intervals for every model
    plan = context.plan(
        environment="pr_env",
        start="1 day ago",
        execution_time=current_time,
        min_intervals=1,
    )

    def _get_missing_intervals(name: str) -> t.List[t.Tuple[datetime, datetime]]:
        snapshot_id = context.get_snapshot(name, raise_if_missing=True).snapshot_id
        snapshot_intervals = next(
            si for si in plan.missing_intervals if si.snapshot_id == snapshot_id
        )
        return [(to_datetime(s), to_datetime(e)) for s, e in snapshot_intervals.merged_intervals]

    # We only operate on completed intervals, so given the current_time this is the range of the last completed week
    _get_missing_intervals("sqlmesh_example.weekly_model") == [
        (to_datetime("2020-01-19 00:00:00"), to_datetime("2020-01-26 00:00:00"))
    ]

    # The daily model needs to cover the week, so it gets its start date moved back to line up
    _get_missing_intervals("sqlmesh_example.daily_model") == [
        (to_datetime("2020-01-19 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]

    # The hourly model needs to cover both the daily model and the weekly model, so it also gets its start date moved back to line up with the weekly model
    assert _get_missing_intervals("sqlmesh_example.hourly_model") == [
        (to_datetime("2020-01-19 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]

    # The two-hourly model only needs to cover 2 hours and should be unaffected by the fact its sibling node has a weekly child node
    # However it still gets backfilled for 24 hours because the plan start is 1 day and this satisfies min_intervals: 1
    assert _get_missing_intervals("sqlmesh_example.two_hourly_model") == [
        (to_datetime("2020-01-31 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]

    # The unrelated model has no upstream constraints, so its start date doesnt get moved to line up with the weekly model
    # However it still gets backfilled for 24 hours because the plan start is 1 day and this satisfies min_intervals: 1
    _get_missing_intervals("sqlmesh_example.unrelated_monthly_model") == [
        (to_datetime("2020-01-01 00:00:00"), to_datetime("2020-02-01 00:00:00"))
    ]

    # Check that actually running the plan produces the correct result, since missing intervals are re-calculated in the evaluator
    context.apply(plan)

    assert context.engine_adapter.fetchall(
        "select min(start_dt), max(end_dt) from sqlmesh_example__pr_env.weekly_model"
    ) == [(to_datetime("2020-01-19 00:00:00"), to_datetime("2020-01-25 23:59:59.999999"))]

    assert context.engine_adapter.fetchall(
        "select min(start_dt), max(end_dt) from sqlmesh_example__pr_env.daily_model"
    ) == [(to_datetime("2020-01-19 00:00:00"), to_datetime("2020-01-31 23:59:59.999999"))]

    assert context.engine_adapter.fetchall(
        "select min(start_dt), max(end_dt) from sqlmesh_example__pr_env.hourly_model"
    ) == [(to_datetime("2020-01-19 00:00:00"), to_datetime("2020-01-31 23:59:59.999999"))]

    assert context.engine_adapter.fetchall(
        "select min(start_dt), max(end_dt) from sqlmesh_example__pr_env.two_hourly_model"
    ) == [(to_datetime("2020-01-31 00:00:00"), to_datetime("2020-01-31 23:59:59.999999"))]

    assert context.engine_adapter.fetchall(
        "select min(start_dt), max(end_dt) from sqlmesh_example__pr_env.unrelated_monthly_model"
    ) == [(to_datetime("2020-01-01 00:00:00"), to_datetime("2020-01-31 23:59:59.999999"))]


def test_defaults_pre_post_statements(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    models_path = tmp_path / "models"
    models_path.mkdir()

    # Create config with default statements
    config_path.write_text(
        """
model_defaults:
    dialect: duckdb
    pre_statements:
        - SET memory_limit = '10GB'
        - SET threads = @var1
    post_statements:
        - ANALYZE @this_model
variables:
    var1: 4
"""
    )

    # Create a model
    model_path = models_path / "test_model.sql"
    model_path.write_text(
        """
MODEL (
    name test_model,
    kind FULL
);

SELECT 1 as id, 'test' as status;
"""
    )

    ctx = Context(paths=[tmp_path])

    # Initial plan and apply
    initial_plan = ctx.plan(auto_apply=True, no_prompts=True)
    assert len(initial_plan.new_snapshots) == 1

    snapshot = list(initial_plan.new_snapshots)[0]
    model = snapshot.model

    # Verify statements are in the model and python environment has been popuplated
    assert len(model.pre_statements) == 2
    assert len(model.post_statements) == 1
    assert model.python_env[c.SQLMESH_VARS].payload == "{'var1': 4}"

    # Verify the statements contain the expected SQL
    assert model.pre_statements[0].sql() == "SET memory_limit = '10GB'"
    assert model.render_pre_statements()[0].sql() == "SET \"memory_limit\" = '10GB'"
    assert model.pre_statements[1].sql() == "SET threads = @var1"
    assert model.render_pre_statements()[1].sql() == 'SET "threads" = 4'

    # Update config to change pre_statement
    config_path.write_text(
        """
model_defaults:
    dialect: duckdb
    pre_statements:
        - SET memory_limit = '5GB'  # Changed value
    post_statements:
        - ANALYZE @this_model
"""
    )

    # Reload context and create new plan
    ctx = Context(paths=[tmp_path])
    updated_plan = ctx.plan(no_prompts=True)

    # Should detect a change due to different pre_statements
    assert len(updated_plan.directly_modified) == 1

    # Apply the plan
    ctx.apply(updated_plan)

    # Reload the models to get the updated version
    ctx.load()
    new_model = ctx.models['"test_model"']

    # Verify updated statements
    assert len(new_model.pre_statements) == 1
    assert new_model.pre_statements[0].sql() == "SET memory_limit = '5GB'"
    assert new_model.render_pre_statements()[0].sql() == "SET \"memory_limit\" = '5GB'"

    # Verify the change was detected by the plan
    assert len(updated_plan.directly_modified) == 1


def test_model_defaults_statements_with_on_virtual_update(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    models_path = tmp_path / "models"
    models_path.mkdir()

    # Create config with on_virtual_update
    config_path.write_text(
        """
model_defaults:
    dialect: duckdb
    on_virtual_update:
        - SELECT  'Model-defailt virtual update' AS message
"""
    )

    # Create a model with its own on_virtual_update as wel
    model_path = models_path / "test_model.sql"
    model_path.write_text(
        """
MODEL (
    name test_model,
    kind FULL
);

SELECT 1 as id, 'test' as name;

ON_VIRTUAL_UPDATE_BEGIN;
SELECT 'Model-specific update' AS message;
ON_VIRTUAL_UPDATE_END;
"""
    )

    ctx = Context(paths=[tmp_path])

    # Plan and apply
    plan = ctx.plan(auto_apply=True, no_prompts=True)

    snapshot = list(plan.new_snapshots)[0]
    model = snapshot.model

    # Verify both default and model-specific on_virtual_update statements
    assert len(model.on_virtual_update) == 2

    # Default statements should come first
    assert model.on_virtual_update[0].sql() == "SELECT 'Model-defailt virtual update' AS message"
    assert model.on_virtual_update[1].sql() == "SELECT 'Model-specific update' AS message"


def test_uppercase_gateway_external_models(tmp_path):
    # Create a temporary SQLMesh project with uppercase gateway name
    config_py = tmp_path / "config.py"
    config_py.write_text("""
from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig

config = Config(
    gateways={
        "UPPERCASE_GATEWAY": GatewayConfig(
            connection=DuckDBConnectionConfig(),
        ),
    },
    default_gateway="UPPERCASE_GATEWAY",
    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
)
""")

    # Create external models file with lowercase gateway name (this should still match uppercase)
    external_models_yaml = tmp_path / "external_models.yaml"
    external_models_yaml.write_text("""
- name: test_db.uppercase_gateway_table
  description: Test external model with lowercase gateway name that should match uppercase gateway
  gateway: uppercase_gateway  # lowercase in external model, but config has UPPERCASE_GATEWAY
  columns:
    id: int
    name: text

- name: test_db.no_gateway_table
  description: Test external model without gateway (should be available for all gateways)
  columns:
    id: int
    name: text
""")

    # Create a model that references the external model
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    model_sql = models_dir / "test_model.sql"
    model_sql.write_text("""
MODEL (
    name test.my_model,
    kind FULL,
);

SELECT * FROM test_db.uppercase_gateway_table;
""")

    # Test with uppercase gateway name - this should find both models
    context_uppercase = Context(paths=[tmp_path], gateway="UPPERCASE_GATEWAY")

    # Verify external model with lowercase gateway name in YAML is found when using uppercase gateway
    gateway_specific_models = [
        model
        for model in context_uppercase.models.values()
        if model.name == "test_db.uppercase_gateway_table"
    ]
    assert len(gateway_specific_models) == 1, (
        f"External model with lowercase gateway name should be found with uppercase gateway. Found {len(gateway_specific_models)} models"
    )

    # Verify external model without gateway is also found
    no_gateway_models = [
        model
        for model in context_uppercase.models.values()
        if model.name == "test_db.no_gateway_table"
    ]
    assert len(no_gateway_models) == 1, (
        f"External model without gateway should be found. Found {len(no_gateway_models)} models"
    )

    # Check that the column types are properly loaded (not UNKNOWN)
    external_model = gateway_specific_models[0]
    column_types = {name: str(dtype) for name, dtype in external_model.columns_to_types.items()}
    assert column_types == {
        "id": "INT",
        "name": "TEXT",
    }, f"External model column types should not be UNKNOWN, got: {column_types}"

    # Test that when using a different case for the gateway parameter, we get the same results
    context_mixed_case = Context(
        paths=[tmp_path], gateway="uppercase_gateway"
    )  # lowercase parameter

    gateway_specific_models_mixed = [
        model
        for model in context_mixed_case.models.values()
        if model.name == "test_db.uppercase_gateway_table"
    ]
    # This should work but might fail if case sensitivity is not handled correctly
    assert len(gateway_specific_models_mixed) == 1, (
        f"External model should be found regardless of gateway parameter case. Found {len(gateway_specific_models_mixed)} models"
    )

    # Test a case that should demonstrate the potential issue:
    # Create another external model file with uppercase gateway name in the YAML
    external_models_yaml_uppercase = tmp_path / "external_models_uppercase.yaml"
    external_models_yaml_uppercase.write_text("""
- name: test_db.uppercase_in_yaml
  description: Test external model with uppercase gateway name in YAML
  gateway: UPPERCASE_GATEWAY  # uppercase in external model yaml
  columns:
    id: int
    status: text
""")

    # Add the new external models file to the project
    models_dir = tmp_path / "external_models"
    models_dir.mkdir(exist_ok=True)
    (models_dir / "uppercase_gateway_models.yaml").write_text("""
- name: test_db.uppercase_in_yaml
  description: Test external model with uppercase gateway name in YAML
  gateway: UPPERCASE_GATEWAY  # uppercase in external model yaml
  columns:
    id: int
    status: text
""")

    # Reload context to pick up the new external models
    context_reloaded = Context(paths=[tmp_path], gateway="UPPERCASE_GATEWAY")

    uppercase_in_yaml_models = [
        model
        for model in context_reloaded.models.values()
        if model.name == "test_db.uppercase_in_yaml"
    ]
    assert len(uppercase_in_yaml_models) == 1, (
        f"External model with uppercase gateway in YAML should be found. Found {len(uppercase_in_yaml_models)} models"
    )


def test_plan_no_start_configured():
    context = Context(config=Config())
    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.xvg,
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column ds
                    ),
                    cron '@daily'
                );

                SELECT id, ds FROM (VALUES
                    ('1', '2020-01-01'),
                ) data(id, ds)
                WHERE ds BETWEEN @start_ds AND @end_ds
                """
            )
        )
    )

    prod_plan = context.plan(auto_apply=True)
    assert len(prod_plan.new_snapshots) == 1

    context.upsert_model(
        load_sql_based_model(
            parse(
                """
                MODEL(
                    name db.xvg,
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column ds
                    ),
                    cron '@daily',
                    physical_properties ('some_prop' = 1),
                );

                SELECT id, ds FROM (VALUES
                    ('1', '2020-01-01'),
                ) data(id, ds)
                WHERE ds BETWEEN @start_ds AND @end_ds
                """
            )
        )
    )

    # This should raise an error because the model has no start configured and the end time is less than the start time which will be calculated from the intervals
    with pytest.raises(
        PlanError,
        match=r"Model '.*xvg.*': Start date / time .* can't be greater than end date / time .*\.\nSet the `start` attribute in your project config model defaults to avoid this issue",
    ):
        context.plan("dev", execution_time="1999-01-05")


def test_lint_model_projections(tmp_path: Path):
    init_example_project(tmp_path, engine_type="duckdb", dialect="duckdb")

    context = Context(paths=tmp_path)
    context.upsert_model(
        load_sql_based_model(
            parse("""MODEL(name sqlmesh_example.m); SELECT 1 AS x, 2 AS x"""),
            default_catalog="db",
        )
    )

    config_err = "Linter detected errors in the code. Please fix them before proceeding."

    with pytest.raises(LinterError, match=config_err):
        prod_plan = context.plan(no_prompts=True, auto_apply=True)


def test_grants_through_plan_apply(sushi_context, mocker):
    from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter
    from sqlmesh.core.model.meta import GrantsTargetLayer

    model = sushi_context.get_model("sushi.waiter_revenue_by_day")

    mocker.patch.object(DuckDBEngineAdapter, "SUPPORTS_GRANTS", True)
    sync_grants_mock = mocker.patch.object(DuckDBEngineAdapter, "sync_grants_config")

    model_with_grants = model.copy(
        update={
            "grants": {"select": ["analyst", "reporter"]},
            "grants_target_layer": GrantsTargetLayer.ALL,
        }
    )
    sushi_context.upsert_model(model_with_grants)

    sushi_context.plan("dev", no_prompts=True, auto_apply=True)

    # When planning for dev env w/ metadata only changes,
    # only virtual layer is updated, so no physical grants are applied
    assert sync_grants_mock.call_count == 1
    assert all(
        call[0][1] == {"select": ["analyst", "reporter"]}
        for call in sync_grants_mock.call_args_list
    )

    sync_grants_mock.reset_mock()

    new_grants = ({"select": ["analyst", "reporter", "manager"], "insert": ["etl_user"]},)
    model_updated = model_with_grants.copy(
        update={
            "query": parse_one(model.query.sql() + " LIMIT 1000"),
            "grants": new_grants,
            # force model update, hence new physical table creation
            "stamp": "update model and grants",
        }
    )
    sushi_context.upsert_model(model_updated)
    sushi_context.plan("dev", no_prompts=True, auto_apply=True)

    # Applies grants 2 times: 1 x physical, 1 x virtual
    assert sync_grants_mock.call_count == 2
    assert all(call[0][1] == new_grants for call in sync_grants_mock.call_args_list)

    sync_grants_mock.reset_mock()

    # plan for prod
    sushi_context.plan(no_prompts=True, auto_apply=True)
    assert sync_grants_mock.call_count == 2
