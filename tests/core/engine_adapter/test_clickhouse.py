import pytest
from sqlmesh.core.engine_adapter import ClickhouseEngineAdapter
from sqlmesh.core.model.definition import load_sql_based_model
from sqlmesh.core.model.kind import ModelKindName
from sqlmesh.core.engine_adapter.shared import EngineRunMode, DataObject
from tests.core.engine_adapter import to_sql_calls
from sqlmesh.core.dialect import parse
from sqlglot import exp, parse_one
import typing as t
from datetime import datetime
from pytest_mock.plugin import MockerFixture
from sqlmesh.core import dialect as d
from sqlglot.optimizer.qualify_columns import quote_identifiers

pytestmark = [pytest.mark.clickhouse, pytest.mark.engine]


@pytest.fixture
def adapter(make_mocked_engine_adapter, mocker) -> ClickhouseEngineAdapter:
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.STANDALONE),
    )

    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    return adapter


def test_create_schema(adapter: ClickhouseEngineAdapter, mocker):
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "cluster",
        new_callable=mocker.PropertyMock(return_value="default"),
    )

    # ON CLUSTER not added because engine_run_mode.is_cluster=False
    adapter.create_schema("foo")

    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.CLUSTER),
    )
    adapter.create_schema("foo")

    assert to_sql_calls(adapter) == [
        'CREATE DATABASE IF NOT EXISTS "foo"',
        'CREATE DATABASE IF NOT EXISTS "foo" ON CLUSTER "default"',
    ]


def test_drop_schema(adapter: ClickhouseEngineAdapter, mocker):
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "cluster",
        new_callable=mocker.PropertyMock(return_value="default"),
    )

    # ON CLUSTER not added because engine_run_mode.is_cluster=False
    adapter.drop_schema("foo")

    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.CLUSTER),
    )
    adapter.drop_schema("foo")

    assert to_sql_calls(adapter) == [
        'DROP DATABASE IF EXISTS "foo"',
        'DROP DATABASE IF EXISTS "foo" ON CLUSTER "default"',
    ]


def test_create_table(adapter: ClickhouseEngineAdapter, mocker):
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "cluster",
        new_callable=mocker.PropertyMock(return_value="default"),
    )

    # ON CLUSTER not added because engine_run_mode.is_cluster=False
    adapter.create_table("foo", {"a": exp.DataType.build("Int8", dialect=adapter.dialect)})
    # adapter.create_table_like("target", "source")

    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.CLUSTER),
    )
    adapter.create_table("foo", {"a": exp.DataType.build("Int8", dialect=adapter.dialect)})
    # adapter.create_table_like("target", "source")

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "foo" ("a" Int8) ENGINE=MergeTree ORDER BY ()',
        # "CREATE TABLE IF NOT EXISTS target AS source",
        'CREATE TABLE IF NOT EXISTS "foo" ON CLUSTER "default" ("a" Int8) ENGINE=MergeTree ORDER BY ()',
        # "CREATE TABLE IF NOT EXISTS target AS source",
    ]


def test_rename_table(adapter: ClickhouseEngineAdapter, mocker):
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "cluster",
        new_callable=mocker.PropertyMock(return_value="default"),
    )

    # ON CLUSTER not added because engine_run_mode.is_cluster=False
    adapter.rename_table(exp.to_table("foo"), exp.to_table("bar"))

    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.CLUSTER),
    )
    adapter.rename_table(exp.to_table("foo"), exp.to_table("bar"))

    assert to_sql_calls(adapter) == [
        'RENAME TABLE "foo" TO "bar"',
        'RENAME TABLE "foo" TO "bar" ON CLUSTER "default" ',
    ]


def test_delete_from(adapter: ClickhouseEngineAdapter, mocker):
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "cluster",
        new_callable=mocker.PropertyMock(return_value="default"),
    )

    # ON CLUSTER not added because engine_run_mode.is_cluster=False
    adapter.delete_from(exp.to_table("foo"), "a = 1")

    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.CLUSTER),
    )
    adapter.delete_from(exp.to_table("foo"), "a = 1")

    assert to_sql_calls(adapter) == [
        'DELETE FROM "foo" WHERE "a" = 1',
        'DELETE FROM "foo" ON CLUSTER "default" WHERE "a" = 1',
    ]


