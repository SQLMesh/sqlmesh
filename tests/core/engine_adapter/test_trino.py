import typing as t
from unittest.mock import MagicMock

import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one

import sqlmesh.core.dialect as d
from sqlmesh.core.config.connection import TrinoConnectionConfig
from sqlmesh.core.engine_adapter import TrinoEngineAdapter
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.model.definition import SqlModel
from sqlmesh.core.dialect import schema_
from sqlmesh.utils.date import to_ds
from sqlmesh.utils.errors import SQLMeshError
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.engine, pytest.mark.trino]


@pytest.fixture
def trino_mocked_engine_adapter(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> TrinoEngineAdapter:
    def mock_catalog_type(catalog_name):
        if "iceberg" in catalog_name:
            return "iceberg"
        if "delta_lake" in catalog_name:
            return "delta_lake"
        return "hive"

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_catalog_type",
        side_effect=mock_catalog_type,
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter._block_until_table_exists",
        return_value=True,
    )

    return make_mocked_engine_adapter(TrinoEngineAdapter)


def test_set_current_catalog(trino_mocked_engine_adapter: TrinoEngineAdapter):
    adapter = trino_mocked_engine_adapter
    adapter.set_current_catalog("test_catalog")

    assert to_sql_calls(adapter) == [
        'USE "test_catalog"."information_schema"',
    ]


@pytest.mark.parametrize("storage_type", ["iceberg", "delta_lake"])
def test_get_catalog_type(
    trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture, storage_type: str
):
    adapter = trino_mocked_engine_adapter
    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="system",
    )

    assert adapter.current_catalog_type == "hive"
    assert adapter.get_catalog_type("foo") == TrinoEngineAdapter.DEFAULT_CATALOG_TYPE
    assert adapter.get_catalog_type("datalake_hive") == "hive"
    assert adapter.get_catalog_type("datalake_iceberg") == "iceberg"
    assert adapter.get_catalog_type("datalake_delta_lake") == "delta_lake"

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value=f"system_{storage_type}",
    )
    assert adapter.current_catalog_type == storage_type


def test_get_catalog_type_cached(
    trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture
):
    adapter = trino_mocked_engine_adapter

    mocker.stop(t.cast(MagicMock, adapter.get_catalog_type))  # to make mypy happy

    def mock_fetchone(sql):
        if "iceberg" in sql:
            return ("iceberg",)
        return ("hive",)

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.fetchone", side_effect=mock_fetchone
    )

    fetchone_mock = t.cast(MagicMock, adapter.fetchone)  # to make mypy happy

    adapter.get_catalog_type("datalake_iceberg")
    adapter.get_catalog_type("datalake_iceberg")
    adapter.get_catalog_type("datalake_iceberg")
    assert fetchone_mock.call_count == 1

    adapter.get_catalog_type("datalake")
    assert fetchone_mock.call_count == 2

    adapter.get_catalog_type("datalake_iceberg")
    assert fetchone_mock.call_count == 2


@pytest.mark.parametrize("storage_type", ["hive", "delta_lake"])
def test_partitioned_by_hive_delta(
    trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture, storage_type: str
):
    adapter = trino_mocked_engine_adapter

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value=f"datalake_{storage_type}",
    )
    assert adapter.get_catalog_type(f"datalake_{storage_type}") == storage_type

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }

    adapter.create_table("test_table", columns_to_types, partitioned_by=[exp.to_column("colb")])

    adapter.ctas("test_table", parse_one("select 1"), partitioned_by=[exp.to_column("colb")])  # type: ignore

    assert to_sql_calls(adapter) == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("cola" INTEGER, "colb" VARCHAR) WITH (PARTITIONED_BY=ARRAY['colb'])""",
        """CREATE TABLE IF NOT EXISTS "test_table" WITH (PARTITIONED_BY=ARRAY['colb']) AS SELECT 1""",
    ]


def test_partitioned_by_iceberg(
    trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture
):
    adapter = trino_mocked_engine_adapter

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="datalake_iceberg",
    )
    assert adapter.get_catalog_type("datalake_iceberg") == "iceberg"

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }

    adapter.create_table("test_table", columns_to_types, partitioned_by=[exp.to_column("colb")])

    adapter.ctas("test_table", parse_one("select 1"), partitioned_by=[exp.to_column("colb")])  # type: ignore

    assert to_sql_calls(adapter) == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("cola" INTEGER, "colb" VARCHAR) WITH (PARTITIONING=ARRAY['colb'])""",
        """CREATE TABLE IF NOT EXISTS "test_table" WITH (PARTITIONING=ARRAY['colb']) AS SELECT 1""",
    ]


