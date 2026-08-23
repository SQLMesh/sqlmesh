import base64
import typing as t
from pathlib import Path
from shutil import copytree

import pytest
from dbt.adapters.base import BaseRelation, Column
from pytest_mock import MockerFixture

from sqlglot import exp

from sqlmesh import Context
from sqlmesh.core.audit import StandaloneAudit
from sqlmesh.core.config import Config, ModelDefaultsConfig
from sqlmesh.core.dialect import jinja_query
from sqlmesh.core.model import SqlModel
from sqlmesh.core.model.kind import OnDestructiveChange, OnAdditiveChange
from sqlmesh.core.state_sync import CachingStateSync, EngineAdapterStateSync
from sqlmesh.dbt.builtin import Api
from sqlmesh.dbt.column import ColumnConfig
from sqlmesh.dbt.common import Dependencies
from sqlmesh.dbt.context import DbtContext
from sqlmesh.dbt.loader import sqlmesh_config
from sqlmesh.dbt.model import (
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    Materialization,
    ModelConfig,
)
from sqlmesh.dbt.project import Project
from sqlmesh.dbt.relation import Policy
from sqlmesh.dbt.source import SourceConfig
from sqlmesh.dbt.target import (
    TARGET_TYPE_TO_CONFIG_CLASS,
    BigQueryConfig,
    DatabricksConfig,
    DuckDbConfig,
    MSSQLConfig,
    PostgresConfig,
    RedshiftConfig,
    SnowflakeConfig,
    TargetConfig,
    TrinoConfig,
    AthenaConfig,
    ClickhouseConfig,
    SCHEMA_DIFFER_OVERRIDES,
)
from sqlmesh.dbt.test import TestConfig
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.yaml import load as yaml_load, dump as yaml_dump
from tests.dbt.conftest import EmptyProjectCreator

pytestmark = pytest.mark.dbt


@pytest.mark.parametrize(
    "current, new, expected",
    [
        ({}, {"alias": "correct name"}, {"alias": "correct name"}),
        ({"alias": "correct name"}, {}, {"alias": "correct name"}),
        (
            {"alias": "wrong name"},
            {"alias": "correct name"},
            {"alias": "correct name"},
        ),
        ({}, {"tags": ["two"]}, {"tags": ["two"]}),
        ({"tags": ["one"]}, {}, {"tags": ["one"]}),
        ({"tags": ["one"]}, {"tags": ["two"]}, {"tags": ["one", "two"]}),
        ({"tags": "one"}, {"tags": "two"}, {"tags": ["one", "two"]}),
        ({}, {"meta": {"owner": "jen"}}, {"meta": {"owner": "jen"}}),
        ({"meta": {"owner": "jen"}}, {}, {"meta": {"owner": "jen"}}),
        (
            {"meta": {"owner": "bob"}},
            {"meta": {"owner": "jen"}},
            {"meta": {"owner": "jen"}},
        ),
        ({}, {"grants": {"select": ["bob"]}}, {"grants": {"select": ["bob"]}}),
        ({"grants": {"select": ["bob"]}}, {}, {"grants": {"select": ["bob"]}}),
        (
            {"grants": {"select": ["bob"]}},
            {"grants": {"select": ["jen"]}},
            {"grants": {"select": ["bob", "jen"]}},
        ),
        ({"uknown": "field"}, {"uknown": "value"}, {"uknown": "value"}),
        ({"uknown": "field"}, {}, {"uknown": "field"}),
        ({}, {"uknown": "value"}, {"uknown": "value"}),
    ],
)
def test_update(current: t.Dict[str, t.Any], new: t.Dict[str, t.Any], expected: t.Dict[str, t.Any]):
    config = ModelConfig(**current).update_with(new)
    assert {k: v for k, v in config.dict().items() if k in expected} == expected


def test_model_to_sqlmesh_fields(dbt_dummy_postgres_config: PostgresConfig):
    model_config = ModelConfig(
        unique_id="model.package.name",
        name="name",
        package_name="package",
        fqn=["package", "name"],
        alias="model",
        schema="custom",
        database="database",
        materialized=Materialization.INCREMENTAL,
        description="test model",
        sql="SELECT 1 AS a FROM foo.table",
        start="Jan 1 2023",
        partition_by=["a"],
        cluster_by=["a", '"b"'],
        incremental_predicates=[
            "55 > DBT_INTERNAL_SOURCE.b",
            "DBT_INTERNAL_DEST.session_start > date_add(current_date, interval 7 day)",
        ],
        cron="@hourly",
        interval_unit="FIVE_MINUTE",
        batch_size=5,
        lookback=3,
        unique_key=["a"],
        meta={"stamp": "bar"},
        owner="Sally",
        tags=["test", "incremental"],
        on_schema_change="sync_all_columns",
    )
    context = DbtContext()
    context.project_name = "Foo"
    context.target = dbt_dummy_postgres_config
    model = model_config.to_sqlmesh(context)

    assert isinstance(model, SqlModel)
    assert model.name == "database.custom.model"
    assert model.dbt_unique_id == "model.package.name"
    assert model.dbt_fqn == "package.name"
    assert model.description == "test model"
    assert (
        model.render_query_or_raise().sql()
        == 'SELECT 1 AS "a" FROM "dbname"."foo"."table" AS "table"'
    )
    assert model.start == "Jan 1 2023"
    assert [col.sql() for col in model.partitioned_by] == ['"a"']
    assert [col.sql() for col in model.clustered_by] == ['"a"', '"b"']
    assert model.cron == "@hourly"
    assert model.interval_unit.value == "five_minute"
    assert model.stamp == "bar"
    assert model.dialect == "postgres"
    assert model.owner == "Sally"
    assert model.tags == ["test", "incremental"]
    assert model.allow_partials
    kind = t.cast(IncrementalByUniqueKeyKind, model.kind)
    assert kind.batch_size == 5
    assert kind.lookback == 3
    assert kind.on_destructive_change == OnDestructiveChange.ALLOW
    assert kind.on_additive_change == OnAdditiveChange.ALLOW
    assert (
        kind.merge_filter.sql(dialect=model.dialect)  # type: ignore
        == """55 > "__MERGE_SOURCE__"."b" AND "__MERGE_TARGET__"."session_start" > CURRENT_DATE + INTERVAL '7 DAY'"""
    )

    model = model_config.update_with({"dialect": "snowflake"}).to_sqlmesh(context)
    assert model.dialect == "snowflake"

    bq_default_context = DbtContext(
        sqlmesh_config=Config(model_defaults=ModelDefaultsConfig(dialect="bigquery"))
    )
    bq_default_context.project_name = "Foo"
    bq_default_context.target = dbt_dummy_postgres_config
    model_config.cluster_by = ["a", "`b`"]
    model = model_config.to_sqlmesh(bq_default_context)
    assert model.dialect == "bigquery"

    model_config = ModelConfig(
        name="name",
        package_name="package",
        alias="model",
        schema="custom",
        database="database",
        materialized=Materialization.INCREMENTAL,
        sql="SELECT * FROM foo.table",
        time_column="ds",
        start="Jan 1 2023",
        batch_size=5,
        batch_concurrency=2,
        on_schema_change="ignore",
    )
    model = model_config.to_sqlmesh(context)
    assert isinstance(model.kind, IncrementalByTimeRangeKind)
    assert model.kind.batch_concurrency == 2
    assert model.kind.time_column.column.name == "ds"
    assert model.kind.on_destructive_change == OnDestructiveChange.IGNORE
    assert model.kind.on_additive_change == OnAdditiveChange.IGNORE