def test_alter_table(
    adapter: ClickhouseEngineAdapter,
    mocker,
):
    adapter.SCHEMA_DIFFER_KWARGS = {}
    current_table_name = "test_table"
    current_table = {"a": "Int8", "b": "String", "c": "Int8"}
    target_table_name = "target_table"
    target_table = {
        "a": "Int8",
        "b": "String",
        "f": "String",
    }

    def table_columns(table_name: str) -> t.Dict[str, exp.DataType]:
        if table_name == current_table_name:
            return {
                k: exp.DataType.build(v, dialect=adapter.dialect) for k, v in current_table.items()
            }
        return {k: exp.DataType.build(v, dialect=adapter.dialect) for k, v in target_table.items()}

    adapter.columns = table_columns  # type: ignore

    # ON CLUSTER not added because engine_run_mode.is_cluster=False
    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))

    mocker.patch.object(
        ClickhouseEngineAdapter,
        "cluster",
        new_callable=mocker.PropertyMock(return_value="default"),
    )
    mocker.patch.object(
        ClickhouseEngineAdapter,
        "engine_run_mode",
        new_callable=mocker.PropertyMock(return_value=EngineRunMode.CLUSTER),
    )

    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))

    assert to_sql_calls(adapter) == [
        'ALTER TABLE "test_table" DROP COLUMN "c"',
        'ALTER TABLE "test_table" ADD COLUMN "f" String',
        'ALTER TABLE "test_table" ON CLUSTER "default" DROP COLUMN "c"',
        'ALTER TABLE "test_table" ON CLUSTER "default" ADD COLUMN "f" String',
    ]


def test_nullable_datatypes_in_model_kind(adapter: ClickhouseEngineAdapter):
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            kind SCD_TYPE_2_BY_TIME(unique_key id, time_data_type Nullable(DateTime64)),
        );

        select 1;
    """,
            default_dialect="clickhouse",
        )
    )

    assert model.kind.name == ModelKindName.SCD_TYPE_2_BY_TIME
    assert model.kind.time_data_type.sql(dialect="clickhouse") == "Nullable(DateTime64)"


def test_nullable_datatypes_in_model_columns(adapter: ClickhouseEngineAdapter):
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            columns (
                id Int64,
                data Nullable(JSON),
                ts DateTime64,
                other Tuple(UInt16, String)
            )
        );

        select 1, 2, 3, 4;
    """,
            default_dialect="clickhouse",
        )
    )

    rendered_columns_to_types = {
        k: v.sql(dialect="clickhouse") for k, v in model.columns_to_types_or_raise.items()
    }

    assert rendered_columns_to_types["id"] == "Int64"
    assert rendered_columns_to_types["data"] == "Nullable(JSON)"
    assert rendered_columns_to_types["ts"] == "DateTime64"
    assert rendered_columns_to_types["other"] == "Tuple(UInt16, String)"


def test_model_properties(adapter: ClickhouseEngineAdapter):
    def build_properties_sql(storage_format="", order_by="", primary_key="", properties=""):
        model = load_sql_based_model(
            parse(
                f"""
        MODEL (
            name foo,
            dialect clickhouse,
            {storage_format}
            physical_properties (
              {order_by}
              {primary_key}
              {properties}
            ),
        );

        select
            *
        from bar;
    """,
                default_dialect="clickhouse",
            )
        )

        return adapter._build_table_properties_exp(
            storage_format=model.storage_format, table_properties=model.physical_properties
        ).sql("clickhouse")

    # no order by or primary key because table engine is not part of "MergeTree" engine family
    assert (
        build_properties_sql(
            storage_format="storage_format Log,",
            order_by="ORDER_BY = a,",
            primary_key="PRIMARY_KEY = a,",
        )
        == "ENGINE=Log"
    )

    assert (
        build_properties_sql(
            storage_format="storage_format ReplicatedMergeTree('/clickhouse/tables/{shard}/table_name', '{replica}', ver),",
            order_by="ORDER_BY = a,",
            primary_key="PRIMARY_KEY = a,",
        )
        == "ENGINE=ReplicatedMergeTree('/clickhouse/tables/{shard}/table_name', '{replica}', ver) ORDER BY (a) PRIMARY KEY (a)"
    )

    assert (
        build_properties_sql(order_by="ORDER_BY = a,", primary_key="PRIMARY_KEY = a,")
        == "ENGINE=MergeTree ORDER BY (a) PRIMARY KEY (a)"
    )

    assert (
        build_properties_sql(order_by='ORDER_BY = "a",', primary_key='PRIMARY_KEY = "a",')
        == 'ENGINE=MergeTree ORDER BY ("a") PRIMARY KEY ("a")'
    )

    assert (
        build_properties_sql(order_by="ORDER_BY = (a),", primary_key="PRIMARY_KEY = (a)")
        == "ENGINE=MergeTree ORDER BY (a) PRIMARY KEY (a)"
    )

    assert build_properties_sql(order_by="ORDER_BY = a + 1,") == "ENGINE=MergeTree ORDER BY (a + 1)"

    assert (
        build_properties_sql(order_by="ORDER_BY = (a + 1),") == "ENGINE=MergeTree ORDER BY (a + 1)"
    )

    assert (
        build_properties_sql(order_by="ORDER_BY = (a, b + 1),", primary_key="PRIMARY_KEY = (a, b)")
        == "ENGINE=MergeTree ORDER BY (a, b + 1) PRIMARY KEY (a, b)"
    )

    # Multiple physical_properties must be combined into a single comma-separated
    # SETTINGS clause. ClickHouse rejects repeated SETTINGS keywords with a syntax
    # error (see https://github.com/SQLMesh/sqlmesh/issues/5803).
    assert (
        build_properties_sql(
            order_by="ORDER_BY = (a, b + 1),",
            primary_key="PRIMARY_KEY = (a, b),",
            properties="PROP1 = 1, PROP2 = '2'",
        )
        == "ENGINE=MergeTree ORDER BY (a, b + 1) PRIMARY KEY (a, b) SETTINGS prop1 = 1, prop2 = '2'"
    )

    # Regression test for #5803: three or more SETTINGS entries also combine.
    assert (
        build_properties_sql(
            order_by="ORDER_BY = (orders_id),",
            properties=(
                "min_age_to_force_merge_seconds = 3600, "
                "min_age_to_force_merge_on_partition_only = 1, "
                "index_granularity = 8192"
            ),
        )
        == "ENGINE=MergeTree ORDER BY (orders_id) "
        "SETTINGS min_age_to_force_merge_seconds = 3600, "
        "min_age_to_force_merge_on_partition_only = 1, "
        "index_granularity = 8192"
    )

    assert (
        build_properties_sql(
            order_by="ORDER_BY = 'timestamp with fill to dateTrunc(\\'DAY\\', toDateTime64(\\'2024-07-11\\', 3)) step toIntervalDay(1) interpolate(price as price)',"
        )
        == "ENGINE=MergeTree ORDER BY (timestamp WITH FILL TO dateTrunc('DAY', toDateTime64('2024-07-11', 3)) STEP toIntervalDay(1) INTERPOLATE (price AS price))"
    )

    assert (
        build_properties_sql(
            order_by="ORDER_BY = (\"a\", 'timestamp with fill to dateTrunc(\\'DAY\\', toDateTime64(\\'2024-07-11\\', 3)) step toIntervalDay(1) interpolate(price as price)'),"
        )
        == "ENGINE=MergeTree ORDER BY (\"a\", timestamp WITH FILL TO dateTrunc('DAY', toDateTime64('2024-07-11', 3)) STEP toIntervalDay(1) INTERPOLATE (price AS price))"
    )

    assert (
        build_properties_sql(properties="TTL = time + INTERVAL 1 WEEK")
        == "ENGINE=MergeTree ORDER BY () TTL time + INTERVAL '1' WEEK"
    )


