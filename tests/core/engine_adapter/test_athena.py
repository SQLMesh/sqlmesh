import typing as t
import pytest
from unittest.mock import Mock
from pytest_mock import MockerFixture
import pandas as pd  # noqa: TID253

from sqlglot import exp, parse_one
import sqlmesh.core.dialect as d
from sqlmesh.core.engine_adapter import AthenaEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObject
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.model.definition import SqlModel
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.core.table_diff import TableDiff

from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.athena, pytest.mark.engine]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable) -> AthenaEngineAdapter:
    return make_mocked_engine_adapter(AthenaEngineAdapter)


@pytest.fixture
def table_diff(adapter: AthenaEngineAdapter) -> TableDiff:
    return TableDiff(
        adapter=adapter,
        source="source_table",
        target="target_table",
        on=["id"],
    )


@pytest.mark.parametrize(
    "config_s3_warehouse_location,table_properties,table,expected_location",
    [
        # No s3_warehouse_location in config
        (None, None, exp.to_table("schema.table"), None),
        (None, {}, exp.to_table("schema.table"), None),
        (
            None,
            {"s3_base_location": exp.Literal.string("s3://some/location/")},
            exp.to_table("schema.table"),
            "s3://some/location/table/",
        ),
        # Location set to bucket
        ("s3://bucket", None, exp.to_table("schema.table"), "s3://bucket/schema/table/"),
        ("s3://bucket", {}, exp.to_table("schema.table"), "s3://bucket/schema/table/"),
        ("s3://bucket", None, exp.to_table("schema.table"), "s3://bucket/schema/table/"),
        (
            "s3://bucket",
            {"s3_base_location": exp.Literal.string("s3://some/location/")},
            exp.to_table("schema.table"),
            "s3://some/location/table/",
        ),
        ("s3://bucket", {}, exp.Table(db=exp.Identifier(this="test")), "s3://bucket/test/"),
        # Location set to bucket with prefix
        (
            "s3://bucket/subpath/",
            None,
            exp.to_table("schema.table"),
            "s3://bucket/subpath/schema/table/",
        ),
        ("s3://bucket/subpath/", None, exp.to_table("table"), "s3://bucket/subpath/table/"),
        (
            "s3://bucket/subpath/",
            None,
            exp.to_table("catalog.schema.table"),
            "s3://bucket/subpath/catalog/schema/table/",
        ),
        (
            "s3://bucket/subpath/",
            None,
            exp.Table(db=exp.Identifier(this="test")),
            "s3://bucket/subpath/test/",
        ),
    ],
)
def test_table_location(
    adapter: AthenaEngineAdapter,
    config_s3_warehouse_location: t.Optional[str],
    table_properties: t.Optional[t.Dict[str, exp.Expr]],
    table: exp.Table,
    expected_location: t.Optional[str],
) -> None:
    adapter.s3_warehouse_location = config_s3_warehouse_location
    if expected_location is None:
        with pytest.raises(SQLMeshError, match=r"Cannot figure out location for table.*"):
            adapter._table_location_or_raise(table_properties, table)
    else:
        location = adapter._table_location_or_raise(
            table_properties, table
        ).this.name  # extract the unquoted location value from the LocationProperty
        assert location == expected_location

    if table_properties is not None:
        # this get consumed by _table_location because we dont want it to end up in a TBLPROPERTIES clause
        assert "s3_base_location" not in table_properties


def test_create_schema(adapter: AthenaEngineAdapter) -> None:
    adapter.create_schema("test")

    adapter.s3_warehouse_location = "s3://base"
    adapter.create_schema("test")

    assert to_sql_calls(adapter) == [
        "CREATE SCHEMA IF NOT EXISTS `test`",
        "CREATE SCHEMA IF NOT EXISTS `test` LOCATION 's3://base/test/'",
    ]


