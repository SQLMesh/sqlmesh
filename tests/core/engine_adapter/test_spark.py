# type: ignore
import typing as t
from datetime import datetime
from unittest.mock import call

import pytest
from pyspark.sql import types as spark_types
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one

from sqlmesh.core.engine_adapter import SparkEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObject
from sqlmesh.utils.errors import SQLMeshError
from tests.core.engine_adapter import to_sql_calls
import sqlmesh.core.dialect as d
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.model.definition import SqlModel

pytestmark = [pytest.mark.engine, pytest.mark.spark]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable) -> SparkEngineAdapter:
    return make_mocked_engine_adapter(SparkEngineAdapter)


def test_create_table_properties(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
        "colc": exp.DataType.build("TEXT"),
    }
    adapter.create_table(
        "test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("colb")],
        clustered_by=["colc"],
        storage_format="parquet",
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` INT, `colb` STRING, `colc` STRING) USING PARQUET PARTITIONED BY (`colb`)",
    ]

    adapter.cursor.reset_mock()
    adapter.create_table(
        "test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("cola"), exp.to_column("colb")],
        storage_format="parquet",
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` INT, `colb` STRING, `colc` STRING) USING PARQUET PARTITIONED BY (`cola`, `colb`)",
    ]

    with pytest.raises(SQLMeshError):
        adapter.create_table(
            "test_table",
            columns_to_types,
            partitioned_by=[parse_one("DATE(cola)")],
            storage_format="parquet",
        )


@pytest.mark.parametrize("wap_enabled", [True, False])
def test_replace_query_table_properties_not_exists(
    mocker: MockerFixture, make_mocked_engine_adapter: t.Callable, wap_enabled: bool
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=False,
    )
    adapter = make_mocked_engine_adapter(SparkEngineAdapter, wap_enabled=wap_enabled)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
        "colc": exp.DataType.build("TEXT"),
    }
    adapter.replace_query(
        "test_table",
        parse_one("SELECT 1 AS cola, '2' AS colb, '3' AS colc"),
        target_columns_to_types=columns_to_types,
        partitioned_by=[exp.to_column("colb")],
        storage_format="ICEBERG",
        table_properties={"a": exp.convert(1)},
    )

    expected_sql_calls = [
        "CREATE TABLE IF NOT EXISTS `test_table` USING ICEBERG PARTITIONED BY (`colb`) TBLPROPERTIES ('a'=1) AS SELECT CAST(`cola` AS INT) AS `cola`, CAST(`colb` AS STRING) AS `colb`, CAST(`colc` AS STRING) AS `colc` FROM (SELECT 1 AS `cola`, '2' AS `colb`, '3' AS `colc`) AS `_subquery`",
    ]
    if wap_enabled:
        expected_sql_calls.append("INSERT INTO `test_table` SELECT * FROM `test_table`")

    assert to_sql_calls(adapter) == expected_sql_calls


def test_replace_query_table_properties_exists(
    mocker: MockerFixture, make_mocked_engine_adapter: t.Callable
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=True,
    )
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test_table", type="table")],
    )

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
        "colc": exp.DataType.build("TEXT"),
    }
    adapter.replace_query(
        "test_table",
        parse_one("SELECT 1 AS cola, '2' AS colb, '3' AS colc"),
        target_columns_to_types=columns_to_types,
        partitioned_by=[exp.to_column("colb")],
        storage_format="ICEBERG",
        table_properties={"a": exp.convert(1)},
    )

    assert to_sql_calls(adapter) == [
        "INSERT OVERWRITE TABLE `test_table` (`cola`, `colb`, `colc`) SELECT 1 AS `cola`, '2' AS `colb`, '3' AS `colc`",
    ]


def test_create_view_properties(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)

    adapter.create_view(
        "test_view", parse_one("SELECT a FROM tbl"), view_properties={"a": exp.convert(1)}
    )  # type: ignore
    adapter.cursor.execute.assert_called_once_with(
        "CREATE OR REPLACE VIEW test_view TBLPROPERTIES ('a'=1) AS SELECT a FROM tbl"
    )