def test_test_to_sqlmesh_fields():
    sql = "SELECT * FROM FOO WHERE cost > 100"
    test_config = TestConfig(
        unique_id="test.test_package.foo_test",
        name="foo_test",
        fqn=["test_package", "foo_test"],
        sql=sql,
        model_name="Foo",
        column_name="cost",
        severity="ERROR",
        enabled=True,
    )

    context = DbtContext()
    context._project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="foo")
    audit = test_config.to_sqlmesh(context)

    assert audit.name == "foo_test"
    assert audit.dbt_unique_id == "test.test_package.foo_test"
    assert audit.dbt_fqn == "test_package.foo_test"
    assert audit.dialect == "duckdb"
    assert not audit.skip
    assert audit.blocking
    assert sql in audit.query.sql()

    sql = "SELECT * FROM FOO WHERE NOT id IS NULL"
    test_config = TestConfig(
        name="foo_null_test",
        sql=sql,
        model_name="Foo",
        column_name="id",
        severity="WARN",
        enabled=False,
    )

    audit = test_config.to_sqlmesh(context)

    assert audit.name == "foo_null_test"
    assert audit.dialect == "duckdb"
    assert audit.skip
    assert not audit.blocking
    assert sql in audit.query.sql()

    test_config = TestConfig(
        name="foo_null_test",
        sql=sql,
        model_name="Foo",
        column_name="id",
        severity="WARN",
        enabled=False,
        dialect="bigquery",
    )

    audit = test_config.to_sqlmesh(context)

    assert audit.dialect == "bigquery"


def test_test_config_canonical_name():
    test_config_upper_case_package = TestConfig(
        name="foo_test",
        package_name="TEST_PACKAGE",
        sql="SELECT 1",
    )

    assert test_config_upper_case_package.canonical_name == "test_package.foo_test"

    test_config_mixed_case_package = TestConfig(
        name="Bar_Test",
        package_name="MixedCase_Package",
        sql="SELECT 1",
    )

    assert test_config_mixed_case_package.canonical_name == "mixedcase_package.bar_test"

    test_config_no_package = TestConfig(
        name="foo_bar_test",
        sql="SELECT 1",
    )

    assert test_config_no_package.canonical_name == "foo_bar_test"


def test_singular_test_to_standalone_audit(dbt_dummy_postgres_config: PostgresConfig):
    sql = "SELECT * FROM FOO.BAR WHERE cost > 100"
    test_config = TestConfig(
        name="bar_test",
        description="test description",
        owner="Sally",
        stamp="bump",
        cron="@monthly",
        interval_unit="Day",
        sql=sql,
        severity="ERROR",
        enabled=True,
        dependencies=Dependencies(refs=["bar"]),
    )

    assert test_config.is_standalone

    model = ModelConfig(schema="foo", name="bar", alias="bar")

    context = DbtContext()
    context.add_models({model.name: model})
    context._project_name = "Foo"
    context.target = dbt_dummy_postgres_config
    standalone_audit = t.cast(StandaloneAudit, test_config.to_sqlmesh(context))

    assert standalone_audit.name == "bar_test"
    assert standalone_audit.description == "test description"
    assert standalone_audit.owner == "Sally"
    assert standalone_audit.stamp == "bump"
    assert standalone_audit.cron == "@monthly"
    assert standalone_audit.interval_unit.value == "day"
    assert standalone_audit.dialect == "postgres"
    assert standalone_audit.query == jinja_query(sql)
    assert standalone_audit.depends_on == {'"dbname"."foo"."bar"'}

    test_config.dialect_ = "bigquery"
    standalone_audit = t.cast(StandaloneAudit, test_config.to_sqlmesh(context))
    assert standalone_audit.dialect == "bigquery"