def test_partitioned_by_iceberg_transforms(
    trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture
):
    adapter = trino_mocked_engine_adapter

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="datalake_iceberg",
    )

    expressions = d.parse(
        """
        MODEL (
            name test_table,
            partitioned_by (day(cola), truncate(colb, 8), colc),
            kind INCREMENTAL_BY_TIME_RANGE(
                time_column cola,
            ),
        );

        SELECT 1::timestamp AS cola, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    adapter.create_table(
        table_name=model.view_name,
        target_columns_to_types=model.columns_to_types_or_raise,
        partitioned_by=model.partitioned_by,
    )

    adapter.ctas(
        table_name=model.view_name,
        query_or_df=t.cast(exp.Query, model.query),
        partitioned_by=model.partitioned_by,
    )

    assert to_sql_calls(adapter) == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("cola" TIMESTAMP, "colb" VARCHAR, "colc" VARCHAR) WITH (PARTITIONING=ARRAY['DAY("cola")', 'TRUNCATE("colb", 8)', '"colc"'])""",
        """CREATE TABLE IF NOT EXISTS "test_table" WITH (PARTITIONING=ARRAY['DAY("cola")', 'TRUNCATE("colb", 8)', '"colc"']) AS SELECT CAST(1 AS TIMESTAMP) AS "cola", CAST(2 AS VARCHAR) AS "colb", \'foo\' AS "colc\"""",
    ]


def test_partitioned_by_with_multiple_catalogs_same_server(
    trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture
):
    adapter = trino_mocked_engine_adapter

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="system",
    )

    adapter.create_table(
        "datalake.test_schema.test_table", columns_to_types, partitioned_by=[exp.to_column("colb")]
    )

    adapter.ctas(
        "datalake.test_schema.test_table",
        parse_one("select 1"),  # type: ignore
        partitioned_by=[exp.to_column("colb")],
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="iceberg",
    )

    adapter.create_table(
        "datalake_iceberg.test_schema.test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("colb")],
    )

    adapter.ctas(
        "datalake_iceberg.test_schema.test_table",
        parse_one("select 1"),  # type: ignore
        partitioned_by=[exp.to_column("colb")],
    )

    assert to_sql_calls(adapter) == [
        """CREATE TABLE IF NOT EXISTS "datalake"."test_schema"."test_table" ("cola" INTEGER, "colb" VARCHAR) WITH (PARTITIONED_BY=ARRAY['colb'])""",
        """CREATE TABLE IF NOT EXISTS "datalake"."test_schema"."test_table" WITH (PARTITIONED_BY=ARRAY['colb']) AS SELECT 1""",
        """CREATE TABLE IF NOT EXISTS "datalake_iceberg"."test_schema"."test_table" ("cola" INTEGER, "colb" VARCHAR) WITH (PARTITIONING=ARRAY['colb'])""",
        """CREATE TABLE IF NOT EXISTS "datalake_iceberg"."test_schema"."test_table" WITH (PARTITIONING=ARRAY['colb']) AS SELECT 1""",
    ]