def test_alter_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    current_table_name = "test_table"
    target_table_name = "test_table__1"

    def table_columns(table_name: str) -> t.Dict[str, exp.DataType]:
        if table_name == current_table_name:
            return {
                "id": exp.DataType.build("INT"),
                "a": exp.DataType.build("INT"),
                "b": exp.DataType.build("STRING"),
                "complex": exp.DataType.build("STRUCT<complex_a: INT, complex_b: STRING>"),
                "ds": exp.DataType.build("STRING"),
            }
        return {
            "id": exp.DataType.build("BIGINT"),
            "a": exp.DataType.build("STRING"),
            "complex": exp.DataType.build("STRUCT<complex_a: INT>"),
            "ds": exp.DataType.build("INT"),
        }

    adapter.columns = table_columns

    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))

    adapter.cursor.execute.assert_has_calls(
        [
            call("""ALTER TABLE `test_table` DROP COLUMN `b`"""),
            call("""ALTER TABLE `test_table` DROP COLUMN `id`"""),
            call("""ALTER TABLE `test_table` ADD COLUMN `id` BIGINT"""),
            call("""ALTER TABLE `test_table` DROP COLUMN `a`"""),
            call("""ALTER TABLE `test_table` ADD COLUMN `a` STRING"""),
            call("""ALTER TABLE `test_table` DROP COLUMN `complex`"""),
            call("""ALTER TABLE `test_table` ADD COLUMN `complex` STRUCT<`complex_a`: INT>"""),
            call("""ALTER TABLE `test_table` DROP COLUMN `ds`"""),
            call("""ALTER TABLE `test_table` ADD COLUMN `ds` INT"""),
        ]
    )