@pytest.mark.slow
def test_variables(assert_exp_eq, sushi_test_project):
    # Case 1: using an undefined variable without a default value
    defined_variables = {}

    context = sushi_test_project.context
    context.variables = defined_variables

    model_config = ModelConfig(
        alias="sushi.test",
        sql="SELECT {{ var('foo') }}",
        dependencies=Dependencies(variables=["foo", "bar"]),
    )

    kwargs = {"context": context}

    # Case 2: using a defined variable without a default value
    defined_variables["empty_list_var"] = []
    defined_variables["jinja_list_var"] = ["{{ 1 + 1 }}"]
    defined_variables["bar"] = "{{ 2 * 3 }}"
    defined_variables["foo"] = "{{ var('bar') }}"
    context.set_and_render_variables(defined_variables, "test_package")
    assert context.variables == {
        "empty_list_var": [],
        "jinja_list_var": ["2"],
        "bar": "6",
        "foo": "6",
    }

    sqlmesh_model = model_config.to_sqlmesh(**kwargs)
    assert_exp_eq(sqlmesh_model.render_query(), 'SELECT 6 AS "6"')
    assert sqlmesh_model.jinja_macros.global_objs["vars"]["bar"] == "6"
    assert sqlmesh_model.jinja_macros.global_objs["vars"]["foo"] == "6"
    assert "empty_list_var" not in sqlmesh_model.jinja_macros.global_objs["vars"]
    assert "jinja_list_var" not in sqlmesh_model.jinja_macros.global_objs["vars"]

    # Case 3: using a defined variable with a default value
    model_config.sql = "SELECT {{ var('foo', 5) }}"

    assert_exp_eq(model_config.to_sqlmesh(**kwargs).render_query(), 'SELECT 6 AS "6"')

    # Case 4: using an undefined variable with a default value
    del defined_variables["foo"]
    context.variables = defined_variables

    assert_exp_eq(model_config.to_sqlmesh(**kwargs).render_query(), 'SELECT 5 AS "5"')

    # Finally, check that variable scoping & overwriting (some_var) works as expected
    expected_sushi_variables = {
        "yet_another_var": 1,
        "top_waiters:limit": "{{ get_top_waiters_limit() }}",
        "top_waiters:revenue": "revenue",
        "customers:boo": ["a", "b"],
        "nested_vars": {
            "some_nested_var": 2,
        },
        "dynamic_test_var": 3,
        "list_var": [
            {"name": "item1", "value": 1},
            {"name": "item2", "value": 2},
        ],
        "customers": {
            "customers:bla": False,
            "customers:customer_id": "customer_id",
            "some_var": ["foo", "bar"],
        },
        "some_var": "should be overridden in customers package",
        "invalid_var": "{{ ref('ref_without_closing_paren' }}",
    }
    expected_customer_variables = {
        "some_var": ["foo", "bar"],  # Takes precedence over the root project variable
        "some_other_var": 5,
        "customers:bla": False,
        "customers:customer_id": "customer_id",
        "yet_another_var": 1,  # Make sure that the project variable takes precedence
        "top_waiters:limit": "{{ get_top_waiters_limit() }}",
        "top_waiters:revenue": "revenue",
        "customers:boo": ["a", "b"],
        "nested_vars": {
            "some_nested_var": 2,
        },
        "dynamic_test_var": 3,
        "list_var": [
            {"name": "item1", "value": 1},
            {"name": "item2", "value": 2},
        ],
        "invalid_var": "{{ ref('ref_without_closing_paren' }}",
    }
    assert sushi_test_project.packages["sushi"].variables == expected_sushi_variables
    assert sushi_test_project.packages["customers"].variables == expected_customer_variables


