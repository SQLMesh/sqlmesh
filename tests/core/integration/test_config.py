from __future__ import annotations

import typing as t
from unittest.mock import patch
import logging
import pytest
from pytest import MonkeyPatch
from pathlib import Path
from pytest_mock.plugin import MockerFixture
from sqlglot import exp
from IPython.utils.capture import capture_output

from sqlmesh.core.config import (
    Config,
    GatewayConfig,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
    OwnershipConfig,
    TableNamingConvention,
    AutoCategorizationMode,
)
from sqlmesh.core.config.common import EnvironmentSuffixTarget
from sqlmesh.core.context import Context
from sqlmesh.core.config.plan import PlanConfig
from sqlmesh.core.engine_adapter import DuckDBEngineAdapter
from sqlmesh.core.model import SqlModel
from sqlmesh.core.model.common import ParsableSql
from sqlmesh.core.snapshot import (
    SnapshotChangeCategory,
)
from sqlmesh.utils.errors import (
    ConfigError,
)
from tests.conftest import DuckDBMetadata
from tests.utils.test_helpers import use_terminal_console
from tests.utils.test_filesystem import create_temp_file
from tests.core.integration.utils import apply_to_environment, initial_add

pytestmark = pytest.mark.slow


@pytest.mark.set_default_connection(disable=True)
def test_missing_connection_config():
    # This is testing the actual implementation of Config.get_connection
    # To make writing tests easier, it's patched by the autouse fixture provide_sqlmesh_default_connection
    # Case 1: No default_connection or gateways specified should raise a ConfigError
    with pytest.raises(ConfigError):
        ctx = Context(config=Config())

    # Case 2: No connection specified in the gateway should raise a ConfigError
    with pytest.raises(ConfigError):
        ctx = Context(config=Config(gateways={"incorrect": GatewayConfig()}))

    # Case 3: Specifying a default_connection or connection in the gateway should work
    ctx = Context(config=Config(default_connection=DuckDBConnectionConfig()))
    ctx = Context(
        config=Config(gateways={"default": GatewayConfig(connection=DuckDBConnectionConfig())})
    )


def test_physical_table_naming_strategy_table_only(copy_to_temp_path: t.Callable):
    sushi_context = Context(
        paths=copy_to_temp_path("examples/sushi"),
        config="table_only_naming_config",
    )

    assert sushi_context.config.physical_table_naming_convention == TableNamingConvention.TABLE_ONLY
    sushi_context.plan(auto_apply=True)

    adapter = sushi_context.engine_adapter

    snapshot_tables = [
        dict(catalog=str(r[0]), schema=str(r[1]), table=str(r[2]))
        for r in adapter.fetchall(
            "select table_catalog, table_schema, table_name from information_schema.tables where table_type='BASE TABLE'"
        )
    ]

    assert all([not t["table"].startswith("sushi") for t in snapshot_tables])

    prod_env = sushi_context.state_reader.get_environment("prod")
    assert prod_env

    prod_env_snapshots = sushi_context.state_reader.get_snapshots(prod_env.snapshots)

    assert all(
        s.table_naming_convention == TableNamingConvention.TABLE_ONLY
        for s in prod_env_snapshots.values()
    )


def test_physical_table_naming_strategy_hash_md5(copy_to_temp_path: t.Callable):
    sushi_context = Context(
        paths=copy_to_temp_path("examples/sushi"),
        config="hash_md5_naming_config",
    )

    assert sushi_context.config.physical_table_naming_convention == TableNamingConvention.HASH_MD5
    sushi_context.plan(auto_apply=True)

    adapter = sushi_context.engine_adapter

    snapshot_tables = [
        dict(catalog=str(r[0]), schema=str(r[1]), table=str(r[2]))
        for r in adapter.fetchall(
            "select table_catalog, table_schema, table_name from information_schema.tables where table_type='BASE TABLE'"
        )
    ]

    assert all([not t["table"].startswith("sushi") for t in snapshot_tables])
    assert all([t["table"].startswith("sqlmesh_md5") for t in snapshot_tables])

    prod_env = sushi_context.state_reader.get_environment("prod")
    assert prod_env

    prod_env_snapshots = sushi_context.state_reader.get_snapshots(prod_env.snapshots)

    assert all(
        s.table_naming_convention == TableNamingConvention.HASH_MD5
        for s in prod_env_snapshots.values()
    )