def test_comments_hive(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(TrinoEngineAdapter)

    current_catalog_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog"
    )
    current_catalog_mock.return_value = "hive"
    catalog_type_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_catalog_type"
    )
    catalog_type_mock.return_value = "hive"

    allowed_table_comment_length = TrinoEngineAdapter.MAX_TABLE_COMMENT_LENGTH
    truncated_table_comment = "a" * allowed_table_comment_length
    long_table_comment = truncated_table_comment + "b"

    allowed_column_comment_length = TrinoEngineAdapter.MAX_COLUMN_COMMENT_LENGTH
    truncated_column_comment = "c" * allowed_column_comment_length
    long_column_comment = truncated_column_comment + "d"

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM source_table"),
        table_description=long_table_comment,
    )

    adapter._create_table_comment(
        "test_table",
        long_table_comment,
    )

    adapter._create_column_comments(
        "test_table",
        {"a": long_column_comment},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        f"""CREATE TABLE IF NOT EXISTS "test_table" ("a" INTEGER COMMENT '{truncated_column_comment}', "b" INTEGER) COMMENT '{truncated_table_comment}'""",
        'DESCRIBE "test_table"',
        f"""CREATE TABLE IF NOT EXISTS "test_table" COMMENT '{truncated_table_comment}' AS SELECT CAST("a" AS INTEGER) AS "a", CAST("b" AS INTEGER) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        'DESCRIBE "test_table"',
        f"""COMMENT ON COLUMN "test_table"."a" IS '{truncated_column_comment}'""",
        """CREATE OR REPLACE VIEW test_view AS SELECT a, b FROM source_table""",
        f"""COMMENT ON VIEW "test_view" IS '{truncated_table_comment}'""",
        f"""COMMENT ON TABLE "test_table" IS '{truncated_table_comment}'""",
        f"""COMMENT ON COLUMN "test_table"."a" IS '{truncated_column_comment}'""",
    ]


@pytest.mark.parametrize("storage_type", ["iceberg", "delta_lake"])
def test_comments_iceberg_delta(
    mocker: MockerFixture, make_mocked_engine_adapter: t.Callable, storage_type: str
):
    adapter = make_mocked_engine_adapter(TrinoEngineAdapter)

    current_catalog_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog"
    )
    current_catalog_mock.return_value = storage_type
    catalog_type_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_catalog_type"
    )
    catalog_type_mock.return_value = storage_type

    allowed_table_comment_length = TrinoEngineAdapter.MAX_TABLE_COMMENT_LENGTH
    truncated_table_comment = "a" * allowed_table_comment_length
    long_table_comment = truncated_table_comment + "b"

    allowed_column_comment_length = TrinoEngineAdapter.MAX_COLUMN_COMMENT_LENGTH
    truncated_column_comment = "c" * allowed_column_comment_length
    long_column_comment = truncated_column_comment + "d"

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM source_table"),
        table_description=long_table_comment,
    )

    adapter._create_table_comment(
        "test_table",
        long_table_comment,
    )

    adapter._create_column_comments(
        "test_table",
        {"a": long_column_comment},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        f"""CREATE TABLE IF NOT EXISTS "test_table" ("a" INTEGER COMMENT '{long_column_comment}', "b" INTEGER) COMMENT '{long_table_comment}'""",
        f"""CREATE TABLE IF NOT EXISTS "test_table" COMMENT '{long_table_comment}' AS SELECT CAST("a" AS INTEGER) AS "a", CAST("b" AS INTEGER) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        f"""COMMENT ON COLUMN "test_table"."a" IS '{long_column_comment}'""",
        """CREATE OR REPLACE VIEW test_view AS SELECT a, b FROM source_table""",
        f"""COMMENT ON VIEW "test_view" IS '{long_table_comment}'""",
        f"""COMMENT ON TABLE "test_table" IS '{long_table_comment}'""",
        f"""COMMENT ON COLUMN "test_table"."a" IS '{long_column_comment}'""",
    ]