def test_view_properties_combine_settings(adapter: ClickhouseEngineAdapter):
    # View properties hit the same SettingsProperty code path as table
    # properties (#5803): multiple entries must collapse into one SETTINGS
    # clause rather than emit repeated SETTINGS keywords.
    view_properties_exp = adapter._build_view_properties_exp(
        view_properties={
            "prop1": exp.Literal.number(1),
            "prop2": exp.Literal.string("2"),
        }
    )
    assert view_properties_exp is not None
    assert view_properties_exp.sql("clickhouse") == "SETTINGS prop1 = 1, prop2 = '2'"


def test_partitioned_by_expr(make_mocked_engine_adapter: t.Callable):
    # user doesn't specify, unknown time column type
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            dialect clickhouse,
            kind INCREMENTAL_BY_TIME_RANGE(
              time_column ds
            )
        );

        select
            *
        from bar;
        """,
            default_dialect="clickhouse",
        )
    )

    assert (
        model.partitioned_by[0].sql("clickhouse")
        == """dateTrunc('WEEK', CAST("ds" AS DateTime64(9, 'UTC')))"""
    )

    # user specifies without time column, unknown time column type
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            dialect clickhouse,
            kind INCREMENTAL_BY_TIME_RANGE(
              time_column ds
            ),
            partitioned_by x
        );

        select
            *
        from bar;
        """,
            default_dialect="clickhouse",
        )
    )

    assert [p.sql("clickhouse") for p in model.partitioned_by] == [
        """dateTrunc('WEEK', CAST("ds" AS DateTime64(9, 'UTC')))""",
        '"x"',
    ]

    # user doesn't specify, conformable date/datetime time column type
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            dialect clickhouse,
            kind INCREMENTAL_BY_TIME_RANGE(
              time_column ds
            )
        );

        select
            ds::DATE as ds
        from bar;
        """,
            default_dialect="clickhouse",
        )
    )

    assert model.partitioned_by[0].sql("clickhouse") == """dateTrunc('WEEK', "ds")"""

    # user doesn't specify, non-conformable time column type
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            dialect clickhouse,
            kind INCREMENTAL_BY_TIME_RANGE(
              time_column ds
            )
        );

        select
            ds::String as ds
        from bar;
        """,
            default_dialect="clickhouse",
        )
    )

    assert (
        model.partitioned_by[0].sql("clickhouse")
        == """CAST(dateTrunc('WEEK', CAST("ds" AS DateTime64(9, 'UTC'))) AS String)"""
    )

    # user specifies partitioned_by with time column
    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name foo,
            dialect clickhouse,
            kind INCREMENTAL_BY_TIME_RANGE(
              time_column ds
            ),
            partitioned_by toStartOfWeek(ds)
        );

        select
            *
        from bar;
        """,
            default_dialect="clickhouse",
        )
    )

    assert model.partitioned_by == [exp.func("toStartOfWeek", '"ds"')]


def test_nullable_partition_cols(make_mocked_engine_adapter: t.Callable, mocker):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }

    adapter.create_table(
        "test_table",
        columns_to_types,
    )

    adapter.create_table(
        "test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("colb")],
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_table" ("cola" Nullable(Int32), "colb" Nullable(String)) ENGINE=MergeTree ORDER BY ()',
        'CREATE TABLE IF NOT EXISTS "test_table" ("cola" Nullable(Int32), "colb" String) ENGINE=MergeTree ORDER BY () PARTITION BY ("colb")',
    ]


def test_create_table_properties(make_mocked_engine_adapter: t.Callable, mocker):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    mocker.patch(
        "sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone",
        return_value="1",
    )

    columns_to_types = {
        "cola": exp.DataType.build("INT", dialect="clickhouse"),
        "colb": exp.DataType.build("TEXT", dialect="clickhouse"),
        "colc": exp.DataType.build("TEXT", dialect="clickhouse"),
    }
    adapter.create_table(
        "test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("colb")],
        storage_format="ReplicatedMergeTree",
        table_properties={
            "ORDER_BY": [exp.to_column("cola"), exp.to_column("colb")],
            "PRIMARY_KEY": [exp.to_column("cola"), exp.to_column("colb")],
        },
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_table" ("cola" Int32, "colb" String, "colc" String) ENGINE=ReplicatedMergeTree ORDER BY ("cola", "colb") PRIMARY KEY ("cola", "colb") PARTITION BY ("colb")',
    ]


def test_nulls_after_join(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    query = exp.select("col1").from_("table")

    assert (
        adapter.ensure_nulls_for_unmatched_after_join(query.copy()).sql(adapter.dialect)
        == "SELECT col1 FROM table SETTINGS join_use_nulls = 1"
    )

    # User already set the setting, so we should not override it
    query_with_setting = query.copy()
    query_with_setting.set(
        "settings",
        [
            exp.EQ(
                this=exp.var("join_use_nulls"),
                expression=exp.Literal(this="0", is_string=False),
            )
        ],
    )

    assert (
        adapter.use_server_nulls_for_unmatched_after_join(query_with_setting) == query_with_setting
    )

    # Server default of 0 != method default of 1, so we inject 0
    mocker.patch(
        "sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone",
        return_value="0",
    )

    assert (
        adapter.use_server_nulls_for_unmatched_after_join(query).sql(adapter.dialect)
        == "SELECT col1 FROM table SETTINGS join_use_nulls = 0"
    )


def test_scd_type_2_by_time(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.side_effect = [
        make_temp_table_name(table_name, "efgh"),
        make_temp_table_name(table_name, "abcd"),
    ]

    mocker.patch.object(
        adapter,
        "get_data_objects",
        return_value=[DataObject(schema="", name=table_name, type="table")],
    )

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = None

    # The SCD query we build must specify the setting join_use_nulls = 1. We need to ensure that our
    # setting on the outer query doesn't override the value the user expects.
    #
    # This test's user query does not contain a setting "join_use_nulls", so we determine whether or not
    # to inject it based on the current server value. The mocked server value is 1, so we should not
    # inject.
    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = "1"

    adapter.scd_type_2_by_time(
        target_table="target",
        source_table=t.cast(
            exp.Select, parse_one("SELECT id, name, price, test_UPDATED_at FROM source")
        ),
        unique_key=[
            parse_one("""COALESCE("id", '') || '|' || COALESCE("name", '')"""),
            parse_one("""COALESCE("name", '')"""),
        ],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        updated_at_col=exp.column("test_UPDATED_at", quoted=True),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_UPDATED_at": exp.DataType.build("TIMESTAMP"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert to_sql_calls(adapter)[3] == parse_one(
        """