@pytest.mark.slow
def test_variables_override(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context(
        "tests/fixtures/dbt/sushi_test", config="test_config_with_var_override"
    )
    dbt_project = context._loaders[0]._load_projects()[0]  # type: ignore
    assert dbt_project.packages["sushi"].variables["some_var"] == "overridden_from_config_py"
    assert dbt_project.packages["customers"].variables["some_var"] == "overridden_from_config_py"


@pytest.mark.slow
def test_jinja_in_dbt_variables(sushi_test_dbt_context: Context):
    assert sushi_test_dbt_context.render("sushi.top_waiters").sql().endswith("LIMIT 10")


@pytest.mark.slow
def test_nested_variables(sushi_test_project):
    model_config = ModelConfig(
        alias="sushi.test_nested",
        sql="SELECT {{ var('nested_vars')['some_nested_var'] }}",
        dependencies=Dependencies(variables=["nested_vars"]),
    )
    context = sushi_test_project.context.copy()
    context.set_and_render_variables(sushi_test_project.packages["sushi"].variables, "sushi")
    sqlmesh_model = model_config.to_sqlmesh(context)
    assert sqlmesh_model.jinja_macros.global_objs["vars"]["nested_vars"] == {"some_nested_var": 2}


@pytest.mark.slow
def test_source_config(sushi_test_project: Project):
    source_configs = sushi_test_project.packages["sushi"].sources
    assert set(source_configs) == {
        "streaming.orders",
        "parquet_file.items",
        "streaming.order_items",
        "streaming.items",
        "parquet_file.orders",
    }

    expected_config = {
        "schema_": "raw",
        "identifier": "order_items",
    }
    actual_config = {
        k: getattr(source_configs["streaming.order_items"], k) for k, v in expected_config.items()
    }
    assert actual_config == expected_config

    assert (
        source_configs["streaming.order_items"].canonical_name(sushi_test_project.context)
        == "raw.order_items"
    )

    assert (
        source_configs["parquet_file.orders"].canonical_name(sushi_test_project.context)
        == "read_parquet('path/to/external/orders.parquet')"
    )


@pytest.mark.slow
def test_seed_config(sushi_test_project: Project, mocker: MockerFixture):
    seed_configs = sushi_test_project.packages["sushi"].seeds
    assert set(seed_configs) == {"waiter_names", "waiter_revenue_semicolon"}
    raw_items_seed = seed_configs["waiter_names"]

    expected_config = {
        "path": Path(sushi_test_project.context.project_root, "seeds/waiter_names.csv"),
        "schema_": "sushi",
    }
    actual_config = {k: getattr(raw_items_seed, k) for k, v in expected_config.items()}
    assert actual_config == expected_config

    context = sushi_test_project.context
    assert raw_items_seed.canonical_name(context) == "sushi.waiter_names"
    assert raw_items_seed.to_sqlmesh(context).name == "sushi.waiter_names"

    raw_items_seed.dialect_ = "snowflake"
    assert raw_items_seed.to_sqlmesh(sushi_test_project.context).name == "sushi.waiter_names"
    assert (
        raw_items_seed.to_sqlmesh(sushi_test_project.context).fqn
        == '"MEMORY"."SUSHI"."WAITER_NAMES"'
    )

    waiter_revenue_semicolon_seed = seed_configs["waiter_revenue_semicolon"]

    expected_config_semicolon = {
        "path": Path(sushi_test_project.context.project_root, "seeds/waiter_revenue_semicolon.csv"),
        "schema_": "sushi",
        "delimiter": ";",
    }
    actual_config_semicolon = {
        k: getattr(waiter_revenue_semicolon_seed, k) for k, v in expected_config_semicolon.items()
    }
    assert actual_config_semicolon == expected_config_semicolon

    assert waiter_revenue_semicolon_seed.canonical_name(context) == "sushi.waiter_revenue_semicolon"
    assert (
        waiter_revenue_semicolon_seed.to_sqlmesh(context).name == "sushi.waiter_revenue_semicolon"
    )
    assert waiter_revenue_semicolon_seed.delimiter == ";"
    assert set(waiter_revenue_semicolon_seed.columns.keys()) == {"waiter_id", "revenue", "quarter"}


def test_quoting():
    model = ModelConfig(alias="bar", schema="foo")
    assert str(BaseRelation.create(**model.relation_info)) == '"foo"."bar"'

    model.quoting["identifier"] = False
    assert str(BaseRelation.create(**model.relation_info)) == '"foo".bar'

    source = SourceConfig(identifier="bar", schema="foo")
    assert str(BaseRelation.create(**source.relation_info)) == '"foo"."bar"'

    source.quoting["schema"] = False
    assert str(BaseRelation.create(**source.relation_info)) == 'foo."bar"'


def test_source_canonical_name_with_dots_and_spaces(mocker):
    from dbt.adapters.base import BaseRelation

    mock_context = mocker.Mock()
    mock_context.target.database = "target_db"

    def mock_source_macro(source_name, table_name):
        if table_name == "my_table_dot":
            identifier = "FILENAME.CSV"
        elif table_name == "my_table_space":
            identifier = "my table space"
        else:
            identifier = "my_table_std"
        return BaseRelation.create(
            database="RAW_DEV",
            schema="raw_schema",
            identifier=identifier,
        )

    mock_context.get_callable_macro.return_value = mock_source_macro

    # 1. Identifier with a dot
    source_dot = SourceConfig(
        name="my_table_dot",
        source_name="my_source",
        identifier="FILENAME.CSV",
    )
    assert source_dot.canonical_name(mock_context) == 'RAW_DEV.raw_schema."FILENAME.CSV"'

    # 2. Identifier with a space
    source_space = SourceConfig(
        name="my_table_space",
        source_name="my_source",
        identifier="my table space",
    )
    assert source_space.canonical_name(mock_context) == 'RAW_DEV.raw_schema."my table space"'

    # 3. Standard identifier (without dots or spaces) should not be quoted
    source_std = SourceConfig(
        name="my_table_std",
        source_name="my_source",
        identifier="my_table_std",
    )
    assert source_std.canonical_name(mock_context) == "RAW_DEV.raw_schema.my_table_std"

    # 4. Standard identifier, but with database matching target database (to test database omission)
    mock_context_target_db = mocker.Mock()
    mock_context_target_db.target.database = "RAW_DEV"
    mock_context_target_db.get_callable_macro.return_value = mock_source_macro

    source_dot_target = SourceConfig(
        name="my_table_dot",
        source_name="my_source",
        identifier="FILENAME.CSV",
    )
    source_std_target = SourceConfig(
        name="my_table_std",
        source_name="my_source",
        identifier="my_table_std",
    )

    assert source_dot_target.canonical_name(mock_context_target_db) == 'raw_schema."FILENAME.CSV"'
    assert source_std_target.canonical_name(mock_context_target_db) == "raw_schema.my_table_std"


def _test_warehouse_config(
    config_yaml: str, target_class: t.Type[TargetConfig], *params_path: str
) -> TargetConfig:
    config_dict = yaml_load(config_yaml)
    for path in params_path:
        config_dict = config_dict[path]

    config = target_class(**{"name": "dev", **config_dict})

    for key, value in config.dict().items():
        input_value = config_dict.get(key)
        if input_value is not None:
            assert input_value == value

    return config


def test_duckdb_threads(tmp_path):
    dbt_project_dir = "tests/fixtures/dbt/sushi_test"
    temp_dir = tmp_path / "sushi_test"

    copytree(dbt_project_dir, temp_dir, symlinks=True)

    with open(temp_dir / "profiles.yml", "w", encoding="utf-8") as f:
        f.write(
            """
            sushi:
              outputs:
                in_memory:
                  type: duckdb
                  schema: sushi
                  threads: 4
              target: in_memory
            """
        )

    config = sqlmesh_config(temp_dir)
    assert config.gateways["in_memory"].connection.concurrent_tasks == 1


def test_snowflake_config():
    config = _test_warehouse_config(
        """
        sushi:
          target: dev
          outputs:
            dev:
              account: redacted_account
              database: sushi
              password: redacted_password
              role: accountadmin
              schema: sushi
              threads: 1
              type: snowflake
              user: redacted_user
              warehouse: redacted_warehouse

        """,
        SnowflakeConfig,
        "sushi",
        "outputs",
        "dev",
    )
    sqlmesh_config = config.to_sqlmesh()
    assert sqlmesh_config.application == "Tobiko_SQLMesh"
    assert (
        sqlmesh_config.schema_differ_overrides == SCHEMA_DIFFER_OVERRIDES["schema_differ_overrides"]
    )


def test_snowflake_config_private_key_path():
    config = _test_warehouse_config(
        """
        sushi:
          target: dev
          outputs:
            dev:
              type: snowflake
              account: redacted_account
              user: redacted_user
              database: sushi
              role: accountadmin
              schema: sushi
              threads: 1
              warehouse: redacted_warehouse
              private_key_path: tests/fixtures/snowflake/rsa_key_pass.p8
              private_key_passphrase: insecure

        """,
        SnowflakeConfig,
        "sushi",
        "outputs",
        "dev",
    )

    sqlmesh_config = config.to_sqlmesh()
    assert sqlmesh_config.private_key_path == "tests/fixtures/snowflake/rsa_key_pass.p8"
    assert sqlmesh_config.private_key_passphrase == "insecure"


def test_snowflake_config_private_key():
    private_key_b64 = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCvMKgsYzoDMnl7QW9nWTzAMMQToyUTslgKlH9MezcEYUvvCv+hYEsY9YGQ5dhI5MSY1vkQ+Wtqc6KsvJQzMaHDA1W+Z5R/yA/IY+Mp2KqJijQxnp8XjZs1t6Unr0ssL2yBjlk2pNOZX3w4A6B6iwpkqUi/HtqI5t2M15FrUMF3rNcH68XMcDa1gAasGuBpzJtBM0bp4/cHa18xWZZfu3d2d+4CCfYUvE3OYXQXMjJunidnU56NZtYlJcKT8Fmlw16fSFsPAG01JOIWBLJmSMi5qhhB2w90AAq5URuupCbwBKB6KvwzPRWn+fZKGAvvlR7P3CGebwBJEJxnq85MljzRAgMBAAECggEAKXaTpwXJGi6dD+35xvUY6sff8GHhiZrhOYfR5TEYYWIBzc7Fl9UpkPuyMbAkk4QJf78JbdoKcURzEP0E+mTZy0UDyy/Ktr+L9LqnbiUIn8rk9YV8U9/BB2KypQTY/tkuji85sDQsnJU72ioJlldIG3DxdcKAqHwznXz7vvF7CK6rcsz37hC5w7MTtguvtzNyHGkvJ1ZBTHI1vvGR/VQJoSSFkv6nLFs2xl197kuM2x+Ss539Xbg7GGXX90/sgJP+QLyNk6kYezekRt5iCK6n3UxNfEqd0GX03AJ1oVtFM9SLx0RMHiLuXVCKlQLJ1LYf8zOT31yOun6hhowNmHvpLQKBgQDzXGQqBLvVNi9gQzQhG6oWXxdtoBILnGnd8DFsb0YZIe4PbiyoFb8b4tJuGz4GVfugeZYL07I8TsQbPKFH3tqFbx69hENMUOo06PZ4H7phucKk8Er/JHW8dhkVQVg1ttTK8J5kOm+uKjirqN5OkLlUNSSJMblaEr9AHGPmTu21MwKBgQC4SeYzJDvq/RTQk5d7AwVEokgFk95aeyv77edFAhnrD3cPIAQnPlfVyG7RgPA94HrSAQ5Hr0PL2hiQ7OxX1HfP+66FMcTVbZwktYULZuj4NMxJqwxKbCmmzzACiPF0sibg8efGMY9sAmcQRw5JRS2s6FQns1MqeksnjzyMf3196wKBgFf8zJ5AjeT9rU1hnuRliy6BfQf+uueFyuUaZdQtuyt1EAx2KiEvk6QycyCqKtfBmLOhojVued/CHrc2SZ2hnmJmFbgxrN9X1gYBQLOXzRxuPEjENGlhNkxIarM7p/frva4OJ0ZXtm9DBrBR4uaG/urKOAZ+euRtKMa2PQxU9y7vAoGAeZWX4MnZFjIe13VojWnywdNnPPbPzlZRMIdG+8plGyY64Km408NX492271XoKoq9vWug5j6FtiqP5p3JWDD/UyKzg4DQYhdM2xM/UcR1k7wRw9Cr7TXrTPiIrkN3OgyHhgVTavkrrJDxOlYG4ORZPCiTzRWMmwvQJatkwTUjsD0CgYEA8nAWBSis9H8n9aCEW30pGHT8LwqlH0XfXwOTPmkxHXOIIkhNFiZRAzc4NKaefyhzdNlc7diSMFVXpyLZ4K0l5dY1Ou2xRh0W+xkRjjKsMib/s9g/crtam+tXddADJDokLELn5PAMhaHBpti+PpOMGqdI3Wub+5yT1XCXT9aj6yU="

    config = _test_warehouse_config(
        f"""
        sushi:
          target: dev
          outputs:
            dev:
              type: snowflake
              account: redacted_account
              user: redacted_user
              database: sushi
              role: accountadmin
              schema: sushi
              threads: 1
              warehouse: redacted_warehouse
              private_key: '{private_key_b64}'

        """,
        SnowflakeConfig,
        "sushi",
        "outputs",
        "dev",
    )
    sqlmesh_config = config.to_sqlmesh()
    assert sqlmesh_config.private_key == base64.b64decode(private_key_b64)


def test_snowflake_config_oauth_access_token():
    oauth_access_token = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFmZmM2MjkwN2E0NDYxODJhZGMxZmE0ZTgxZmRiYTYzMTBkY2U2M2YifQ.eyJhenAiOiIyNzIxOTYwNjkxNzMtZm81ZWI0MXQzbmR1cTZ1ZXRkc2pkdWdzZXV0ZnBtc3QuYXBwcy5nb29nbGV1c2VyY29udGVudC5jb20iLCJhdWQiOiIyNzIxOTYwNjkxNzMtZm81ZWI0MXQzbmR1cTZ1ZXRkc2pkdWdzZXV0ZnBtc3QuYXBwcy5nb29nbGV1c2VyY29udGVudC5jb20iLCJzdWIiOiIxMTc4NDc5MTI4NzU5MTM5MDU0OTMiLCJlbWFpbCI6ImFhcm9uLnBhcmVja2lAZ21haWwuY29tIiwiZW1haWxfdmVyaWZpZWQiOnRydWUsImF0X2hhc2giOiJpRVljNDBUR0luUkhoVEJidWRncEpRIiwiZXhwIjoxNTI0NTk5MDU2LCJpc3MiOiJodHRwczovL2FjY291bnRzLmdvb2dsZS5jb20iLCJpYXQiOjE1MjQ1OTU0NTZ9.ho2czp_1JWsglJ9jN8gCgWfxDi2gY4X5-QcT56RUGkgh5BJaaWdlrRhhN_eNuJyN3HRPhvVA_KJVy1tMltTVd2OQ6VkxgBNfBsThG_zLPZriw7a1lANblarwxLZID4fXDYG-O8U-gw4xb-NIsOzx6xsxRBdfKKniavuEg56Sd3eKYyqrMA0DWnIagqLiKE6kpZkaGImIpLcIxJPF0-yeJTMt_p1NoJF7uguHHLYr6752hqppnBpMjFL2YMDVeg3jl1y5DeSKNPh6cZ8H2p4Xb2UIrJguGbQHVIJvtm_AspRjrmaTUQKrzXDRCfDROSUU-h7XKIWRrEd2-W9UkV5oCg"
    config = _test_warehouse_config(
        f"""
            sushi:
              target: dev
              outputs:
                dev:
                  type: snowflake
                  account: redacted_account
                  user: redacted_user
                  database: sushi
                  role: accountadmin
                  schema: sushi
                  threads: 1
                  warehouse: redacted_warehouse
                  authenticator: oauth
                  token: '{oauth_access_token}'

            """,
        SnowflakeConfig,
        "sushi",
        "outputs",
        "dev",
    )
    sqlmesh_config = config.to_sqlmesh()
    assert sqlmesh_config.token == oauth_access_token


def test_postgres_config():
    _test_warehouse_config(
        """
        dbt-postgres:
          target: dev
          outputs:
            dev:
              type: postgres
              host: postgres
              user: postgres
              password: postgres
              port: 5432
              dbname: postgres
              schema: demo
              threads: 3
              keepalives_idle: 0
        """,
        PostgresConfig,
        "dbt-postgres",
        "outputs",
        "dev",
    )
    # 'pass' field instead of 'password'
    _test_warehouse_config(
        """
        dbt-postgres:
          target: dev
          outputs:
            dev:
              type: postgres
              host: postgres
              user: postgres
              pass: postgres
              port: 5432
              dbname: postgres
              schema: demo
              threads: 3
              keepalives_idle: 0
        """,
        PostgresConfig,
        "dbt-postgres",
        "outputs",
        "dev",
    )


def test_redshift_config():
    _test_warehouse_config(
        """
        dbt-redshift:
          target: dev
          outputs:
            dev:
              type: redshift
              host: hostname.region.redshift.amazonaws.com
              user: username
              password: password1
              port: 5439
              dbname: analytics
              schema: analytics
              threads: 4
              ra3_node: false
        """,
        RedshiftConfig,
        "dbt-redshift",
        "outputs",
        "dev",
    )
    # 'pass' field instead of 'password'
    _test_warehouse_config(
        """
        dbt-redshift:
          target: dev
          outputs:
            dev:
              type: redshift
              host: hostname.region.redshift.amazonaws.com
              user: username
              pass: password1
              port: 5439
              dbname: analytics
              schema: analytics
              threads: 4
              ra3_node: false
        """,
        RedshiftConfig,
        "dbt-redshift",
        "outputs",
        "dev",
    )


def test_databricks_config():
    _test_warehouse_config(
        """
        dbt-databricks:
          target: dev
          outputs:
            dev:
              type: databricks
              catalog: test_catalog
              schema: analytics
              host: yourorg.databrickshost.com
              http_path: /sql/your/http/path
              token: dapi01234567890123456789012
        """,
        DatabricksConfig,
        "dbt-databricks",
        "outputs",
        "dev",
    )


def test_databricks_config_oauth():
    config = _test_warehouse_config(
        """
        dbt-databricks:
          target: dev
          outputs:
            dev:
              type: databricks
              catalog: test_catalog
              schema: analytics
              host: yourorg.databrickshost.com
              http_path: /sql/your/http/path
              auth_type: oauth
              client_id: client-id
              client_secret: client-secret
        """,
        DatabricksConfig,
        "dbt-databricks",
        "outputs",
        "dev",
    )

    as_sqlmesh = config.to_sqlmesh()
    assert as_sqlmesh.auth_type == "databricks-oauth"
    assert as_sqlmesh.oauth_client_id == "client-id"
    assert as_sqlmesh.oauth_client_secret == "client-secret"
    assert as_sqlmesh.schema_differ_overrides == SCHEMA_DIFFER_OVERRIDES["schema_differ_overrides"]


def test_bigquery_config():
    _test_warehouse_config(
        """
        dbt-bigquery:
          target: dev
          outputs:
            dev:
              type: bigquery
              method: oauth
              project: your-project
              dataset: your-dataset
              threads: 1
              location: US
              keyfile: /path/to/keyfile.json
        """,
        BigQueryConfig,
        "dbt-bigquery",
        "outputs",
        "dev",
    )
    _test_warehouse_config(
        """
        dbt-bigquery:
          target: dev
          outputs:
            dev:
              type: bigquery
              method: oauth
              project: your-project
              schema: your-dataset
              threads: 1
              location: US
              keyfile: /path/to/keyfile.json
        """,
        BigQueryConfig,
        "dbt-bigquery",
        "outputs",
        "dev",
    )
    with pytest.raises(ConfigError):
        _test_warehouse_config(
            """
            dbt-bigquery:
              target: dev
              outputs:
                dev:
                  type: bigquery
                  method: oauth
                  project: your-project
                  threads: 1
                  location: US
                  keyfile: /path/to/keyfile.json
            """,
            BigQueryConfig,
            "dbt-bigquery",
            "outputs",
            "dev",
        )


def test_sqlserver_config():
    _test_warehouse_config(
        """
        dbt-sqlserver:
          target: dev
          outputs:
            dev:
              type: sqlserver
              host: localhost
              user: user
              password: password
              database: master
              driver: "ODBC Driver 17 for SQL Server"
              schema: sushi
              threads: 1
              authentication: sql
        """,
        MSSQLConfig,
        "dbt-sqlserver",
        "outputs",
        "dev",
    )


def test_trino_config():
    _test_warehouse_config(
        """
        dbt-trino:
          target: dev
          outputs:
            dev:
              type: trino
              method: ldap
              user: user
              password: password
              host: localhost
              database: database
              schema: dbt_schema
              port: 443
              threads: 1
        """,
        TrinoConfig,
        "dbt-trino",
        "outputs",
        "dev",
    )


def test_athena_config():
    _test_warehouse_config(
        """
        dbt-athena:
          target: dev
          outputs:
            dev:
              type: athena
              s3_staging_dir: s3://athena-query-results/dbt/
              s3_data_dir: s3://your_s3_bucket/dbt/
              s3_data_naming: schema_table
              s3_tmp_table_dir: s3://your_s3_bucket/temp/
              region_name: eu-west-1
              schema: dbt
              database: awsdatacatalog
              threads: 4
              aws_profile_name: my-profile
              work_group: my-workgroup
              spark_work_group: my-spark-workgroup
              seed_s3_upload_args:
                ACL: bucket-owner-full-control
        """,
        AthenaConfig,
        "dbt-athena",
        "outputs",
        "dev",
    )


def test_clickhouse_config():
    _test_warehouse_config(
        """
        dbt-clickhouse:
          target: dev
          outputs:
            dev:
              type: clickhouse
              host: thehost
              user: theuser
              password: thepassword
              port: 1234
              secure: true
              cluster: thecluster
              connect_timeout: 1
              send_receive_timeout: 2
              verify: false
              compression: lz4
              custom_settings:
                setting: value

        """,
        ClickhouseConfig,
        "dbt-clickhouse",
        "outputs",
        "dev",
    )


def test_connection_args(tmp_path):
    dbt_project_dir = "tests/fixtures/dbt/sushi_test"

    config = sqlmesh_config(dbt_project_dir)
    assert not config.gateways["in_memory"].connection.register_comments

    config = sqlmesh_config(dbt_project_dir, register_comments=True)
    assert config.gateways["in_memory"].connection.register_comments


def test_custom_dbt_loader():
    from sqlmesh.core.loader import SqlMeshLoader
    from sqlmesh.dbt.loader import DbtLoader

    dbt_project_dir = "tests/fixtures/dbt/sushi_test"
    with pytest.raises(ConfigError, match="The loader must be a DbtLoader."):
        sqlmesh_config(dbt_project_dir, loader=SqlMeshLoader)

    class CustomDbtLoader(DbtLoader):
        pass

    sqlmesh_config(dbt_project_dir, loader=CustomDbtLoader)


@pytest.mark.cicdonly
@pytest.mark.slow
def test_db_type_to_relation_class():
    from dbt.adapters.bigquery.relation import BigQueryRelation
    from dbt.adapters.databricks.relation import DatabricksRelation
    from dbt.adapters.duckdb.relation import DuckDBRelation
    from dbt.adapters.redshift import RedshiftRelation
    from dbt.adapters.snowflake import SnowflakeRelation

    assert (TARGET_TYPE_TO_CONFIG_CLASS["bigquery"].relation_class) == BigQueryRelation
    assert (TARGET_TYPE_TO_CONFIG_CLASS["databricks"].relation_class) == DatabricksRelation
    assert (TARGET_TYPE_TO_CONFIG_CLASS["duckdb"].relation_class) == DuckDBRelation
    assert (TARGET_TYPE_TO_CONFIG_CLASS["redshift"].relation_class) == RedshiftRelation
    assert (TARGET_TYPE_TO_CONFIG_CLASS["snowflake"].relation_class) == SnowflakeRelation

    from dbt.adapters.clickhouse.relation import ClickHouseRelation
    from dbt.adapters.trino.relation import TrinoRelation
    from dbt.adapters.athena.relation import AthenaRelation

    assert (TARGET_TYPE_TO_CONFIG_CLASS["clickhouse"].relation_class) == ClickHouseRelation
    assert (TARGET_TYPE_TO_CONFIG_CLASS["trino"].relation_class) == TrinoRelation
    assert (TARGET_TYPE_TO_CONFIG_CLASS["athena"].relation_class) == AthenaRelation


@pytest.mark.cicdonly
@pytest.mark.slow
def test_db_type_to_column_class():
    from dbt.adapters.bigquery import BigQueryColumn
    from dbt.adapters.databricks.column import DatabricksColumn
    from dbt.adapters.snowflake import SnowflakeColumn

    assert (TARGET_TYPE_TO_CONFIG_CLASS["bigquery"].column_class) == BigQueryColumn
    assert (TARGET_TYPE_TO_CONFIG_CLASS["databricks"].column_class) == DatabricksColumn
    assert (TARGET_TYPE_TO_CONFIG_CLASS["duckdb"].column_class) == Column
    assert (TARGET_TYPE_TO_CONFIG_CLASS["snowflake"].column_class) == SnowflakeColumn

    from dbt.adapters.clickhouse.column import ClickHouseColumn
    from dbt.adapters.trino.column import TrinoColumn
    from dbt.adapters.athena.column import AthenaColumn

    assert (TARGET_TYPE_TO_CONFIG_CLASS["clickhouse"].column_class) == ClickHouseColumn
    assert (TARGET_TYPE_TO_CONFIG_CLASS["trino"].column_class) == TrinoColumn
    assert (TARGET_TYPE_TO_CONFIG_CLASS["athena"].column_class) == AthenaColumn


def test_db_type_to_quote_policy():
    assert isinstance(TARGET_TYPE_TO_CONFIG_CLASS["duckdb"].quote_policy, Policy)


def test_variable_override():
    project_root = "tests/fixtures/dbt/sushi_test"
    project = Project.load(
        DbtContext(
            project_root=Path(project_root),
            sqlmesh_config=Config(model_defaults=ModelDefaultsConfig(start="2021-01-01")),
        ),
        variables={"yet_another_var": 2},
    )
    assert project.packages["sushi"].variables["yet_another_var"] == 2


@pytest.mark.slow
def test_depends_on(assert_exp_eq, sushi_test_project):
    # Case 1: using an undefined variable without a default value
    context = sushi_test_project.context

    model_config = ModelConfig(
        alias="sushi.test",
        sql="SELECT * FROM {{ ref('waiter_revenue_by_day') }} JOIN other_table",
        dependencies=Dependencies(refs=["waiter_revenue_by_day"]),
    )

    sqlmesh_model = model_config.to_sqlmesh(context)
    assert sqlmesh_model.depends_on_ == {'"memory"."sushi"."waiter_revenue_by_day_v2"'}
    assert sqlmesh_model.depends_on == {'"memory"."sushi"."waiter_revenue_by_day_v2"'}
    assert sqlmesh_model.full_depends_on == {'"memory"."sushi"."waiter_revenue_by_day_v2"'}

    # Make sure the query wasn't rendered
    assert not sqlmesh_model._query_renderer._cache


@pytest.mark.parametrize(
    "on_schema_change, expected_additive, expected_destructive",
    [
        ("ignore", OnAdditiveChange.IGNORE, OnDestructiveChange.IGNORE),
        ("fail", OnAdditiveChange.ERROR, OnDestructiveChange.ERROR),
        ("append_new_columns", OnAdditiveChange.ALLOW, OnDestructiveChange.IGNORE),
        ("sync_all_columns", OnAdditiveChange.ALLOW, OnDestructiveChange.ALLOW),
    ],
)
def test_on_schema_change_properties(
    on_schema_change: str,
    expected_additive: OnAdditiveChange,
    expected_destructive: OnDestructiveChange,
):
    model_config = ModelConfig(
        name="name",
        package_name="package",
        alias="model",
        schema="custom",
        database="database",
        materialized=Materialization.INCREMENTAL,
        sql="SELECT * FROM foo.table",
        time_column="ds",
        start="Jan 1 2023",
        batch_size=5,
        batch_concurrency=2,
        on_schema_change=on_schema_change,
    )
    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="foo")
    model = model_config.to_sqlmesh(context)

    assert model.on_additive_change == expected_additive
    assert model.on_destructive_change == expected_destructive