def test_replace_query_not_exists(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=False,
    )
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.replace_query(
        "test_table", parse_one("SELECT a FROM tbl"), {"a": exp.DataType.build("INT")}
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` AS SELECT CAST(`a` AS INT) AS `a` FROM (SELECT `a` FROM `tbl`) AS `_subquery`",
    ]


def test_replace_query_exists(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=True,
    )
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test_table", type="table")],
    )
    adapter.replace_query("test_table", parse_one("SELECT a FROM tbl"), {"a": "int"})

    assert to_sql_calls(adapter) == [
        "INSERT OVERWRITE TABLE `test_table` (`a`) SELECT `a` FROM `tbl`",
    ]


def test_replace_query_self_ref_not_exists(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.get_current_catalog",
        lambda self: "spark_catalog",
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter._get_current_schema",
        side_effect=lambda: "default",
    )

    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.cursor.fetchone.return_value = (1,)

    table_name = "db.table"
    temp_table_id = "abcdefgh"
    mocker.patch(
        "sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table",
        return_value=make_temp_table_name(table_name, temp_table_id),
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.columns",
        return_value={"col": exp.DataType(this=exp.DataType.Type.INT)},
    )

    def check_table_exists(table_name: exp.Table) -> bool:
        for sql in to_sql_calls(adapter):
            if f"CREATE TABLE IF NOT EXISTS {table_name.sql(dialect=adapter.dialect)}" in sql:
                return True
        return False

    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        side_effect=check_table_exists,
    )

    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="db", name="table", type="table")],
    )

    adapter.replace_query(table_name, parse_one(f"SELECT col + 1 AS col FROM {table_name}"))

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`table` (`col` INT)",
        "CREATE SCHEMA IF NOT EXISTS `db`",
        "CREATE TABLE IF NOT EXISTS `db`.`temp_table_abcdefgh` AS SELECT CAST(`col` AS INT) AS `col` FROM (SELECT `col` FROM `db`.`table`) AS `_subquery`",
        "INSERT OVERWRITE TABLE `db`.`table` (`col`) SELECT `col` + 1 AS `col` FROM `db`.`temp_table_abcdefgh`",
        "DROP TABLE IF EXISTS `db`.`temp_table_abcdefgh`",
    ]


def test_replace_query_self_ref_exists(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=True,
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.get_current_catalog",
        return_value="spark_catalog",
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter._get_current_schema",
        return_value="default",
    )

    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.cursor.fetchone.return_value = (1,)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="db", name="table", type="table")],
    )

    table_name = "db.table"
    temp_table_id = "abcdefgh"
    mocker.patch(
        "sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table",
        return_value=make_temp_table_name(table_name, temp_table_id),
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.columns",
        return_value={"col": exp.DataType(this=exp.DataType.Type.INT)},
    )

    adapter.replace_query(table_name, parse_one(f"SELECT col + 1 AS col FROM {table_name}"))

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`table` (`col` INT)",
        # This is something temp_table does and isn't really needed. The contract with `replace_query` is that
        # the schema for the table already exists
        "CREATE SCHEMA IF NOT EXISTS `db`",
        f"CREATE TABLE IF NOT EXISTS `db`.`temp_table_{temp_table_id}` AS SELECT CAST(`col` AS INT) AS `col` FROM (SELECT `col` FROM `db`.`table`) AS `_subquery`",
        "INSERT OVERWRITE TABLE `db`.`table` (`col`) SELECT `col` + 1 AS `col` FROM `db`.`temp_table_abcdefgh`",
        f"DROP TABLE IF EXISTS `db`.`temp_table_{temp_table_id}`",
    ]


def test_create_table_table_options(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("int"), "b": exp.DataType.build("int")},
        table_properties={
            "test.conf.key": exp.convert("value"),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT, `b` INT) TBLPROPERTIES ('test.conf.key'='value')",
    ]


def test_create_state_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)

    adapter.create_state_table(
        "test_table",
        {"a": exp.DataType.build("int"), "b": exp.DataType.build("int")},
        primary_key=["a"],
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT, `b` INT) PARTITIONED BY (`a`)",
    ]


test_primitive_params = [
    ("byte", spark_types.ByteType()),
    ("short", spark_types.ShortType()),
    ("int", spark_types.IntegerType()),
    ("bigint", spark_types.LongType()),
    ("float", spark_types.FloatType()),
    ("double", spark_types.DoubleType()),
    ("decimal", spark_types.DecimalType()),
    ("text", spark_types.StringType()),
    ("varchar(25)", spark_types.StringType()),
    ("char(30)", spark_types.StringType()),
    ("binary", spark_types.BinaryType()),
    ("boolean", spark_types.BooleanType()),
    ("date", spark_types.DateType()),
    ("datetime", spark_types.TimestampNTZType()),
    ("timestamp", spark_types.TimestampType()),
]


@pytest.mark.parametrize(
    "type_name, spark_type",
    test_primitive_params,
    ids=[x[0] for x in test_primitive_params],
)
def test_col_to_types_to_spark_schema_primitives(type_name, spark_type):
    from pyspark.sql import types as spark_types

    assert SparkEngineAdapter.sqlglot_to_spark_types(
        {f"col_{type_name}": exp.DataType.build(type_name, dialect="spark")}
    ) == spark_types.StructType([spark_types.StructField(f"col_{type_name}", spark_type)])


test_complex_params = [
    (
        "map",
        "map<int, string>",
        spark_types.MapType(spark_types.IntegerType(), spark_types.StringType()),
    ),
    (
        "array_primitives",
        "array<int>",
        spark_types.ArrayType(spark_types.IntegerType(), True),
    ),
    (
        "array_struct",
        "array<struct<cola:int,colb:array<string>>>",
        spark_types.ArrayType(
            spark_types.StructType(
                [
                    spark_types.StructField("cola", spark_types.IntegerType()),
                    spark_types.StructField(
                        "colb", spark_types.ArrayType(spark_types.StringType(), True)
                    ),
                ]
            ),
            True,
        ),
    ),
    (
        "struct_primitives",
        "struct<col1: int, col2: string>",
        spark_types.StructType(
            [
                spark_types.StructField("col1", spark_types.IntegerType()),
                spark_types.StructField("col2", spark_types.StringType()),
            ]
        ),
    ),
    (
        "struct_of_arrays",
        "struct<col1: array<int>, col2: array<string>>",
        spark_types.StructType(
            [
                spark_types.StructField(
                    "col1", spark_types.ArrayType(spark_types.IntegerType(), True)
                ),
                spark_types.StructField(
                    "col2", spark_types.ArrayType(spark_types.StringType(), True)
                ),
            ]
        ),
    ),
    (
        "struct_and_array",
        "struct<col1: array<struct<cola:int,colb:array<string>>>, col2: struct<col1: int, col2: string>>",
        spark_types.StructType(
            [
                spark_types.StructField(
                    "col1",
                    spark_types.ArrayType(
                        spark_types.StructType(
                            [
                                spark_types.StructField("cola", spark_types.IntegerType()),
                                spark_types.StructField(
                                    "colb",
                                    spark_types.ArrayType(spark_types.StringType(), True),
                                ),
                            ]
                        ),
                        True,
                    ),
                ),
                spark_types.StructField(
                    "col2",
                    spark_types.StructType(
                        [
                            spark_types.StructField("col1", spark_types.IntegerType()),
                            spark_types.StructField("col2", spark_types.StringType()),
                        ]
                    ),
                ),
            ]
        ),
    ),
    (
        "array_of_maps",
        "array<map<int, string>>",
        spark_types.ArrayType(
            spark_types.MapType(spark_types.IntegerType(), spark_types.StringType()), True
        ),
    ),
    (
        "struct_with_struct",
        "struct<col1: struct<cola:int,colb:array<string>>, col2: struct<col1: int, col2: string>>",
        spark_types.StructType(
            [
                spark_types.StructField(
                    "col1",
                    spark_types.StructType(
                        [
                            spark_types.StructField("cola", spark_types.IntegerType()),
                            spark_types.StructField(
                                "colb",
                                spark_types.ArrayType(spark_types.StringType(), True),
                            ),
                        ]
                    ),
                ),
                spark_types.StructField(
                    "col2",
                    spark_types.StructType(
                        [
                            spark_types.StructField("col1", spark_types.IntegerType()),
                            spark_types.StructField("col2", spark_types.StringType()),
                        ]
                    ),
                ),
            ]
        ),
    ),
]


@pytest.mark.parametrize(
    "type_name, spark_type",
    [x[1:] for x in test_complex_params],
    ids=[x[0] for x in test_complex_params],
)
def test_col_to_types_to_spark_schema_complex(type_name, spark_type):
    actual = SparkEngineAdapter.sqlglot_to_spark_types(
        {f"col_{type_name}": exp.DataType.build(type_name, dialect="spark")}
    )
    expected = spark_types.StructType([spark_types.StructField(f"col_{type_name}", spark_type)])
    assert actual == expected


@pytest.mark.parametrize(
    "type_name, spark_type",
    test_primitive_params,
    ids=[x[0] for x in test_primitive_params],
)
def test_spark_struct_primitives_to_col_to_types(type_name, spark_type):
    actual = SparkEngineAdapter.spark_to_sqlglot_types(
        spark_types.StructType([spark_types.StructField(f"col_{type_name}", spark_type)])
    )

    expected_type = (
        exp.DataType.build("string")
        if "char" in type_name
        else exp.DataType.build(type_name, dialect="spark")
    )
    expected = {f"col_{type_name}": expected_type}
    assert actual == expected


@pytest.mark.parametrize(
    "type_name, spark_type",
    [x[1:] for x in test_complex_params],
    ids=[x[0] for x in test_complex_params],
)
def test_spark_struct_complex_to_col_to_types(type_name, spark_type):
    actual = SparkEngineAdapter.spark_to_sqlglot_types(
        spark_types.StructType([spark_types.StructField(f"col_{type_name}", spark_type)])
    )
    expected = {f"col_{type_name}": exp.DataType.build(type_name, dialect="spark")}
    assert actual == expected


def test_scd_type_2_by_time(
    make_mocked_engine_adapter: t.Callable, make_temp_table_name: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter._default_catalog = "spark_catalog"
    adapter.spark.catalog.currentCatalog.return_value = "spark_catalog"
    adapter.spark.catalog.currentDatabase.return_value = "default"

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "db.target"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    def check_table_exists(table_name: exp.Table) -> bool:
        for sql in to_sql_calls(adapter):
            if f"CREATE TABLE IF NOT EXISTS {table_name.sql(dialect=adapter.dialect)}" in sql:
                return True
        return False

    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        side_effect=check_table_exists,
    )
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="db", name="target", type="table")],
    )

    adapter.scd_type_2_by_time(
        target_table="db.target",
        source_table=t.cast(
            exp.Select, parse_one("SELECT id, name, price, test_updated_at FROM db.source")
        ),
        unique_key=[exp.func("COALESCE", "id", "''")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        updated_at_col=exp.column("test_updated_at", quoted=True),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_updated_at": exp.DataType.build("TIMESTAMP"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`target` (`id` INT, `name` STRING, `price` DOUBLE, `test_updated_at` TIMESTAMP, `test_valid_from` TIMESTAMP, `test_valid_to` TIMESTAMP)",
        "CREATE SCHEMA IF NOT EXISTS `db`",
        "CREATE TABLE IF NOT EXISTS `db`.`temp_target_abcdefgh` AS SELECT CAST(`id` AS INT) AS `id`, CAST(`name` AS STRING) AS `name`, CAST(`price` AS DOUBLE) AS `price`, CAST(`test_updated_at` AS TIMESTAMP) AS `test_updated_at`, CAST(`test_valid_from` AS TIMESTAMP) AS `test_valid_from`, CAST(`test_valid_to` AS TIMESTAMP) AS `test_valid_to` FROM (SELECT `id`, `name`, `price`, `test_updated_at`, `test_valid_from`, `test_valid_to` FROM `db`.`target`) AS `_subquery`",
        parse_one(
            """WITH `source` AS (
  SELECT
    `_exists`,
    `id`,
    `name`,
    `price`,
    `test_updated_at`
  FROM (
    SELECT
      TRUE AS `_exists`,
      `id` AS `id`,
      `name` AS `name`,
      `price` AS `price`,
      CAST(`test_updated_at` AS TIMESTAMP) AS `test_updated_at`,
      ROW_NUMBER() OVER (PARTITION BY COALESCE(`id`, '') ORDER BY COALESCE(`id`, '')) AS _row_number
    FROM (
      SELECT
        `id`,
        `name`,
        `price`,
        `test_updated_at`
      FROM `db`.`source`
    ) AS `raw_source`
  ) AS _t
  WHERE
    _row_number = 1
), `static` AS (
  SELECT
    `id`,
    `name`,
    `price`,
    `test_updated_at`,
    `test_valid_from`,
    `test_valid_to`,
    TRUE AS `_exists`
  FROM `db`.`temp_target_abcdefgh`
  WHERE
    NOT `test_valid_to` IS NULL
), `latest` AS (
  SELECT
    `id`,
    `name`,
    `price`,
    `test_updated_at`,
    `test_valid_from`,
    `test_valid_to`,
    TRUE AS `_exists`
  FROM `db`.`temp_target_abcdefgh`
  WHERE
    `test_valid_to` IS NULL
), `deleted` AS (
  SELECT
    `static`.`id`,
    `static`.`name`,
    `static`.`price`,
    `static`.`test_updated_at`,
    `static`.`test_valid_from`,
    `static`.`test_valid_to`
  FROM `static`
  LEFT JOIN `latest`
    ON COALESCE(`static`.`id`, '') = COALESCE(`latest`.`id`, '')
  WHERE
    `latest`.`test_valid_to` IS NULL
), `latest_deleted` AS (
  SELECT
    TRUE AS `_exists`,
    COALESCE(`id`, '') AS `_key0`,
    MAX(`test_valid_to`) AS `test_valid_to`
  FROM `deleted`
  GROUP BY
    COALESCE(`id`, '')
), `joined` AS (
  SELECT
    `source`.`_exists` AS `_exists`,
    `latest`.`id` AS `t_id`,
    `latest`.`name` AS `t_name`,
    `latest`.`price` AS `t_price`,
    `latest`.`test_updated_at` AS `t_test_updated_at`,
    `latest`.`test_valid_from` AS `t_test_valid_from`,
    `latest`.`test_valid_to` AS `t_test_valid_to`,
    `source`.`id` AS `id`,
    `source`.`name` AS `name`,
    `source`.`price` AS `price`,
    `source`.`test_updated_at` AS `test_updated_at`
  FROM `latest`
  LEFT JOIN `source`
    ON COALESCE(`latest`.`id`, '') = COALESCE(`source`.`id`, '')
  UNION ALL
  SELECT
    `source`.`_exists` AS `_exists`,
    `latest`.`id` AS `t_id`,
    `latest`.`name` AS `t_name`,
    `latest`.`price` AS `t_price`,
    `latest`.`test_updated_at` AS `t_test_updated_at`,
    `latest`.`test_valid_from` AS `t_test_valid_from`,
    `latest`.`test_valid_to` AS `t_test_valid_to`,
    `source`.`id` AS `id`,
    `source`.`name` AS `name`,
    `source`.`price` AS `price`,
    `source`.`test_updated_at` AS `test_updated_at`
  FROM `latest`
  RIGHT JOIN `source`
    ON COALESCE(`latest`.`id`, '') = COALESCE(`source`.`id`, '')
  WHERE
    `latest`.`_exists` IS NULL
), `updated_rows` AS (
  SELECT
    COALESCE(`joined`.`t_id`, `joined`.`id`) AS `id`,
    COALESCE(`joined`.`t_name`, `joined`.`name`) AS `name`,
    COALESCE(`joined`.`t_price`, `joined`.`price`) AS `price`,
    COALESCE(`joined`.`t_test_updated_at`, `joined`.`test_updated_at`) AS `test_updated_at`,
    CASE
      WHEN `t_test_valid_from` IS NULL AND NOT `latest_deleted`.`_exists` IS NULL
      THEN CASE
        WHEN `latest_deleted`.`test_valid_to` > `test_updated_at`
        THEN `latest_deleted`.`test_valid_to`
        ELSE `test_updated_at`
      END
      WHEN `t_test_valid_from` IS NULL
      THEN CAST('1970-01-01 00:00:00' AS TIMESTAMP)
      ELSE `t_test_valid_from`
    END AS `test_valid_from`,
    CASE
      WHEN `joined`.`test_updated_at` > `joined`.`t_test_updated_at`
      THEN `joined`.`test_updated_at`
      WHEN `joined`.`_exists` IS NULL
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE `t_test_valid_to`
    END AS `test_valid_to`
  FROM `joined`
  LEFT JOIN `latest_deleted`
    ON COALESCE(`joined`.`id`, '') = `latest_deleted`.`_key0`
), `inserted_rows` AS (
  SELECT
    `id`,
    `name`,
    `price`,
    `test_updated_at`,
    `test_updated_at` AS `test_valid_from`,
    CAST(NULL AS TIMESTAMP) AS `test_valid_to`
  FROM `joined`
  WHERE
    `joined`.`test_updated_at` > `joined`.`t_test_updated_at`
)
INSERT OVERWRITE TABLE `db`.`target` (
  `id`,
  `name`,
  `price`,
  `test_updated_at`,
  `test_valid_from`,
  `test_valid_to`
)
SELECT
  `id`,
  `name`,
  `price`,
  `test_updated_at`,
  `test_valid_from`,
  `test_valid_to`
FROM `static`
UNION ALL
SELECT
  `id`,
  `name`,
  `price`,
  `test_updated_at`,
  `test_valid_from`,
  `test_valid_to`
FROM `updated_rows`
UNION ALL
SELECT
  `id`,
  `name`,
  `price`,
  `test_updated_at`,
  `test_valid_from`,
  `test_valid_to`
FROM `inserted_rows`
        """,
            dialect="spark",
        ).sql(dialect="spark"),
        "DROP TABLE IF EXISTS `db`.`temp_target_abcdefgh`",
    ]


def test_wap_prepare(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.connection.get_current_catalog.return_value = "spark_catalog"
    adapter.spark.catalog.currentDatabase.return_value = "default"

    table_name = "test_db.test_table"
    wap_id = "test_wap_id"

    updated_table_name = adapter.wap_prepare(table_name, wap_id)
    assert updated_table_name == f"spark_catalog.{table_name}.branch_wap_{wap_id}"

    adapter.cursor.execute.assert_called_once_with(
        f"ALTER TABLE spark_catalog.{table_name} CREATE BRANCH wap_{wap_id}"
    )


def test_wap_publish(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    iceberg_snapshot_id = 123

    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.connection.get_current_catalog.return_value = "spark_catalog"
    adapter.spark.catalog.currentDatabase.return_value = "default"
    adapter.cursor.fetchall.return_value = [(iceberg_snapshot_id,)]

    table_name = "test_db.test_table"
    wap_id = "test_wap_id"

    adapter.wap_publish(table_name, wap_id)

    adapter.cursor.execute.assert_has_calls(
        [
            call(
                f"SELECT snapshot_id FROM spark_catalog.{table_name}.refs WHERE name = 'wap_{wap_id}'"
            ),
            call(
                f"CALL spark_catalog.system.cherrypick_snapshot('{table_name}', {iceberg_snapshot_id})"
            ),
            call(f"ALTER TABLE spark_catalog.{table_name} DROP BRANCH wap_{wap_id}"),
        ]
    )


@pytest.mark.parametrize("wap_enabled", [True, False])
def test_create_table_iceberg(
    mocker: MockerFixture, make_mocked_engine_adapter: t.Callable, wap_enabled: bool
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=False,
    )

    adapter = make_mocked_engine_adapter(SparkEngineAdapter, wap_enabled=wap_enabled)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
        "colc": exp.DataType.build("TEXT"),
    }
    adapter.create_table(
        "test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("colb")],
        clustered_by=["colc"],
        storage_format="ICEBERG",
    )

    expected_sql_calls = [
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` INT, `colb` STRING, `colc` STRING) USING ICEBERG PARTITIONED BY (`colb`)",
    ]
    if wap_enabled:
        expected_sql_calls.append("INSERT INTO `test_table` SELECT * FROM `test_table`")

    assert to_sql_calls(adapter) == expected_sql_calls