def test_delta_timestamps(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(TrinoEngineAdapter)

    ts6 = exp.DataType.build("timestamp(6)")
    ts3_tz = exp.DataType.build("timestamp(3) with time zone")

    columns_to_types = {
        "ts": exp.DataType.build("TIMESTAMP"),
        "ts_1": exp.DataType.build("TIMESTAMP(1)"),
        "ts_tz": exp.DataType.build("TIMESTAMP WITH TIME ZONE"),
        "ts_tz_1": exp.DataType.build("TIMESTAMP(1) WITH TIME ZONE"),
    }

    delta_columns_to_types = adapter._to_delta_ts(columns_to_types)

    assert delta_columns_to_types == {
        "ts": ts6,
        "ts_1": ts6,
        "ts_tz": ts3_tz,
        "ts_tz_1": ts3_tz,
    }


def test_timestamp_mapping():
    """Test that timestamp_mapping config property is properly defined and accessible."""
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
    )

    assert config._connection_factory_with_kwargs.keywords["source"] == "sqlmesh"

    adapter = config.create_engine_adapter()
    assert adapter.timestamp_mapping is None

    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        source="my_source",
        timestamp_mapping={
            "TIMESTAMP": "TIMESTAMP(6)",
            "TIMESTAMP(3)": "TIMESTAMP WITH TIME ZONE",
        },
    )
    assert config._connection_factory_with_kwargs.keywords["source"] == "my_source"
    adapter = config.create_engine_adapter()
    assert adapter.timestamp_mapping is not None
    assert adapter.timestamp_mapping[exp.DataType.build("TIMESTAMP")] == exp.DataType.build(
        "TIMESTAMP(6)"
    )


def test_delta_timestamps_with_custom_mapping(make_mocked_engine_adapter: t.Callable):
    """Test that _apply_timestamp_mapping + _to_delta_ts respects custom timestamp_mapping."""
    # Create config with custom timestamp mapping
    # Mapped columns are skipped by _to_delta_ts
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        timestamp_mapping={
            "TIMESTAMP": "TIMESTAMP(3)",
            "TIMESTAMP(1)": "TIMESTAMP(3)",
            "TIMESTAMP WITH TIME ZONE": "TIMESTAMP(6) WITH TIME ZONE",
            "TIMESTAMP(1) WITH TIME ZONE": "TIMESTAMP(6) WITH TIME ZONE",
        },
    )

    adapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, timestamp_mapping=config.timestamp_mapping
    )

    ts3 = exp.DataType.build("timestamp(3)")
    ts6_tz = exp.DataType.build("timestamp(6) with time zone")

    columns_to_types = {
        "ts": exp.DataType.build("TIMESTAMP"),
        "ts_1": exp.DataType.build("TIMESTAMP(1)"),
        "ts_tz": exp.DataType.build("TIMESTAMP WITH TIME ZONE"),
        "ts_tz_1": exp.DataType.build("TIMESTAMP(1) WITH TIME ZONE"),
    }

    # Apply mapping first, then convert to delta types (skipping mapped columns)
    mapped_columns_to_types, mapped_column_names = adapter._apply_timestamp_mapping(
        columns_to_types
    )
    delta_columns_to_types = adapter._to_delta_ts(mapped_columns_to_types, mapped_column_names)

    # All types were mapped, so _to_delta_ts skips them - they keep their mapped types
    assert delta_columns_to_types == {
        "ts": ts3,
        "ts_1": ts3,
        "ts_tz": ts6_tz,
        "ts_tz_1": ts6_tz,
    }


def test_delta_timestamps_with_partial_mapping(make_mocked_engine_adapter: t.Callable):
    """Test that _apply_timestamp_mapping + _to_delta_ts uses custom mapping for specified types."""
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        timestamp_mapping={
            "TIMESTAMP": "TIMESTAMP(3)",
        },
    )

    adapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, timestamp_mapping=config.timestamp_mapping
    )

    ts3 = exp.DataType.build("TIMESTAMP(3)")
    ts6 = exp.DataType.build("timestamp(6)")
    ts3_tz = exp.DataType.build("timestamp(3) with time zone")

    columns_to_types = {
        "ts": exp.DataType.build("TIMESTAMP"),
        "ts_1": exp.DataType.build("TIMESTAMP(1)"),
        "ts_tz": exp.DataType.build("TIMESTAMP WITH TIME ZONE"),
    }

    # Apply mapping first, then convert to delta types (skipping mapped columns)
    mapped_columns_to_types, mapped_column_names = adapter._apply_timestamp_mapping(
        columns_to_types
    )
    delta_columns_to_types = adapter._to_delta_ts(mapped_columns_to_types, mapped_column_names)

    # TIMESTAMP is in mapping → TIMESTAMP(3), skipped by _to_delta_ts
    # TIMESTAMP(1) is NOT in mapping, uses default TIMESTAMP → ts6
    # TIMESTAMP WITH TIME ZONE is NOT in mapping, uses default TIMESTAMPTZ → ts3_tz
    assert delta_columns_to_types == {
        "ts": ts3,  # Mapped to TIMESTAMP(3), skipped by _to_delta_ts
        "ts_1": ts6,  # Not in mapping, uses default
        "ts_tz": ts3_tz,  # Not in mapping, uses default
    }