def test_sqlmesh_model_kwargs_columns_override():
    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="foo")

    kwargs = ModelConfig(dialect="duckdb").sqlmesh_model_kwargs(
        context,
        {"c": ColumnConfig(name="c", data_type="uinteger")},
    )
    assert kwargs.get("columns") == {"c": exp.DataType.build(exp.DataType.Type.UINT)}


@pytest.mark.parametrize(
    "dialect",
    [
        "databricks",
        "duckdb",
        "postgres",
        "redshift",
        "snowflake",
        "bigquery",
        "trino",
        "clickhouse",
    ],
)
def test_api_class_loading(dialect: str):
    Api(dialect)


def test_empty_vars_config(tmp_path):
    """Test that a dbt project can be loaded with an empty vars config."""
    dbt_project_dir = tmp_path / "test_project"
    dbt_project_dir.mkdir()

    # Create a minimal dbt_project.yml with empty vars
    dbt_project_yml = dbt_project_dir / "dbt_project.yml"
    dbt_project_yml.write_text("""
name: test_empty_vars

version: "1.0.0"
config-version: 2

profile: test_empty_vars

models:
  +start: Jan 1 2022

# Empty vars section - various ways to specify empty
vars:
    """)

    # Create a minimal profiles.yml
    profiles_yml = dbt_project_dir / "profiles.yml"
    profiles_yml.write_text("""
test_empty_vars:
  outputs:
    dev:
      type: duckdb
      schema: test
  target: dev
    """)

    # Create a simple model
    model = dbt_project_dir / "models" / "some_model.sql"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("SELECT 1 as id")

    # Load the project
    from sqlmesh.dbt.context import DbtContext
    from sqlmesh.dbt.project import Project
    from sqlmesh.core.config import Config

    context = DbtContext(project_root=dbt_project_dir, sqlmesh_config=Config())

    # This should not raise an error even with empty vars
    project = Project.load(context)

    # Verify the project loaded successfully
    assert project.packages["test_empty_vars"] is not None
    assert project.packages["test_empty_vars"].name == "test_empty_vars"

    # Verify the variables are empty (not causing any issues)
    assert project.packages["test_empty_vars"].variables == {}
    assert project.context.variables == {}