def test_comments_hive(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)

    current_catalog_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.EngineAdapter.get_catalog_type"
    )
    current_catalog_mock.return_value = "hive"

    allowed_table_comment_length = SparkEngineAdapter.MAX_TABLE_COMMENT_LENGTH
    truncated_table_comment = "a" * allowed_table_comment_length
    long_table_comment = truncated_table_comment + "b"

    allowed_column_comment_length = SparkEngineAdapter.MAX_COLUMN_COMMENT_LENGTH
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
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT COMMENT '{truncated_column_comment}', `b` INT) COMMENT '{truncated_table_comment}'",
        f"CREATE TABLE IF NOT EXISTS `test_table` COMMENT '{truncated_table_comment}' AS SELECT CAST(`a` AS INT) AS `a`, CAST(`b` AS INT) AS `b` FROM (SELECT `a`, `b` FROM `source_table`) AS `_subquery`",
        f"ALTER TABLE `test_table` ALTER COLUMN `a` COMMENT '{truncated_column_comment}'",
        f"CREATE OR REPLACE VIEW test_view COMMENT '{truncated_table_comment}' AS SELECT a, b FROM source_table",
        f"COMMENT ON TABLE `test_table` IS '{truncated_table_comment}'",
        f"ALTER TABLE `test_table` ALTER COLUMN `a` COMMENT '{truncated_column_comment}'",
    ]