def test_environment_suffix_target_table(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context(
        "examples/sushi", config="environment_suffix_table_config"
    )
    context.apply(plan)
    metadata = DuckDBMetadata.from_context(context)
    environments_schemas = {"sushi"}
    internal_schemas = {"sqlmesh", "sqlmesh__sushi"}
    starting_schemas = environments_schemas | internal_schemas
    # Make sure no new schemas are created
    assert set(metadata.schemas) - starting_schemas == {"raw"}
    prod_views = {x for x in metadata.qualified_views if x.db in environments_schemas}
    # Make sure that all models are present
    assert len(prod_views) == 16
    apply_to_environment(context, "dev")
    # Make sure no new schemas are created
    assert set(metadata.schemas) - starting_schemas == {"raw"}
    dev_views = {
        x for x in metadata.qualified_views if x.db in environments_schemas and "__dev" in x.name
    }
    # Make sure that there is a view with `__dev` for each view that exists in prod
    assert len(dev_views) == len(prod_views)
    assert {x.name.replace("__dev", "") for x in dev_views} - {x.name for x in prod_views} == set()
    context.invalidate_environment("dev")
    context._run_janitor()
    views_after_janitor = metadata.qualified_views
    # Make sure that the number of views after the janitor is the same as when you subtract away dev views
    assert len(views_after_janitor) == len(
        {x.sql(dialect="duckdb") for x in views_after_janitor}
        - {x.sql(dialect="duckdb") for x in dev_views}
    )
    # Double check there are no dev views
    assert len({x for x in views_after_janitor if "__dev" in x.name}) == 0
    # Make sure prod views were not removed
    assert {x.sql(dialect="duckdb") for x in prod_views} - {
        x.sql(dialect="duckdb") for x in views_after_janitor
    } == set()


def test_environment_suffix_target_catalog(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(catalogs={"main_warehouse": ":memory:"}),
        environment_suffix_target=EnvironmentSuffixTarget.CATALOG,
    )

    assert config.default_connection

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    (models_dir / "model.sql").write_text("""
    MODEL (
        name example_schema.test_model,
        kind FULL
    );

    SELECT '1' as a""")

    (models_dir / "fqn_model.sql").write_text("""
    MODEL (
        name memory.example_fqn_schema.test_model_fqn,
        kind FULL
    );

    SELECT '1' as a""")

    ctx = Context(config=config, paths=tmp_path)

    metadata = DuckDBMetadata.from_context(ctx)
    assert ctx.default_catalog == "main_warehouse"
    assert metadata.catalogs == {"main_warehouse", "memory"}

    ctx.plan(auto_apply=True)

    # prod should go to the default catalog and not be overridden to a catalog called 'prod'
    assert (
        ctx.engine_adapter.fetchone("select * from main_warehouse.example_schema.test_model")[0]  # type: ignore
        == "1"
    )
    assert (
        ctx.engine_adapter.fetchone("select * from memory.example_fqn_schema.test_model_fqn")[0]  # type: ignore
        == "1"
    )
    assert metadata.catalogs == {"main_warehouse", "memory"}
    assert metadata.schemas_in_catalog("main_warehouse") == [
        "example_schema",
        "sqlmesh__example_schema",
    ]
    assert metadata.schemas_in_catalog("memory") == [
        "example_fqn_schema",
        "sqlmesh__example_fqn_schema",
    ]

    # dev should be overridden to go to a catalogs called 'main_warehouse__dev' and 'memory__dev'
    ctx.plan(environment="dev", include_unmodified=True, auto_apply=True)
    assert (
        ctx.engine_adapter.fetchone("select * from main_warehouse__dev.example_schema.test_model")[
            0
        ]  # type: ignore
        == "1"
    )
    assert (
        ctx.engine_adapter.fetchone("select * from memory__dev.example_fqn_schema.test_model_fqn")[
            0
        ]  # type: ignore
        == "1"
    )
    assert metadata.catalogs == {"main_warehouse", "main_warehouse__dev", "memory", "memory__dev"}

    # schemas in dev envs should match prod and not have a suffix
    assert metadata.schemas_in_catalog("main_warehouse") == [
        "example_schema",
        "sqlmesh__example_schema",
    ]
    assert metadata.schemas_in_catalog("main_warehouse__dev") == ["example_schema"]
    assert metadata.schemas_in_catalog("memory") == [
        "example_fqn_schema",
        "sqlmesh__example_fqn_schema",
    ]
    assert metadata.schemas_in_catalog("memory__dev") == ["example_fqn_schema"]

    ctx.invalidate_environment("dev", sync=True)

    # dev catalogs cleaned up
    assert metadata.catalogs == {"main_warehouse", "memory"}

    # prod catalogs still contain physical layer and views still work
    assert metadata.schemas_in_catalog("main_warehouse") == [
        "example_schema",
        "sqlmesh__example_schema",
    ]
    assert metadata.schemas_in_catalog("memory") == [
        "example_fqn_schema",
        "sqlmesh__example_fqn_schema",
    ]

    assert (
        ctx.engine_adapter.fetchone("select * from main_warehouse.example_schema.test_model")[0]  # type: ignore
        == "1"
    )
    assert (
        ctx.engine_adapter.fetchone("select * from memory.example_fqn_schema.test_model_fqn")[0]  # type: ignore
        == "1"
    )


def test_environment_catalog_mapping(init_and_plan_context: t.Callable):
    environments_schemas = {"raw", "sushi"}

    def get_prod_dev_views(metadata: DuckDBMetadata) -> t.Tuple[t.Set[exp.Table], t.Set[exp.Table]]:
        views = metadata.qualified_views
        prod_views = {
            x for x in views if x.catalog == "prod_catalog" if x.db in environments_schemas
        }
        dev_views = {x for x in views if x.catalog == "dev_catalog" if x.db in environments_schemas}
        return prod_views, dev_views

    def get_default_catalog_and_non_tables(
        metadata: DuckDBMetadata, default_catalog: t.Optional[str]
    ) -> t.Tuple[t.Set[exp.Table], t.Set[exp.Table]]:
        tables = metadata.qualified_tables
        user_default_tables = {
            x for x in tables if x.catalog == default_catalog and x.db != "sqlmesh"
        }
        non_default_tables = {x for x in tables if x.catalog != default_catalog}
        return user_default_tables, non_default_tables

    context, plan = init_and_plan_context(
        "examples/sushi", config="environment_catalog_mapping_config"
    )
    context.apply(plan)
    metadata = DuckDBMetadata(context.engine_adapter)
    state_metadata = DuckDBMetadata.from_context(context.state_sync.state_sync)
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 16
    assert len(dev_views) == 0
    assert len(user_default_tables) == 15
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )
    apply_to_environment(context, "dev")
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 16
    assert len(dev_views) == 16
    assert len(user_default_tables) == 16
    assert len(non_default_tables) == 0
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )
    apply_to_environment(context, "prodnot")
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 16
    assert len(dev_views) == 32
    assert len(user_default_tables) == 16
    assert len(non_default_tables) == 0
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )
    context.invalidate_environment("dev")
    context._run_janitor()
    prod_views, dev_views = get_prod_dev_views(metadata)
    (
        user_default_tables,
        non_default_tables,
    ) = get_default_catalog_and_non_tables(metadata, context.default_catalog)
    assert len(prod_views) == 16
    assert len(dev_views) == 16
    assert len(user_default_tables) == 16
    assert len(non_default_tables) == 0
    assert state_metadata.schemas == ["sqlmesh"]
    assert {x.sql() for x in state_metadata.qualified_tables}.issuperset(
        {
            "physical.sqlmesh._environments",
            "physical.sqlmesh._intervals",
            "physical.sqlmesh._snapshots",
            "physical.sqlmesh._versions",
        }
    )