def test_table_format(trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture):
    adapter = trino_mocked_engine_adapter
    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="iceberg",
    )

    expressions = d.parse(
        """
        MODEL (
            name iceberg.test_table,
            kind FULL,
            table_format iceberg,
            storage_format orc
        );

        SELECT 1::timestamp AS cola, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
        storage_format=model.storage_format,
    )

    adapter.ctas(
        table_name=model.name,
        query_or_df=t.cast(exp.Query, model.query),
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
        storage_format=model.storage_format,
    )

    # Trino needs to ignore the `table_format` property because to create Iceberg tables, you target an Iceberg catalog
    # rather than explicitly telling it to create an Iceberg table. So this is testing that `FORMAT='ORC'` is output
    # instead of `FORMAT='ICEBERG'` which would be invalid
    assert to_sql_calls(adapter) == [
        """CREATE TABLE IF NOT EXISTS "iceberg"."test_table" ("cola" TIMESTAMP, "colb" VARCHAR, "colc" VARCHAR) WITH (format='orc')""",
        '''CREATE TABLE IF NOT EXISTS "iceberg"."test_table" WITH (format='orc') AS SELECT CAST("cola" AS TIMESTAMP) AS "cola", CAST("colb" AS VARCHAR) AS "colb", CAST("colc" AS VARCHAR) AS "colc" FROM (SELECT CAST(1 AS TIMESTAMP) AS "cola", CAST(2 AS VARCHAR) AS "colb", \'foo\' AS "colc") AS "_subquery"''',
    ]


def test_table_location(trino_mocked_engine_adapter: TrinoEngineAdapter, mocker: MockerFixture):
    adapter = trino_mocked_engine_adapter
    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_current_catalog",
        return_value="iceberg",
    )

    expressions = d.parse(
        """
        MODEL (
            name iceberg.test_table,
            kind FULL,
            physical_properties (
                location = 'hdfs://some/table/location'
            )
        );

        SELECT 1::timestamp AS cola, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    adapter.ctas(
        table_name=model.name,
        query_or_df=t.cast(exp.Query, model.query),
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "iceberg"."test_table" ("cola" TIMESTAMP, "colb" VARCHAR, "colc" VARCHAR) WITH (location=\'hdfs://some/table/location\')',
        'CREATE TABLE IF NOT EXISTS "iceberg"."test_table" WITH (location=\'hdfs://some/table/location\') AS SELECT CAST("cola" AS TIMESTAMP) AS "cola", CAST("colb" AS VARCHAR) AS "colb", CAST("colc" AS VARCHAR) AS "colc" FROM (SELECT CAST(1 AS TIMESTAMP) AS "cola", CAST(2 AS VARCHAR) AS "colb", \'foo\' AS "colc") AS "_subquery"',
    ]


def test_schema_location_mapping():
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
    )

    adapter = config.create_engine_adapter()
    assert adapter.schema_location_mapping is None
    assert adapter._schema_location("foo") is None

    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        schema_location_mapping={
            "^utils$": "s3://utils-bucket/@{schema_name}",
            "^landing\\..*$": "s3://raw-data/@{catalog_name}/@{schema_name}",
            "^staging.*$": "s3://bucket/@{schema_name}_dev",
            "^sqlmesh.*$": "s3://sqlmesh-internal/dev/@{schema_name}",
        },
    )
    adapter = config.create_engine_adapter()
    assert adapter.schema_location_mapping is not None
    assert adapter._schema_location("foo") is None
    assert adapter._schema_location("utils_dev") is None
    assert adapter._schema_location("utils") == "s3://utils-bucket/utils"
    assert adapter._schema_location("staging_customers") == "s3://bucket/staging_customers_dev"
    assert adapter._schema_location("staging_accounts") == "s3://bucket/staging_accounts_dev"
    assert (
        adapter._schema_location("sqlmesh__staging_customers")
        == "s3://sqlmesh-internal/dev/sqlmesh__staging_customers"
    )
    assert (
        adapter._schema_location("sqlmesh__staging_utils")
        == "s3://sqlmesh-internal/dev/sqlmesh__staging_utils"
    )
    assert adapter._schema_location("landing.transactions") == "s3://raw-data/landing/transactions"
    assert (
        adapter._schema_location(schema_("transactions", "landing"))
        == "s3://raw-data/landing/transactions"
    )
    assert (
        adapter._schema_location('"landing"."transactions"') == "s3://raw-data/landing/transactions"
    )