def test_create_table_hive(adapter: AthenaEngineAdapter) -> None:
    expressions = d.parse(
        """
        MODEL (
            name test_table,
            kind FULL,
            partitioned_by (cola, colb),
            storage_format parquet,
            physical_properties (
                s3_base_location = 's3://foo',
                has_encrypted_data = 'true'
            )
        );

        SELECT 1::timestamp AS cola, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
        partitioned_by=model.partitioned_by,
        storage_format=model.storage_format,
    )

    assert to_sql_calls(adapter) == [
        "CREATE EXTERNAL TABLE IF NOT EXISTS `test_table` (`colc` STRING) PARTITIONED BY (`cola` TIMESTAMP, `colb` STRING) STORED AS PARQUET LOCATION 's3://foo/test_table/' TBLPROPERTIES ('has_encrypted_data'='true')"
    ]


def test_create_table_iceberg(adapter: AthenaEngineAdapter) -> None:
    expressions = d.parse(
        """
        MODEL (
            name test_table,
            kind FULL,
            partitioned_by (colc, bucket(16, cola)),
            table_format iceberg,
            storage_format parquet,
            physical_properties (
                s3_base_location = 's3://foo'
            )
        );

        SELECT 1::timestamp AS cola, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
        partitioned_by=model.partitioned_by,
        table_format=model.table_format,
        storage_format=model.storage_format,
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` TIMESTAMP, `colb` STRING, `colc` STRING) PARTITIONED BY (`colc`, BUCKET(16, `cola`)) LOCATION 's3://foo/test_table/' TBLPROPERTIES ('table_type'='iceberg', 'format'='parquet')"
    ]


def test_create_table_no_location(adapter: AthenaEngineAdapter) -> None:
    expressions = d.parse(
        """
        MODEL (
            name test_table,
            kind FULL
        );

        SELECT a::int FROM foo;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    with pytest.raises(SQLMeshError, match=r"Cannot figure out location.*"):
        adapter.create_table(
            model.name,
            target_columns_to_types=model.columns_to_types_or_raise,
            table_properties=model.physical_properties,
        )

    adapter.s3_warehouse_location = "s3://bucket/prefix"
    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    assert to_sql_calls(adapter) == [
        "CREATE EXTERNAL TABLE IF NOT EXISTS `test_table` (`a` INT) LOCATION 's3://bucket/prefix/test_table/'",
    ]


def test_ctas_hive(adapter: AthenaEngineAdapter):
    adapter.s3_warehouse_location = "s3://bucket/prefix/"

    adapter.ctas(
        table_name="foo.bar",
        target_columns_to_types={"a": exp.DataType.build("int")},
        query_or_df=parse_one("select 1", into=exp.Select),
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "foo"."bar" WITH (external_location=\'s3://bucket/prefix/foo/bar/\') AS SELECT CAST("a" AS INTEGER) AS "a" FROM (SELECT 1) AS "_subquery"'
    ]


def test_ctas_iceberg(adapter: AthenaEngineAdapter):
    adapter.s3_warehouse_location = "s3://bucket/prefix/"

    adapter.ctas(
        table_name="foo.bar",
        target_columns_to_types={"a": exp.DataType.build("int")},
        query_or_df=parse_one("select 1", into=exp.Select),
        table_format="iceberg",
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "foo"."bar" WITH (table_type=\'iceberg\', location=\'s3://bucket/prefix/foo/bar/\', is_external=false) AS SELECT CAST("a" AS INTEGER) AS "a" FROM (SELECT 1) AS "_subquery"'
    ]


def test_ctas_iceberg_no_specific_location(adapter: AthenaEngineAdapter):
    with pytest.raises(SQLMeshError, match=r"Cannot figure out location.*"):
        adapter.ctas(
            table_name="foo.bar",
            target_columns_to_types={"a": exp.DataType.build("int")},
            query_or_df=parse_one("select 1", into=exp.Select),
            table_properties={"table_type": exp.Literal.string("iceberg")},
        )

    assert to_sql_calls(adapter) == []


def test_ctas_iceberg_partitioned(adapter: AthenaEngineAdapter):
    expressions = d.parse(
        """
        MODEL (
            name test_table,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column business_date
            ),
            table_format iceberg,
            start '2025-01-15'
        );

        SELECT 1::timestamp AS business_date, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    adapter.s3_warehouse_location = "s3://bucket/prefix/"
    adapter.ctas(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types,
        partitioned_by=model.partitioned_by,
        query_or_df=model.ctas_query(),
        table_format=model.table_format,
    )

    assert to_sql_calls(adapter) == [
        """CREATE TABLE IF NOT EXISTS "test_table" WITH (table_type='iceberg', partitioning=ARRAY['business_date'], location='s3://bucket/prefix/test_table/', is_external=false) AS SELECT CAST("business_date" AS TIMESTAMP) AS "business_date", CAST("colb" AS VARCHAR) AS "colb", CAST("colc" AS VARCHAR) AS "colc" FROM (SELECT CAST(1 AS TIMESTAMP) AS "business_date", CAST(2 AS VARCHAR) AS "colb", 'foo' AS "colc" LIMIT 0) AS "_subquery\""""
    ]


def test_replace_query(adapter: AthenaEngineAdapter, mocker: MockerFixture):
    mocker.patch(
        "sqlmesh.core.engine_adapter.athena.AthenaEngineAdapter.table_exists", return_value=True
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.athena.AthenaEngineAdapter._query_table_type",
        return_value="iceberg",
    )
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test", type="table")],
    )

    adapter.replace_query(
        table_name="test",
        query_or_df=parse_one("select 1 as a", into=exp.Select),
        target_columns_to_types={"a": exp.DataType.build("int")},
        table_properties={},
    )

    assert to_sql_calls(adapter) == [
        'DELETE FROM "test" WHERE TRUE',
        'INSERT INTO "test" ("a") SELECT 1 AS "a"',
    ]

    mocker.patch(
        "sqlmesh.core.engine_adapter.athena.AthenaEngineAdapter.table_exists", return_value=False
    )
    mocker.patch.object(adapter, "_get_data_objects", return_value=[])
    adapter.cursor.execute.reset_mock()
    adapter._clear_data_object_cache()

    adapter.s3_warehouse_location = "s3://foo"
    adapter.replace_query(
        table_name="test",
        query_or_df=parse_one("select 1 as a", into=exp.Select),
        target_columns_to_types={"a": exp.DataType.build("int")},
        table_properties={},
    )

    # gets recreated as a Hive table because table_exists=False and nothing in the properties indicates it should be Iceberg
    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test" WITH (external_location=\'s3://foo/test/\') AS SELECT CAST("a" AS INTEGER) AS "a" FROM (SELECT 1 AS "a") AS "_subquery"'
    ]


def test_columns(adapter: AthenaEngineAdapter, mocker: MockerFixture):
    mock = mocker.patch(
        "pandas.io.sql.read_sql_query",
        return_value=pd.DataFrame(
            data=[["col1", "int"], ["col2", "varchar"]], columns=["column_name", "data_type"]
        ),
    )

    assert adapter.columns("foo.bar") == {
        "col1": exp.DataType.build("int"),
        "col2": exp.DataType.build("varchar"),
    }

    assert (
        mock.call_args_list[0][0][0]
        == """SELECT "column_name", "data_type" FROM "information_schema"."columns" WHERE "table_schema" = 'foo' AND "table_name" = 'bar' ORDER BY "ordinal_position" NULLS FIRST"""
    )


def test_truncate_table_iceberg(adapter: AthenaEngineAdapter, mocker: MockerFixture):
    mocker.patch.object(
        adapter,
        "_query_table_type",
        return_value="iceberg",
    )
    mocker.patch.multiple(
        adapter, _clear_partition_data=mocker.DEFAULT, _clear_s3_location=mocker.DEFAULT
    )
    adapter._truncate_table(exp.to_table("foo.bar"))

    assert to_sql_calls(adapter) == ['DELETE FROM "foo"."bar" WHERE TRUE']
    t.cast(Mock, adapter._clear_partition_data).assert_not_called()
    t.cast(Mock, adapter._clear_s3_location).assert_not_called()


def test_truncate_table_hive(adapter: AthenaEngineAdapter, mocker: MockerFixture):
    mocker.patch.object(
        adapter,
        "_query_table_type",
        return_value="hive",
    )
    mocker.patch.object(
        adapter,
        "_is_hive_partitioned_table",
        return_value=False,
    )
    mocker.patch.object(adapter, "_query_table_s3_location", return_value="s3://foo/bar")
    mocker.patch.multiple(
        adapter, _clear_partition_data=mocker.DEFAULT, _clear_s3_location=mocker.DEFAULT
    )

    adapter._truncate_table(exp.to_table("foo.bar"))

    assert to_sql_calls(adapter) == []
    t.cast(Mock, adapter._clear_partition_data).assert_not_called()
    t.cast(Mock, adapter._clear_s3_location).assert_called_with("s3://foo/bar")


def test_truncate_table_hive_partitioned(adapter: AthenaEngineAdapter, mocker: MockerFixture):
    mocker.patch.object(
        adapter,
        "_query_table_type",
        return_value="hive",
    )
    mocker.patch.object(
        adapter,
        "_is_hive_partitioned_table",
        return_value=True,
    )
    mocker.patch.object(adapter, "_clear_partition_data")
    mocker.patch.object(adapter, "_clear_s3_location")
    adapter._truncate_table(exp.to_table("foo.bar"))

    assert to_sql_calls(adapter) == []
    t.cast(Mock, adapter._clear_partition_data).assert_called_with(
        exp.to_table("foo.bar"), exp.true()
    )
    t.cast(Mock, adapter._clear_s3_location).assert_not_called()


def test_create_state_table(adapter: AthenaEngineAdapter):
    adapter.s3_warehouse_location = "s3://base"
    adapter.create_state_table("_snapshots", {"name": exp.DataType.build("varchar")})

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `_snapshots` (`name` STRING) LOCATION 's3://base/_snapshots/' TBLPROPERTIES ('table_type'='iceberg')"
    ]


def test_drop_partitions_from_metastore_uses_batches(
    adapter: AthenaEngineAdapter, mocker: MockerFixture
):
    glue_client_mock = mocker.patch.object(AthenaEngineAdapter, "_glue_client", autospec=True)

    glue_client_mock.batch_delete_partition.assert_not_called()

    partition_values = []

    for i in range(63):
        partition_values.append([str(i)])

    adapter._drop_partitions_from_metastore(
        table=exp.table_("foo"), partition_values=partition_values
    )

    glue_client_mock.batch_delete_partition.assert_called()

    # should have been called in batches of 25
    calls = glue_client_mock.batch_delete_partition.call_args_list
    assert len(calls) == 3

    assert len(calls[0][1]["PartitionsToDelete"]) == 25
    assert len(calls[1][1]["PartitionsToDelete"]) == 25
    assert len(calls[2][1]["PartitionsToDelete"]) == 13

    # first call 0-24
    assert calls[0][1]["PartitionsToDelete"][0]["Values"][0] == "0"
    assert calls[0][1]["PartitionsToDelete"][-1]["Values"][0] == "24"

    # second call 25-49
    assert calls[1][1]["PartitionsToDelete"][0]["Values"][0] == "25"
    assert calls[1][1]["PartitionsToDelete"][-1]["Values"][0] == "49"

    # third call 50-62
    assert calls[2][1]["PartitionsToDelete"][0]["Values"][0] == "50"
    assert calls[2][1]["PartitionsToDelete"][-1]["Values"][0] == "62"


def test_iceberg_partition_transforms(adapter: AthenaEngineAdapter):
    expressions = d.parse(
        """
        MODEL (
            name test_table,
            kind FULL,
            table_format iceberg,
            partitioned_by (month(business_date), bucket(4, colb), colc)
        );

        SELECT 1::timestamp AS business_date, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    assert model.partitioned_by == [
        exp.Month(this=exp.column("business_date", quoted=True)),
        exp.PartitionedByBucket(
            this=exp.column("colb", quoted=True), expression=exp.Literal.number(4)
        ),
        exp.column("colc", quoted=True),
    ]

    adapter.s3_warehouse_location = "s3://bucket/prefix/"

    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        partitioned_by=model.partitioned_by,
        table_format=model.table_format,
    )

    adapter.ctas(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        partitioned_by=model.partitioned_by,
        query_or_df=model.ctas_query(),
        table_format=model.table_format,
    )

    assert to_sql_calls(adapter) == [
        # Hive syntax - create table
        """CREATE TABLE IF NOT EXISTS `test_table` (`business_date` TIMESTAMP, `colb` STRING, `colc` STRING) PARTITIONED BY (MONTH(`business_date`), BUCKET(4, `colb`), `colc`) LOCATION 's3://bucket/prefix/test_table/' TBLPROPERTIES ('table_type'='iceberg')""",
        # Trino syntax - CTAS
        """CREATE TABLE IF NOT EXISTS "test_table" WITH (table_type='iceberg', partitioning=ARRAY['MONTH(business_date)', 'BUCKET(colb, 4)', 'colc'], location='s3://bucket/prefix/test_table/', is_external=false) AS SELECT CAST("business_date" AS TIMESTAMP) AS "business_date", CAST("colb" AS VARCHAR) AS "colb", CAST("colc" AS VARCHAR) AS "colc" FROM (SELECT CAST(1 AS TIMESTAMP) AS "business_date", CAST(2 AS VARCHAR) AS "colb", 'foo' AS "colc" LIMIT 0) AS "_subquery\"""",
    ]


@pytest.mark.parametrize(
    "source_format, target_format, expected_temp_format, expect_error",
    [
        ("hive", "hive", None, False),
        ("iceberg", "hive", None, True),  # Expect error for mismatched formats
        ("hive", "iceberg", None, True),  # Expect error for mismatched formats
        ("iceberg", "iceberg", "iceberg", False),
        (None, "iceberg", None, True),  # Source doesn't exist or type unknown, target is iceberg
        (
            "iceberg",
            None,
            "iceberg",
            True,
        ),  # Target doesn't exist or type unknown, source is iceberg
        (None, "hive", None, False),  # Source doesn't exist or type unknown, target is hive
        ("hive", None, None, False),  # Target doesn't exist or type unknown, source is hive
        (None, None, None, False),  # Both don't exist or types unknown
    ],
)
def test_table_diff_temp_table_format(
    table_diff: TableDiff,
    mocker: MockerFixture,
    source_format: t.Optional[str],
    target_format: t.Optional[str],
    expected_temp_format: t.Optional[str],
    expect_error: bool,
):
    adapter = t.cast(AthenaEngineAdapter, table_diff.adapter)

    # Mock _query_table_type to return specified formats
    def mock_query_table_type(table_name: exp.Table) -> t.Optional[str]:
        if table_name.name == "source_table":
            return source_format
        if table_name.name == "target_table":
            return target_format
        return "hive"  # Default for other tables if any

    mocker.patch.object(adapter, "_query_table_type", side_effect=mock_query_table_type)

    # Mock temp_table to capture kwargs
    mock_temp_table = mocker.patch.object(adapter, "temp_table", autospec=True)
    mock_temp_table.return_value.__enter__.return_value = exp.to_table("diff_table")

    # Mock fetchdf and other calls made within row_diff to avoid actual DB interaction
    mocker.patch.object(adapter, "fetchdf", return_value=pd.DataFrame())
    mocker.patch.object(adapter, "get_data_objects", return_value=[])
    mocker.patch.object(adapter, "columns", return_value={"id": exp.DataType.build("int")})

    if expect_error:
        with pytest.raises(
            SQLMeshError,
            match="do not match for Athena. Diffing between different table formats is not supported.",
        ):
            table_diff.row_diff()
        mock_temp_table.assert_not_called()  # temp_table should not be called if formats mismatch
        return

    try:
        table_diff.row_diff()
    except Exception:
        pass  # We only care about the temp_table call args for non-error cases

    mock_temp_table.assert_called_once()
    _, called_kwargs = mock_temp_table.call_args

    if expected_temp_format:
        assert called_kwargs.get("table_format") == expected_temp_format
    else:
        assert "table_format" not in called_kwargs