@use_terminal_console
def test_plan_always_recreate_environment(tmp_path: Path):
    def plan_with_output(ctx: Context, environment: str):
        with patch.object(logger, "info") as mock_logger:
            with capture_output() as output:
                ctx.load()
                ctx.plan(environment, no_prompts=True, auto_apply=True)

            # Facade logs info "Promoting environment {environment}"
            assert mock_logger.call_args[0][1] == environment

        return output

    models_dir = tmp_path / "models"

    logger = logging.getLogger("sqlmesh.core.state_sync.db.facade")

    create_temp_file(
        tmp_path, models_dir / "a.sql", "MODEL (name test.a, kind FULL); SELECT 1 AS col"
    )

    config = Config(plan=PlanConfig(always_recreate_environment=True))
    ctx = Context(paths=[tmp_path], config=config)

    # Case 1: Neither prod nor dev exists, so dev is initialized
    output = plan_with_output(ctx, "dev")

    assert """`dev` environment will be initialized""" in output.stdout

    # Case 2: Prod does not exist, so dev is updated
    create_temp_file(
        tmp_path, models_dir / "a.sql", "MODEL (name test.a, kind FULL); SELECT 5 AS col"
    )

    output = plan_with_output(ctx, "dev")
    assert "`dev` environment will be initialized" in output.stdout

    # Case 3: Prod is initialized, so plan comparisons moving forward should be against prod
    output = plan_with_output(ctx, "prod")
    assert "`prod` environment will be initialized" in output.stdout

    # Case 4: Dev is updated with a breaking change. Prod exists now so plan comparisons moving forward should be against prod
    create_temp_file(
        tmp_path, models_dir / "a.sql", "MODEL (name test.a, kind FULL); SELECT 10 AS col"
    )
    ctx.load()

    plan = ctx.plan_builder("dev").build()

    assert (
        next(iter(plan.context_diff.snapshots.values())).change_category
        == SnapshotChangeCategory.BREAKING
    )

    output = plan_with_output(ctx, "dev")
    assert "New environment `dev` will be created from `prod`" in output.stdout
    assert "Differences from the `prod` environment" in output.stdout

    # Case 5: Dev is updated with a metadata change, but comparison against prod shows both the previous and the current changes
    # so it's still classified as a breaking change
    create_temp_file(
        tmp_path,
        models_dir / "a.sql",
        "MODEL (name test.a, kind FULL, owner 'test'); SELECT 10 AS col",
    )
    ctx.load()

    plan = ctx.plan_builder("dev").build()

    assert (
        next(iter(plan.context_diff.snapshots.values())).change_category
        == SnapshotChangeCategory.BREAKING
    )

    output = plan_with_output(ctx, "dev")
    assert "New environment `dev` will be created from `prod`" in output.stdout
    assert "Differences from the `prod` environment" in output.stdout

    stdout_rstrip = "\n".join([line.rstrip() for line in output.stdout.split("\n")])
    assert (
        """MODEL (
   name test.a,
+  owner test,
   kind FULL
 )
 SELECT
-  5 AS col
+  10 AS col"""
        in stdout_rstrip
    )

    # Case 6: Ensure that target environment and create_from environment are not the same
    output = plan_with_output(ctx, "prod")
    assert not "New environment `prod` will be created from `prod`" in output.stdout

    # Case 7: Check that we can still run Context::diff() against any environment
    for environment in ["dev", "prod"]:
        context_diff = ctx._context_diff(environment)
        assert context_diff.environment == environment