def test_create_schema_sets_location(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter.get_catalog_type",
        return_value="iceberg",
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.trino.TrinoEngineAdapter._block_until_table_exists",
        return_value=True,
    )

    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        schema_location_mapping={
            "^utils$": "s3://utils-bucket/@{schema_name}",
            "^landing\\..*$": "s3://raw-data/@{catalog_name}/@{schema_name}",
            "^staging.*$": "s3://bucket/@{schema_name}_dev",
            "^sqlmesh.*$": "s3://sqlmesh-internal/dev/@{schema_name}",
            "^iceberg\\.staging.*$": "s3://iceberg-catalog/foo_@{schema_name}",
        },
    )

    adapter: TrinoEngineAdapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, schema_location_mapping=config.schema_location_mapping
    )

    adapter.create_schema("foo")
    adapter.create_schema(schema_("utils_dev", "db"))
    adapter.create_schema(schema_("utils", "db"))
    adapter.create_schema(schema_("utils"))
    adapter.create_schema(schema_("sqlmesh"))
    adapter.create_schema("sqlmesh__staging")
    adapter.create_schema(schema_("snapshots", "sqlmesh"))
    adapter.create_schema(schema_("staging_foo"))
    adapter.create_schema(schema_("staging_bar", "iceberg"))
    adapter.create_schema('"catalog"."staging_customers"')
    adapter.create_schema(schema_("transactions", "landing"))

    assert (
        to_sql_calls(adapter)
        == [
            'CREATE SCHEMA IF NOT EXISTS "foo"',  # no match
            'CREATE SCHEMA IF NOT EXISTS "db"."utils_dev"',  # no match
            'CREATE SCHEMA IF NOT EXISTS "db"."utils"',  # no match on '^utils$' because of catalog
            "CREATE SCHEMA IF NOT EXISTS \"utils\" WITH (LOCATION='s3://utils-bucket/utils')",  # match '^utils$'
            "CREATE SCHEMA IF NOT EXISTS \"sqlmesh\" WITH (LOCATION='s3://sqlmesh-internal/dev/sqlmesh')",  # match '^sqlmesh.*$'
            "CREATE SCHEMA IF NOT EXISTS \"sqlmesh__staging\" WITH (LOCATION='s3://sqlmesh-internal/dev/sqlmesh__staging')",  # match '^sqlmesh.*$'
            'CREATE SCHEMA IF NOT EXISTS "sqlmesh"."snapshots" WITH (LOCATION=\'s3://sqlmesh-internal/dev/snapshots\')',  # match '^sqlmesh.*$' on the catalog
            "CREATE SCHEMA IF NOT EXISTS \"staging_foo\" WITH (LOCATION='s3://bucket/staging_foo_dev')",  # match '^staging.*$'
            'CREATE SCHEMA IF NOT EXISTS "iceberg"."staging_bar" WITH (LOCATION=\'s3://iceberg-catalog/foo_staging_bar\')',  # match '^iceberg\.staging.*$'
            'CREATE SCHEMA IF NOT EXISTS "catalog"."staging_customers"',  # no match
            'CREATE SCHEMA IF NOT EXISTS "landing"."transactions" WITH (LOCATION=\'s3://raw-data/landing/transactions\')',  # match '^landing\..*$'
        ]
    )