def test_comments_iceberg(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)

    current_catalog_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.EngineAdapter.get_catalog_type"
    )
    current_catalog_mock.return_value = "iceberg"

    allowed_table_comment_length = SparkEngineAdapter.MAX_TABLE_COMMENT_LENGTH
    truncated_table_comment = "a" * allowed_table_comment_length
    long_table_comment = truncated_table_comment + "b"

    allowed_column_comment_length = SparkEngineAdapter.MAX_COLUMN_COMMENT_LENGTH
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
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT COMMENT '{long_column_comment}', `b` INT) COMMENT '{long_table_comment}'",
        f"CREATE TABLE IF NOT EXISTS `test_table` COMMENT '{long_table_comment}' AS SELECT CAST(`a` AS INT) AS `a`, CAST(`b` AS INT) AS `b` FROM (SELECT `a`, `b` FROM `source_table`) AS `_subquery`",
        f"ALTER TABLE `test_table` ALTER COLUMN `a` COMMENT '{long_column_comment}'",
        f"CREATE OR REPLACE VIEW test_view COMMENT '{long_table_comment}' AS SELECT a, b FROM source_table",
        f"COMMENT ON TABLE `test_table` IS '{long_table_comment}'",
        f"ALTER TABLE `test_table` ALTER COLUMN `a` COMMENT '{long_column_comment}'",
    ]