def test_infer_state_schema_name(create_empty_project: EmptyProjectCreator):
    project_dir, _ = create_empty_project("test_foo", "dev")

    # infer_state_schema_name defaults to False if omitted
    config = sqlmesh_config(project_root=project_dir)
    assert config.dbt
    assert not config.dbt.infer_state_schema_name
    assert config.get_state_schema() == "sqlmesh"

    # create_empty_project() uses the default dbt template for sqlmesh yaml config which
    # sets infer_state_schema_name=True
    ctx = Context(paths=[project_dir])
    assert ctx.config.dbt
    assert ctx.config.dbt.infer_state_schema_name
    assert ctx.config.get_state_schema() == "sqlmesh_state_test_foo_main"
    assert isinstance(ctx.state_sync, CachingStateSync)
    assert isinstance(ctx.state_sync.state_sync, EngineAdapterStateSync)
    assert ctx.state_sync.state_sync.schema == "sqlmesh_state_test_foo_main"

    # If the user delberately overrides state_schema then we should respect this choice
    config_file = project_dir / "sqlmesh.yaml"
    config_yaml = yaml_load(config_file)
    config_yaml["gateways"] = {"dev": {"state_schema": "state_override"}}
    config_file.write_text(yaml_dump(config_yaml))

    ctx = Context(paths=[project_dir])
    assert ctx.config.dbt
    assert ctx.config.dbt.infer_state_schema_name
    assert ctx.config.get_state_schema() == "state_override"
    assert isinstance(ctx.state_sync, CachingStateSync)
    assert isinstance(ctx.state_sync.state_sync, EngineAdapterStateSync)
    assert ctx.state_sync.state_sync.schema == "state_override"