def test_session_authorization(trino_mocked_engine_adapter: TrinoEngineAdapter):
    adapter = trino_mocked_engine_adapter

    # Test 1: No authorization property - should not execute any authorization commands
    with adapter.session({}):
        pass

    assert to_sql_calls(adapter) == []

    # Test 2: String authorization
    with adapter.session({"authorization": "test_user"}):
        adapter.execute("SELECT 1")

    assert to_sql_calls(adapter) == [
        "SET SESSION AUTHORIZATION 'test_user'",
        "SELECT 1",
        "RESET SESSION AUTHORIZATION",
    ]

    # Test 3: Expression authorization
    adapter.cursor.execute.reset_mock()
    with adapter.session({"authorization": exp.Literal.string("another_user")}):
        adapter.execute("SELECT 2")

    assert to_sql_calls(adapter) == [
        "SET SESSION AUTHORIZATION 'another_user'",
        "SELECT 2",
        "RESET SESSION AUTHORIZATION",
    ]

    # Test 4: RESET is called even if exception occurs during session
    adapter.cursor.execute.reset_mock()
    try:
        with adapter.session({"authorization": "test_user"}):
            adapter.execute("SELECT 1")
            raise RuntimeError("Test exception")
    except RuntimeError:
        pass

    # Test 5: Invalid authorization value
    with pytest.raises(
        SQLMeshError,
        match="Invalid value for `session_properties.authorization`. Must be a string literal.",
    ):
        with adapter.session({"authorization": exp.Literal.number(1)}):
            adapter.execute("SELECT 1")

    assert to_sql_calls(adapter) == [
        "SET SESSION AUTHORIZATION 'test_user'",
        "SELECT 1",
        "RESET SESSION AUTHORIZATION",
    ]


@pytest.mark.parametrize(
    "catalog_name,expected_replace",
    [
        ("hive_catalog", False),
        ("iceberg_catalog", True),
        ("delta_catalog", False),
        ("acme_delta_lake", True),
        ("acme_iceberg", True),
        ("custom_delta_lake_something", True),
        ("my_iceberg_store", True),
        ("plain_catalog", False),
    ],
)
def test_replace_table_catalog_support(
    trino_mocked_engine_adapter: TrinoEngineAdapter, catalog_name, expected_replace
):
    adapter = trino_mocked_engine_adapter

    adapter.replace_query(
        table_name=".".join([catalog_name, "schema", "test_table"]),
        query_or_df=t.cast(exp.Query, parse_one("SELECT 1 AS col")),
    )

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 1
    if expected_replace:
        assert (
            sql_calls[0]
            == f'CREATE OR REPLACE TABLE "{catalog_name}"."schema"."test_table" AS SELECT 1 AS "col"'
        )
    else:
        assert (
            sql_calls[0]
            == f'CREATE TABLE IF NOT EXISTS "{catalog_name}"."schema"."test_table" AS SELECT 1 AS "col"'
        )


@pytest.mark.parametrize(
    "catalog_type_overrides", [{}, {"my_catalog": "hive"}, {"other_catalog": "iceberg"}]
)
def test_insert_overwrite_time_partition_hive(
    make_mocked_engine_adapter: t.Callable, catalog_type_overrides: t.Dict[str, str]
):
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        catalog_type_overrides=catalog_type_overrides,
    )
    adapter: TrinoEngineAdapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, catalog_type_overrides=config.catalog_type_overrides
    )
    adapter.fetchone = MagicMock(return_value=None)  # type: ignore

    adapter.insert_overwrite_by_time_partition(
        table_name=".".join(["my_catalog", "schema", "test_table"]),
        query_or_df=t.cast(exp.Query, parse_one("SELECT a, b FROM tbl")),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
    )

    assert to_sql_calls(adapter) == [
        "SET SESSION my_catalog.insert_existing_partitions_behavior='OVERWRITE'",
        'INSERT INTO "my_catalog"."schema"."test_table" ("a", "b") SELECT "a", "b" FROM (SELECT "a", "b" FROM "tbl") AS "_subquery" WHERE "b" BETWEEN \'2022-01-01\' AND \'2022-01-02\'',
        "SET SESSION my_catalog.insert_existing_partitions_behavior='APPEND'",
    ]