INSERT INTO "__temp_target_abcd" ("id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to")
WITH "source" AS (
  SELECT DISTINCT ON (COALESCE("id", '') || '|' || COALESCE("name", ''), COALESCE("name", ''))
    TRUE AS "_exists",
    "id",
    "name",
    "price",
    CAST("test_UPDATED_at" AS Nullable(DateTime)) AS "test_UPDATED_at"
  FROM (
    SELECT
      "id",
      "name",
      "price",
      "test_UPDATED_at"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "__temp_target_efgh"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "__temp_target_efgh"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_UPDATED_at",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON (
      COALESCE("static"."id", '') || '|' || COALESCE("static"."name", '')
    ) = (
      COALESCE("latest"."id", '') || '|' || COALESCE("latest"."name", '')
    )
    AND COALESCE("static"."name", '') = COALESCE("latest"."name", '')
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    COALESCE("id", '') || '|' || COALESCE("name", '') AS "_key0",
    COALESCE("name", '') AS "_key1",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    COALESCE("id", '') || '|' || COALESCE("name", ''),
    COALESCE("name", '')
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_UPDATED_at" AS "t_test_UPDATED_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_UPDATED_at" AS "test_UPDATED_at"
  FROM "latest"
  LEFT JOIN "source"
    ON (
      COALESCE("latest"."id", '') || '|' || COALESCE("latest"."name", '')
    ) = (
      COALESCE("source"."id", '') || '|' || COALESCE("source"."name", '')
    )
    AND COALESCE("latest"."name", '') = COALESCE("source"."name", '')
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_UPDATED_at" AS "t_test_UPDATED_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_UPDATED_at" AS "test_UPDATED_at"
  FROM "latest"
  RIGHT JOIN "source"
    ON (
      COALESCE("latest"."id", '') || '|' || COALESCE("latest"."name", '')
    ) = (
      COALESCE("source"."id", '') || '|' || COALESCE("source"."name", '')
    )
    AND COALESCE("latest"."name", '') = COALESCE("source"."name", '')
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("joined"."t_test_UPDATED_at", "joined"."test_UPDATED_at") AS "test_UPDATED_at",
    CASE
      WHEN "t_test_valid_from" IS NULL AND NOT "latest_deleted"."_exists" IS NULL
      THEN CASE
        WHEN "latest_deleted"."test_valid_to" > "test_UPDATED_at"
        THEN "latest_deleted"."test_valid_to"
        ELSE "test_UPDATED_at"
      END
      WHEN "t_test_valid_from" IS NULL
      THEN CAST('1970-01-01 00:00:00' AS Nullable(DateTime64(6)))
      ELSE "t_test_valid_from"
    END AS "test_valid_from",
    CASE
      WHEN "joined"."test_UPDATED_at" > "joined"."t_test_UPDATED_at"
      THEN "joined"."test_UPDATED_at"
      WHEN "joined"."_exists" IS NULL
      THEN CAST('2020-01-01 00:00:00' AS Nullable(DateTime64(6)))
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON (
      COALESCE("joined"."id", '') || '|' || COALESCE("joined"."name", '')
    ) = "latest_deleted"."_key0"
    AND COALESCE("joined"."name", '') = "latest_deleted"."_key1"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_UPDATED_at" AS "test_valid_from",
    CAST(NULL AS Nullable(DateTime64(6))) AS "test_valid_to"
  FROM "joined"
  WHERE
    "joined"."test_UPDATED_at" > "joined"."t_test_UPDATED_at"
)
SELECT "id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to" FROM "static"
UNION ALL SELECT "id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to" FROM "updated_rows"
UNION ALL SELECT "id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to" FROM "inserted_rows"
SETTINGS join_use_nulls = 1
    """,
        dialect=adapter.dialect,
    ).sql(adapter.dialect)


def test_scd_type_2_by_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.side_effect = [
        make_temp_table_name(table_name, "efgh"),
        make_temp_table_name(table_name, "abcd"),
    ]

    mocker.patch.object(
        adapter,
        "get_data_objects",
        return_value=[DataObject(schema="", name=table_name, type="table")],
    )

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = None

    # The SCD query we build must specify the setting join_use_nulls = 1. We need to ensure that our
    # setting on the outer query doesn't override the value the user expects.
    #
    # This test's user query does not contain a setting "join_use_nulls", so we determine whether or not
    # to inject it based on the current server value. The mocked server value is 0, so we should inject.
    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = "0"

    adapter.scd_type_2_by_column(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, name, price FROM source")),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("test_VALID_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        check_columns=[exp.column("name"), exp.column("price")],
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_VALID_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert to_sql_calls(adapter)[3] == parse_one(
        """