def test_before_all_after_all_execution_order(tmp_path: Path, mocker: MockerFixture):
    model = """
    MODEL (
        name test_schema.model_that_depends_on_before_all,
        kind FULL,
    );

    SELECT id, value FROM before_all_created_table
    """

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    with open(models_dir / "model.sql", "w") as f:
        f.write(model)

    # before_all statement that creates a table that the above model depends on
    before_all_statement = (
        "CREATE TABLE IF NOT EXISTS before_all_created_table AS SELECT 1 AS id, 'test' AS value"
    )

    # after_all that depends on the model
    after_all_statement = "CREATE TABLE IF NOT EXISTS after_all_created_table AS SELECT id, value FROM test_schema.model_that_depends_on_before_all"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        before_all=[before_all_statement],
        after_all=[after_all_statement],
    )

    execute_calls: t.List[str] = []

    original_duckdb_execute = DuckDBEngineAdapter.execute

    def track_duckdb_execute(self, expression, **kwargs):
        sql = expression if isinstance(expression, str) else expression.sql(dialect="duckdb")
        state_tables = [
            "_snapshots",
            "_environments",
            "_versions",
            "_intervals",
            "_auto_restatements",
            "_environment_statements",
        ]

        # to ignore the state queries
        if not any(table in sql.lower() for table in state_tables):
            execute_calls.append(sql)

        return original_duckdb_execute(self, expression, **kwargs)

    ctx = Context(paths=[tmp_path], config=config)

    # the plan would fail if the execution order ever changes and before_all statements dont execute first
    ctx.plan(auto_apply=True, no_prompts=True)

    mocker.patch.object(DuckDBEngineAdapter, "execute", track_duckdb_execute)

    # run with the patched execute
    ctx.run("prod", start="2023-01-01", end="2023-01-02")

    # validate explicitly that the first execute is for the before_all
    assert "before_all_created_table" in execute_calls[0]

    # and that the last is the sole after all that depends on the model
    assert "after_all_created_table" in execute_calls[-1]


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


def test_ownership_config_plan_applies_without_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """OwnershipConfig flows through the full plan/apply lifecycle without errors.

    DuckDB's alter_schema_owner/alter_view_owner are no-ops, so we cannot verify
    that ownership was actually changed — but we confirm the config plumbing
    doesn't break schema creation, view promotion, or dev environment application.
    """
    monkeypatch.chdir(tmp_path)

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(),
        ownership=OwnershipConfig(
            environment_owner_mapping={
                "^prod$": "svc_prod_owner",
                ".*": "group:shared-developers",
            }
        ),
    )

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "model.sql").write_text(
        """
        MODEL (name example_schema.test_model, kind FULL);
        SELECT '1' AS a
        """
    )

    ctx = Context(config=config, paths=tmp_path)

    # Prod plan/apply — exercises virtual layer schema and view creation with ownership config
    ctx.plan(auto_apply=True)
    assert ctx.engine_adapter.table_exists("example_schema.test_model")

    # Dev plan/apply — exercises env-suffixed schema and view creation with ownership config
    ctx.plan(environment="dev", include_unmodified=True, auto_apply=True)
    metadata = DuckDBMetadata.from_context(ctx)
    dev_schemas = {s for s in metadata.schemas if "__dev" in s}
    assert len(dev_schemas) > 0