@pytest.mark.parametrize(
    "catalog_type_overrides",
    [
        {"my_catalog": "iceberg"},
        {"my_catalog": "unknown"},
    ],
)
def test_insert_overwrite_time_partition_iceberg(
    make_mocked_engine_adapter: t.Callable, catalog_type_overrides: t.Dict[str, str]
):
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        catalog_type_overrides=catalog_type_overrides,
    )
    adapter: TrinoEngineAdapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, catalog_type_overrides=config.catalog_type_overrides
    )
    adapter.fetchone = MagicMock(return_value=None)  # type: ignore

    adapter.insert_overwrite_by_time_partition(
        table_name=".".join(["my_catalog", "schema", "test_table"]),
        query_or_df=t.cast(exp.Query, parse_one("SELECT a, b FROM tbl")),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
    )

    assert to_sql_calls(adapter) == [
        'DELETE FROM "my_catalog"."schema"."test_table" WHERE "b" BETWEEN \'2022-01-01\' AND \'2022-01-02\'',
        'INSERT INTO "my_catalog"."schema"."test_table" ("a", "b") SELECT "a", "b" FROM (SELECT "a", "b" FROM "tbl") AS "_subquery" WHERE "b" BETWEEN \'2022-01-01\' AND \'2022-01-02\'',
    ]


def test_delta_timestamps_with_non_timestamp_columns(make_mocked_engine_adapter: t.Callable):
    """Test that _apply_timestamp_mapping + _to_delta_ts handles non-timestamp columns."""
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        timestamp_mapping={
            "TIMESTAMP": "TIMESTAMP(3)",
        },
    )

    adapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, timestamp_mapping=config.timestamp_mapping
    )

    ts3 = exp.DataType.build("TIMESTAMP(3)")
    ts6 = exp.DataType.build("timestamp(6)")

    columns_to_types = {
        "ts": exp.DataType.build("TIMESTAMP"),
        "ts_1": exp.DataType.build("TIMESTAMP(1)"),
        "int_col": exp.DataType.build("INT"),
        "varchar_col": exp.DataType.build("VARCHAR(100)"),
        "decimal_col": exp.DataType.build("DECIMAL(10,2)"),
    }

    # Apply mapping first, then convert to delta types (skipping mapped columns)
    mapped_columns_to_types, mapped_column_names = adapter._apply_timestamp_mapping(
        columns_to_types
    )
    delta_columns_to_types = adapter._to_delta_ts(mapped_columns_to_types, mapped_column_names)

    # TIMESTAMP is in mapping → TIMESTAMP(3), skipped by _to_delta_ts
    # TIMESTAMP(1) is NOT in mapping (exact match), uses default TIMESTAMP → ts6
    # Non-timestamp columns should pass through unchanged
    assert delta_columns_to_types == {
        "ts": ts3,  # Mapped to TIMESTAMP(3), skipped by _to_delta_ts
        "ts_1": ts6,  # Not in mapping, uses default
        "int_col": exp.DataType.build("INT"),
        "varchar_col": exp.DataType.build("VARCHAR(100)"),
        "decimal_col": exp.DataType.build("DECIMAL(10,2)"),
    }


def test_delta_timestamps_with_empty_mapping(make_mocked_engine_adapter: t.Callable):
    """Test that _to_delta_ts handles empty custom mapping dictionary."""
    config = TrinoConnectionConfig(
        user="user",
        host="host",
        catalog="catalog",
        timestamp_mapping={},
    )

    adapter = make_mocked_engine_adapter(
        TrinoEngineAdapter, timestamp_mapping=config.timestamp_mapping
    )

    ts6 = exp.DataType.build("timestamp(6)")
    ts3_tz = exp.DataType.build("timestamp(3) with time zone")

    columns_to_types = {
        "ts": exp.DataType.build("TIMESTAMP"),
        "ts_tz": exp.DataType.build("TIMESTAMP WITH TIME ZONE"),
    }

    delta_columns_to_types = adapter._to_delta_ts(columns_to_types)

    # With empty custom mapping, should fall back to defaults
    assert delta_columns_to_types == {
        "ts": ts6,
        "ts_tz": ts3_tz,
    }