INSERT INTO "__temp_target_abcd" ("id", "name", "price", "test_VALID_from", "test_valid_to")
WITH "source" AS (
  SELECT DISTINCT ON ("id")
    TRUE AS "_exists",
    "id",
    "name",
    "price"
  FROM (
    SELECT
      "id",
      "name",
      "price"
    FROM "source"
    SETTINGS join_use_nulls = 0
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_VALID_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "__temp_target_efgh"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_VALID_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "__temp_target_efgh"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_VALID_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id" = "latest"."id"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id" AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_VALID_from" AS "t_test_VALID_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id" = "source"."id"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_VALID_from" AS "t_test_VALID_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id" = "source"."id"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("t_test_VALID_from", CAST('2020-01-01 00:00:00' AS Nullable(DateTime64(6)))) AS "test_VALID_from",
    CASE
      WHEN "joined"."_exists" IS NULL
      OR (
        (
          NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
        )
        AND (
          "joined"."name" <> "joined"."t_name"
          OR (
            "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
          )
          OR (
            NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
          )
          OR "joined"."price" <> "joined"."t_price"
          OR (
            "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
          )
          OR (
            NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
          )
        )
      )
      THEN CAST('2020-01-01 00:00:00' AS Nullable(DateTime64(6)))
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id" = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    CAST('2020-01-01 00:00:00' AS Nullable(DateTime64(6))) AS "test_VALID_from",
    CAST(NULL AS Nullable(DateTime64(6))) AS "test_valid_to"
  FROM "joined"
  WHERE
    (
      NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
    )
    AND (
      "joined"."name" <> "joined"."t_name"
      OR (
        "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
      )
      OR (
        NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
      )
      OR "joined"."price" <> "joined"."t_price"
      OR (
        "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
      )
      OR (
        NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
      )
    )
)
SELECT "id", "name", "price", "test_VALID_from", "test_valid_to" FROM "static" UNION ALL SELECT "id", "name", "price", "test_VALID_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id", "name", "price", "test_VALID_from", "test_valid_to" FROM "inserted_rows" SETTINGS join_use_nulls = 1
    """,
        dialect=adapter.dialect,
    ).sql(adapter.dialect)


def test_insert_overwrite_by_condition_replace_partitioned(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.return_value = make_temp_table_name(table_name, "abcd")

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = "dateTrunc('WEEK', ds)"

    insert_table_name = make_temp_table_name("new_records", "abcd")
    existing_table_name = make_temp_table_name("existing_records", "abcd")

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table_name}"),
        {
            "id": exp.DataType.build("Int8", dialect="clickhouse"),
            "ds": exp.DataType.build("Date", dialect="clickhouse"),
        },
        existing_table_name,
    )

    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT * FROM "__temp_new_records_abcd") AS "_subquery"',
        'EXCHANGE TABLES "__temp_existing_records_abcd" AND "__temp_target_abcd"',
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
    ]


def test_insert_overwrite_by_condition_replace(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.return_value = make_temp_table_name(table_name, "abcd")

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = None

    insert_table_name = make_temp_table_name("new_records", "abcd")
    existing_table_name = make_temp_table_name("existing_records", "abcd")

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table_name}"),
        {
            "id": exp.DataType.build("Int8", dialect="clickhouse"),
            "ds": exp.DataType.build("Date", dialect="clickhouse"),
        },
        existing_table_name,
    )

    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
    )

    to_sql_calls(adapter) == [
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT * FROM "__temp_new_records_abcd") AS "_subquery"',
        'EXCHANGE TABLES "__temp_existing_records_abcd" AND "__temp_target_abcd"',
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
    ]


def test_insert_overwrite_by_condition_where_partitioned(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.return_value = make_temp_table_name(table_name, "abcd")

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = "dateTrunc('WEEK', ds)"

    fetchall_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchall")
    fetchall_mock.side_effect = [
        [("1",), ("2",), ("3",), ("4",)],
        ["1", "2", "4"],
    ]

    insert_table_name = make_temp_table_name("new_records", "abcd")
    existing_table_name = make_temp_table_name("existing_records", "abcd")

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table_name}"),
        {
            "id": exp.DataType.build("Int8", dialect="clickhouse"),
            "ds": exp.DataType.build("Date", dialect="clickhouse"),
        },
        existing_table_name,
    )

    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
        exp.Between(
            this=exp.column("ds"),
            low=parse_one("'2024-02-15'"),
            high=parse_one("'2024-04-30'"),
        ),
    )

    to_sql_calls(adapter) == [
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        """INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT * FROM "__temp_new_records_abcd") AS "_subquery" WHERE "ds" BETWEEN '2024-02-15' AND '2024-04-30'""",
        """CREATE TABLE IF NOT EXISTS "__temp_target_abcd" ENGINE=MergeTree ORDER BY () AS SELECT DISTINCT "partition_id" FROM (SELECT "_partition_id" AS "partition_id" FROM "__temp_existing_records_abcd" WHERE "ds" BETWEEN '2024-02-15' AND '2024-04-30' UNION DISTINCT SELECT "_partition_id" AS "partition_id" FROM "__temp_target_abcd") AS "_affected_partitions\"""",
        """INSERT INTO "__temp_target_abcd" SELECT "id", "ds" FROM "__temp_existing_records_abcd" WHERE NOT ("ds" BETWEEN '2024-02-15' AND '2024-04-30') AND "_partition_id" IN (SELECT "partition_id" FROM "__temp_target_abcd")""",
        """ALTER TABLE "__temp_existing_records_abcd" REPLACE PARTITION ID '1' FROM "__temp_target_abcd", REPLACE PARTITION ID '2' FROM "__temp_target_abcd", REPLACE PARTITION ID '4' FROM "__temp_target_abcd", DROP PARTITION ID '3'""",
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
    ]