def test_create_table_with_wap(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=False,
    )
    adapter = make_mocked_engine_adapter(SparkEngineAdapter, wap_enabled=True)

    adapter.create_table(
        "catalog.schema.table.branch_wap_12345",
        {"a": exp.DataType.build("int")},
        storage_format="ICEBERG",
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        "CREATE TABLE IF NOT EXISTS `catalog`.`schema`.`table` (`a` INT) USING ICEBERG",
        "INSERT INTO `catalog`.`schema`.`table` SELECT * FROM `catalog`.`schema`.`table`",
    ]


def test_replace_query_with_wap_self_reference(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=True,
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="abcdefgh",
    )

    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="schema", name="table", type="table")],
    )

    adapter.replace_query(
        "catalog.schema.table.branch_wap_12345",
        parse_one("SELECT 1 as a FROM catalog.schema.table.branch_wap_12345"),
        target_columns_to_types={"a": exp.DataType.build("INT")},
        storage_format="ICEBERG",
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        "CREATE TABLE IF NOT EXISTS `catalog`.`schema`.`table` (`a` INT) USING ICEBERG",
        "CREATE SCHEMA IF NOT EXISTS `catalog`.`schema`",
        "CREATE TABLE IF NOT EXISTS `catalog`.`schema`.`temp_branch_wap_12345_abcdefgh` USING ICEBERG AS SELECT CAST(`a` AS INT) AS `a` FROM (SELECT `a` FROM `catalog`.`schema`.`table`.`branch_wap_12345`) AS `_subquery`",
        "INSERT OVERWRITE TABLE `catalog`.`schema`.`table`.`branch_wap_12345` (`a`) SELECT 1 AS `a` FROM `catalog`.`schema`.`temp_branch_wap_12345_abcdefgh`",
        "DROP TABLE IF EXISTS `catalog`.`schema`.`temp_branch_wap_12345_abcdefgh`",
    ]