def test_insert_overwrite_by_condition_by_key(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.return_value = make_temp_table_name(table_name, "abcd")

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = None

    insert_table_name = make_temp_table_name("new_records", "abcd")
    existing_table_name = make_temp_table_name("existing_records", "abcd")

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table_name}"),
        {
            "id": exp.DataType.build("Int8", dialect="clickhouse"),
            "ds": exp.DataType.build("Date", dialect="clickhouse"),
        },
        existing_table_name,
    )
    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
        dynamic_key=[exp.column("id")],
        dynamic_key_exp=exp.column("id"),
        dynamic_key_unique=True,
    )

    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
        dynamic_key=[exp.column("id")],
        dynamic_key_exp=exp.column("id"),
        dynamic_key_unique=False,
    )

    to_sql_calls(adapter) == [
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT DISTINCT ON ("id") * FROM "__temp_new_records_abcd") AS "_subquery"',
        'INSERT INTO "__temp_target_abcd" SELECT "id", "ds" FROM "__temp_existing_records_abcd" WHERE NOT ("id" IN (SELECT "id" FROM "__temp_target_abcd"))',
        'EXCHANGE TABLES "__temp_existing_records_abcd" AND "__temp_target_abcd"',
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT * FROM "__temp_new_records_abcd") AS "_subquery"',
        'INSERT INTO "__temp_target_abcd" SELECT "id", "ds" FROM "__temp_existing_records_abcd" WHERE NOT ("id" IN (SELECT "id" FROM "__temp_target_abcd"))',
        'EXCHANGE TABLES "__temp_existing_records_abcd" AND "__temp_target_abcd"',
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
    ]