def test_table_format(adapter: SparkEngineAdapter, mocker: MockerFixture):
    mocker.patch(
        "sqlmesh.core.engine_adapter.spark.SparkEngineAdapter.table_exists",
        return_value=True,
    )

    expressions = d.parse(
        """
        MODEL (
            name test_table,
            kind FULL,
            table_format iceberg,
            storage_format orc
        );

        SELECT 1::timestamp AS cola, 2::varchar as colb, 'foo' as colc;
    """
    )
    model: SqlModel = t.cast(SqlModel, load_sql_based_model(expressions))

    # both table_format and storage_format
    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
        storage_format=model.storage_format,
    )

    # just table_format
    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
    )

    # just storage_format set to a table format (test for backwards compatibility)
    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        storage_format=model.table_format,
    )

    adapter.ctas(
        table_name=model.name,
        query_or_df=model.query,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
        storage_format=model.storage_format,
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` TIMESTAMP, `colb` STRING, `colc` STRING) USING ICEBERG TBLPROPERTIES ('write.format.default'='orc')",
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` TIMESTAMP, `colb` STRING, `colc` STRING) USING ICEBERG",
        "CREATE TABLE IF NOT EXISTS `test_table` (`cola` TIMESTAMP, `colb` STRING, `colc` STRING) USING ICEBERG",
        "CREATE TABLE IF NOT EXISTS `test_table` USING ICEBERG TBLPROPERTIES ('write.format.default'='orc') AS SELECT CAST(`cola` AS TIMESTAMP) AS `cola`, CAST(`colb` AS STRING) AS `colb`, CAST(`colc` AS STRING) AS `colc` FROM (SELECT CAST(1 AS TIMESTAMP) AS `cola`, CAST(2 AS STRING) AS `colb`, 'foo' AS `colc`) AS `_subquery`",
    ]