def test_insert_overwrite_by_condition_by_key_partitioned(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.return_value = make_temp_table_name(table_name, "abcd")

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.side_effect = ["dateTrunc('WEEK', ds)", "dateTrunc('WEEK', ds)"]

    fetchall_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchall")
    fetchall_mock.side_effect = [
        [("1",), ("2",), ("3",), ("4",)],
        ["1", "2", "4"],
        [("1",), ("2",), ("3",), ("4",)],
        ["1", "2", "4"],
    ]

    insert_table_name = make_temp_table_name("new_records", "abcd")
    existing_table_name = make_temp_table_name("existing_records", "abcd")

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table_name}"),
        {
            "id": exp.DataType.build("Int8", dialect="clickhouse"),
            "ds": exp.DataType.build("Date", dialect="clickhouse"),
        },
        existing_table_name,
    )
    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
        dynamic_key=[exp.column("id")],
        dynamic_key_exp=exp.column("id"),
        dynamic_key_unique=True,
    )

    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
        dynamic_key=[exp.column("id")],
        dynamic_key_exp=exp.column("id"),
        dynamic_key_unique=False,
    )

    to_sql_calls(adapter) == [
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT DISTINCT ON ("id") * FROM "__temp_new_records_abcd") AS "_subquery"',
        'CREATE TABLE IF NOT EXISTS "__temp_target_abcd" ENGINE=MergeTree ORDER BY () AS SELECT DISTINCT "partition_id" FROM (SELECT "_partition_id" AS "partition_id" FROM "__temp_existing_records_abcd" WHERE "id" IN (SELECT "id" FROM "__temp_target_abcd") UNION DISTINCT SELECT "_partition_id" AS "partition_id" FROM "__temp_target_abcd") AS "_affected_partitions"',
        'INSERT INTO "__temp_target_abcd" SELECT "id", "ds" FROM "__temp_existing_records_abcd" WHERE NOT ("id" IN (SELECT "id" FROM "__temp_target_abcd")) AND "_partition_id" IN (SELECT "partition_id" FROM "__temp_target_abcd")',
        """ALTER TABLE "__temp_existing_records_abcd" REPLACE PARTITION ID '2' FROM "__temp_target_abcd", REPLACE PARTITION ID '1' FROM "__temp_target_abcd", REPLACE PARTITION ID '4' FROM "__temp_target_abcd", DROP PARTITION ID '3'""",
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT * FROM "__temp_new_records_abcd") AS "_subquery"',
        'CREATE TABLE IF NOT EXISTS "__temp_target_abcd" ENGINE=MergeTree ORDER BY () AS SELECT DISTINCT "partition_id" FROM (SELECT "_partition_id" AS "partition_id" FROM "__temp_existing_records_abcd" WHERE "id" IN (SELECT "id" FROM "__temp_target_abcd") UNION DISTINCT SELECT "_partition_id" AS "partition_id" FROM "__temp_target_abcd") AS "_affected_partitions"',
        'INSERT INTO "__temp_target_abcd" SELECT "id", "ds" FROM "__temp_existing_records_abcd" WHERE NOT ("id" IN (SELECT "id" FROM "__temp_target_abcd")) AND "_partition_id" IN (SELECT "partition_id" FROM "__temp_target_abcd")',
        """ALTER TABLE "__temp_existing_records_abcd" REPLACE PARTITION ID '2' FROM "__temp_target_abcd", REPLACE PARTITION ID '1' FROM "__temp_target_abcd", REPLACE PARTITION ID '4' FROM "__temp_target_abcd", DROP PARTITION ID '3'""",
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
    ]


def test_insert_overwrite_by_condition_inc_by_partition(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_mock.return_value = make_temp_table_name(table_name, "abcd")

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchone")
    fetchone_mock.return_value = "dateTrunc('WEEK', ds)"

    fetchall_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.fetchall")
    fetchall_mock.return_value = [("1",), ("2",), ("4",)]

    insert_table_name = make_temp_table_name("new_records", "abcd")
    existing_table_name = make_temp_table_name("existing_records", "abcd")

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table_name}"),
        {
            "id": exp.DataType.build("Int8", dialect="clickhouse"),
            "ds": exp.DataType.build("Date", dialect="clickhouse"),
        },
        existing_table_name,
    )
    adapter._insert_overwrite_by_condition(
        existing_table_name.sql(),
        source_queries,
        columns_to_types,
        keep_existing_partition_rows=False,
    )

    to_sql_calls(adapter) == [
        "CREATE TABLE __temp_target_abcd AS __temp_existing_records_abcd",
        'INSERT INTO "__temp_target_abcd" ("id", "ds") SELECT "id", "ds" FROM (SELECT * FROM "__temp_new_records_abcd") AS "_subquery"',
        """ALTER TABLE "__temp_existing_records_abcd" REPLACE PARTITION ID '1' FROM "__temp_target_abcd", REPLACE PARTITION ID '2' FROM "__temp_target_abcd", REPLACE PARTITION ID '4' FROM "__temp_target_abcd\"""",
        'DROP TABLE IF EXISTS "__temp_target_abcd"',
    ]


def test_to_time_column():
    # we should get DateTime64(6) back for any temporal type other than explicit DateTime64
    expressions = d.parse(
        """
        MODEL (
            name db.table,
            kind INCREMENTAL_BY_TIME_RANGE(
                time_column (ds)
            ),
            dialect clickhouse
        );

        SELECT ds::datetime
    """
    )
    model = load_sql_based_model(expressions)
    assert (
        model.convert_to_time_column("2022-01-01 00:00:00.000001").sql("clickhouse")
        == "CAST('2022-01-01 00:00:00.000001' AS DateTime64(6))"
    )

    # We should respect the user's DateTime64 precision if specified
    expressions = d.parse(
        """
        MODEL (
            name db.table,
            kind INCREMENTAL_BY_TIME_RANGE(
                time_column (ds)
            ),
            dialect clickhouse
        );

        SELECT ds::DateTime64(4)
    """
    )
    model = load_sql_based_model(expressions)
    assert (
        model.convert_to_time_column("2022-01-01 00:00:00.000001").sql("clickhouse")
        == "CAST('2022-01-01 00:00:00.000001' AS DateTime64(4))"
    )

    # We should respect the user's DateTime64 precision if specified, even if we're making it nullable
    from sqlmesh.utils.date import to_time_column

    expressions = d.parse(
        """
        MODEL (
            name db.table,
            kind INCREMENTAL_BY_TIME_RANGE(
                time_column (ds)
            ),
            dialect clickhouse
        );

        SELECT ds::DateTime64(4)
    """
    )
    model = load_sql_based_model(expressions)
    assert (
        to_time_column(
            "2022-01-01 00:00:00.000001",
            exp.DataType.build("DateTime64(4)", dialect="clickhouse"),
            dialect="clickhouse",
            nullable=True,
        ).sql("clickhouse")
        == "CAST('2022-01-01 00:00:00.000001' AS Nullable(DateTime64(4)))"
    )


def test_exchange_tables(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    from clickhouse_connect.driver.exceptions import DatabaseError  # type: ignore

    adapter = make_mocked_engine_adapter(ClickhouseEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    temp_table_mock.return_value = make_temp_table_name("table1", "abcd")

    execute_mock = mocker.patch("sqlmesh.core.engine_adapter.ClickhouseEngineAdapter.execute")
    execute_mock.side_effect = [
        DatabaseError(
            "DB::Exception: Moving tables between databases of different engines is not supported. (NOT_IMPLEMENTED)"
        ),
        None,
        None,
        None,
    ]

    adapter._exchange_tables("table1", "table2")

    # The EXCHANGE TABLES call errored, so we RENAME TABLE instead
    assert [
        quote_identifiers(call.args[0]).sql("clickhouse")
        if isinstance(call.args[0], exp.Expr)
        else call.args[0]
        for call in execute_mock.call_args_list
    ] == [
        'EXCHANGE TABLES "table1" AND "table2"',
        'RENAME TABLE "table1" TO "__temp_table1_abcd"',
        'RENAME TABLE "table2" TO "table1"',
        'DROP TABLE IF EXISTS "__temp_table1_abcd"',
    ]