def test_alter_schema_owner(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.alter_schema_owner("catalog.my_schema", "svc_prod_spn")
    assert to_sql_calls(adapter) == ["ALTER SCHEMA `catalog`.`my_schema` OWNER TO `svc_prod_spn`"]


def test_alter_schema_owner_three_part_name(make_mocked_engine_adapter: t.Callable):
    # Schema references are typically 2-part (catalog.schema), but verify quoting is correct.
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.alter_schema_owner("my_schema", "svc_prod_spn")
    assert to_sql_calls(adapter) == ["ALTER SCHEMA `my_schema` OWNER TO `svc_prod_spn`"]


def test_alter_view_owner(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.alter_view_owner("catalog.my_schema.my_view", "svc_prod_spn")
    assert to_sql_calls(adapter) == [
        "ALTER VIEW `catalog`.`my_schema`.`my_view` OWNER TO `svc_prod_spn`"
    ]


def test_alter_view_owner_special_chars_in_principal(make_mocked_engine_adapter: t.Callable):
    # Databricks Unity Catalog principals can contain colons and @ signs.
    # Verify they are safely backtick-quoted and not interpreted as SQL syntax.
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.alter_view_owner("catalog.sushi__dev.orders", "group:devs@company.com")
    assert to_sql_calls(adapter) == [
        "ALTER VIEW `catalog`.`sushi__dev`.`orders` OWNER TO `group:devs@company.com`"
    ]


def test_alter_table_owner(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.alter_table_owner("catalog.sqlmesh__sushi.orders__abc123", "svc_prod_spn")
    assert to_sql_calls(adapter) == [
        "ALTER TABLE `catalog`.`sqlmesh__sushi`.`orders__abc123` OWNER TO `svc_prod_spn`"
    ]


def test_alter_table_owner_special_chars_in_principal(make_mocked_engine_adapter: t.Callable):
    # Databricks Unity Catalog principals can contain colons and @ signs.
    adapter = make_mocked_engine_adapter(SparkEngineAdapter)
    adapter.alter_table_owner("catalog.sqlmesh__sushi.orders__abc123", "group:data@company.com")
    assert to_sql_calls(adapter) == [
        "ALTER TABLE `catalog`.`sqlmesh__sushi`.`orders__abc123` OWNER TO `group:data@company.com`"
    ]


def test_alter_schema_owner_base_noop(make_mocked_engine_adapter: t.Callable):
    # The base EngineAdapter.alter_schema_owner is a no-op: adapters that don't
    # support ownership control silently skip it without emitting any SQL.
    from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter

    adapter = make_mocked_engine_adapter(DuckDBEngineAdapter)
    adapter.alter_schema_owner("my_schema", "some_owner")
    adapter.alter_view_owner("my_schema.my_view", "some_owner")
    adapter.alter_table_owner("my_schema.my_table", "some_owner")
    # No ALTER SQL should have been emitted
    alter_calls = [s for s in to_sql_calls(adapter) if "OWNER" in s.upper()]
    assert alter_calls == []


def test_get_data_object_wap_branch(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SparkEngineAdapter, patch_get_data_objects=False)
    mocker.patch.object(adapter, "_get_data_objects", return_value=[])

    table = exp.to_table(
        "`catalog`.`sqlmesh__test`.`test__test_view__630979748`.`branch_wap_472234d7`",
        dialect="spark",
    )
    adapter.get_data_object(table)

    adapter._get_data_objects.assert_called_once_with(
        d.schema_("sqlmesh__test", "catalog"),
        {"test__test_view__630979748"},
    )
