# type: ignore
import typing as t
from datetime import datetime

import pandas as pd  # noqa: TID253
import pytest
from google.cloud import bigquery
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one
from sqlglot.helper import ensure_list

import sqlmesh.core.dialect as d
from sqlmesh.core.engine_adapter import BigQueryEngineAdapter
from sqlmesh.core.engine_adapter.bigquery import select_partitions_expr
from sqlmesh.core.engine_adapter.shared import DataObjectType
from sqlmesh.core.node import IntervalUnit
from sqlmesh.utils import AttributeDict
from sqlmesh.utils.errors import SQLMeshError

pytestmark = [pytest.mark.bigquery, pytest.mark.engine]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture) -> BigQueryEngineAdapter:
    mocked_adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    mocker.patch("sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute")
    return mocked_adapter


def test_insert_overwrite_by_time_partition_query(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, ds FROM tbl"),
        start="2022-01-01",
        end="2022-01-05",
        time_formatter=lambda x, _: exp.Literal.string(x.strftime("%Y-%m-%d")),
        time_column="ds",
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("string"),
        },
    )
    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "MERGE INTO `test_table` AS `__MERGE_TARGET__` USING (SELECT `a`, `ds` FROM (SELECT `a`, `ds` FROM `tbl`) AS `_subquery` WHERE `ds` BETWEEN '2022-01-01' AND '2022-01-05') AS `__MERGE_SOURCE__` ON FALSE WHEN NOT MATCHED BY SOURCE AND `ds` BETWEEN '2022-01-01' AND '2022-01-05' THEN DELETE WHEN NOT MATCHED THEN INSERT (`a`, `ds`) VALUES (`a`, `ds`)"
    ]


def test_insert_overwrite_by_partition_query(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter._default_catalog = "test_project"
    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "test_schema.test_table"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    adapter.insert_overwrite_by_partition(
        table_name,
        parse_one("SELECT a, ds FROM tbl"),
        partitioned_by=[
            d.parse_one("DATETIME_TRUNC(ds, MONTH)"),
        ],
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("DATETIME"),
        },
    )

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE SCHEMA IF NOT EXISTS `test_schema`",
        f"CREATE TABLE IF NOT EXISTS `test_schema`.`__temp_test_table_{temp_table_id}` PARTITION BY DATETIME_TRUNC(`ds`, MONTH) AS SELECT `a`, `ds` FROM `tbl`",
        f"DECLARE _sqlmesh_target_partitions_ ARRAY<DATETIME> DEFAULT (SELECT ARRAY_AGG(PARSE_DATETIME('%Y%m', partition_id)) FROM `test_project`.`test_schema`.`INFORMATION_SCHEMA.PARTITIONS` AS PARTITIONS WHERE table_name = '__temp_test_table_{temp_table_id}' AND NOT partition_id IS NULL AND partition_id <> '__NULL__');",
        f"MERGE INTO `test_schema`.`test_table` AS `__MERGE_TARGET__` USING (SELECT `a`, `ds` FROM (SELECT * FROM `test_schema`.`__temp_test_table_{temp_table_id}`) AS `_subquery` WHERE DATETIME_TRUNC(`ds`, MONTH) IN UNNEST(`_sqlmesh_target_partitions_`)) AS `__MERGE_SOURCE__` ON FALSE WHEN NOT MATCHED BY SOURCE AND DATETIME_TRUNC(`ds`, MONTH) IN UNNEST(`_sqlmesh_target_partitions_`) THEN DELETE WHEN NOT MATCHED THEN INSERT (`a`, `ds`) VALUES (`a`, `ds`)",
        f"DROP TABLE IF EXISTS `test_schema`.`__temp_test_table_{temp_table_id}`",
    ]


def test_insert_overwrite_by_partition_query_unknown_column_types(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    columns_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.columns"
    )
    columns_mock.return_value = {
        "a": exp.DataType.build("int"),
        "ds": exp.DataType.build("DATETIME"),
    }
    adapter._default_catalog = "test_project"
    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "test_schema.test_table"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    adapter.insert_overwrite_by_partition(
        table_name,
        parse_one("SELECT a, ds FROM tbl"),
        partitioned_by=[
            d.parse_one("DATETIME_TRUNC(ds, MONTH)"),
        ],
        target_columns_to_types={
            "a": exp.DataType.build("unknown"),
            "ds": exp.DataType.build("UNKNOWN"),
        },
    )

    columns_mock.assert_called_once_with(table_name)

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE SCHEMA IF NOT EXISTS `test_schema`",
        f"CREATE TABLE IF NOT EXISTS `test_schema`.`__temp_test_table_{temp_table_id}` PARTITION BY DATETIME_TRUNC(`ds`, MONTH) AS SELECT `a`, `ds` FROM `tbl`",
        f"DECLARE _sqlmesh_target_partitions_ ARRAY<DATETIME> DEFAULT (SELECT ARRAY_AGG(PARSE_DATETIME('%Y%m', partition_id)) FROM `test_project`.`test_schema`.`INFORMATION_SCHEMA.PARTITIONS` AS PARTITIONS WHERE table_name = '__temp_test_table_{temp_table_id}' AND NOT partition_id IS NULL AND partition_id <> '__NULL__');",
        f"MERGE INTO `test_schema`.`test_table` AS `__MERGE_TARGET__` USING (SELECT `a`, `ds` FROM (SELECT * FROM `test_schema`.`__temp_test_table_{temp_table_id}`) AS `_subquery` WHERE DATETIME_TRUNC(`ds`, MONTH) IN UNNEST(`_sqlmesh_target_partitions_`)) AS `__MERGE_SOURCE__` ON FALSE WHEN NOT MATCHED BY SOURCE AND DATETIME_TRUNC(`ds`, MONTH) IN UNNEST(`_sqlmesh_target_partitions_`) THEN DELETE WHEN NOT MATCHED THEN INSERT (`a`, `ds`) VALUES (`a`, `ds`)",
        f"DROP TABLE IF EXISTS `test_schema`.`__temp_test_table_{temp_table_id}`",
    ]


def test_insert_overwrite_by_time_partition_pandas(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    temp_table_exists_counter = 0

    def temp_table_exists(table: exp.Table) -> bool:
        nonlocal temp_table_exists_counter
        temp_table_exists_counter += 1
        if table.sql() == "project.dataset.temp_table" and temp_table_exists_counter == 1:
            return False
        return True

    mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.table_exists",
        side_effect=temp_table_exists,
    )

    get_temp_bq_table = mocker.Mock()
    get_temp_bq_table.return_value = AttributeDict(
        {"project": "project", "dataset_id": "dataset", "table_id": "temp_table"}
    )
    adapter._BigQueryEngineAdapter__get_temp_bq_table = get_temp_bq_table
    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    db_call_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._db_call"
    )
    retry_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._BigQueryEngineAdapter__retry"
    )
    retry_resp = mocker.MagicMock()
    retry_resp_call = mocker.MagicMock()
    retry_resp.return_value = retry_resp_call
    retry_resp_call.errors = None
    retry_mock.return_value = retry_resp
    db_call_mock.return_value = AttributeDict({"errors": None})
    df = pd.DataFrame({"a": [1, 2, 3], "ds": ["2020-01-01", "2020-01-02", "2020-01-03"]})
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        df,
        start="2022-01-01",
        end="2022-01-05",
        time_formatter=lambda x, _: exp.Literal.string(x.strftime("%Y-%m-%d")),
        time_column="ds",
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("string"),
        },
    )
    assert execute_mock.call_count == 2
    assert retry_resp.call_count == 1
    assert db_call_mock.call_count == 1

    create_temp_table = db_call_mock.call_args_list[0]
    load_temp_table = retry_resp.call_args_list[0]
    merge, drop_temp_table = execute_mock.call_args_list
    merge_sql = merge[0][0]
    drop_temp_table_sql = drop_temp_table[0][0]

    assert create_temp_table.kwargs == {
        "exists_ok": False,
        "table": get_temp_bq_table.return_value,
    }
    assert sorted(load_temp_table.kwargs) == [
        "df",
        "job_config",
        "table",
    ]
    assert load_temp_table.kwargs["df"].equals(df)
    assert load_temp_table.kwargs["table"] == get_temp_bq_table.return_value
    assert load_temp_table.kwargs["job_config"].write_disposition is None
    assert (
        merge_sql.sql(dialect="bigquery")
        == "MERGE INTO test_table AS __MERGE_TARGET__ USING (SELECT `a`, `ds` FROM (SELECT CAST(`a` AS INT64) AS `a`, CAST(`ds` AS STRING) AS `ds` FROM project.dataset.temp_table) AS _subquery WHERE ds BETWEEN '2022-01-01' AND '2022-01-05') AS __MERGE_SOURCE__ ON FALSE WHEN NOT MATCHED BY SOURCE AND ds BETWEEN '2022-01-01' AND '2022-01-05' THEN DELETE WHEN NOT MATCHED THEN INSERT (a, ds) VALUES (a, ds)"
    )
    assert (
        drop_temp_table_sql.sql(dialect="bigquery")
        == "DROP TABLE IF EXISTS project.dataset.temp_table"
    )


def test_replace_query(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter.replace_query(
        "test_table", parse_one("SELECT a FROM tbl"), {"a": exp.DataType.build("INT")}
    )

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE OR REPLACE TABLE `test_table` AS SELECT CAST(`a` AS INT64) AS `a` FROM (SELECT `a` FROM `tbl`) AS `_subquery`"
    ]


def test_replace_query_pandas(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    get_bq_table_value = AttributeDict(
        {"project": "project", "dataset_id": "dataset", "table_id": "temp_table"}
    )
    get_bq_table = mocker.Mock()
    get_bq_table.return_value = get_bq_table_value
    adapter._BigQueryEngineAdapter__get_bq_table = get_bq_table
    temp_table_exists_counter = 0

    def temp_table_exists(table: exp.Table) -> bool:
        nonlocal temp_table_exists_counter
        temp_table_exists_counter += 1
        if table.sql() == "project.dataset.temp_table" and temp_table_exists_counter == 1:
            return False
        return True

    mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.table_exists",
        side_effect=temp_table_exists,
    )
    db_call_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._db_call"
    )
    retry_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._BigQueryEngineAdapter__retry"
    )
    retry_resp = mocker.MagicMock()
    retry_resp_call = mocker.MagicMock()
    retry_resp.return_value = retry_resp_call
    retry_resp_call.errors = None
    retry_mock.return_value = retry_resp
    db_call_mock.return_value = AttributeDict({"errors": None})

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.replace_query(
        "test_table", df, {"a": exp.DataType.build("int"), "b": exp.DataType.build("int")}
    )

    assert db_call_mock.call_count == 1
    assert retry_resp.call_count == 1
    create_table = db_call_mock.call_args_list[0]
    load_table = retry_resp.call_args_list[0]
    assert create_table.kwargs == {
        "table": get_bq_table_value,
        "exists_ok": False,
    }
    assert sorted(load_table.kwargs) == [
        "df",
        "job_config",
        "table",
    ]
    assert load_table.kwargs["df"].equals(df)
    assert load_table.kwargs["table"] == get_bq_table_value
    assert load_table.kwargs["job_config"].write_disposition is None
    assert load_table.kwargs["job_config"].schema == [
        bigquery.SchemaField("a", "INT64"),
        bigquery.SchemaField("b", "INT64"),
    ]
    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE OR REPLACE TABLE `test_table` AS SELECT CAST(`a` AS INT64) AS `a`, CAST(`b` AS INT64) AS `b` FROM (SELECT CAST(`a` AS INT64) AS `a`, CAST(`b` AS INT64) AS `b` FROM `project`.`dataset`.`temp_table`) AS `_subquery`",
        "DROP TABLE IF EXISTS `project`.`dataset`.`temp_table`",
    ]


@pytest.mark.parametrize(
    "partition_by_cols, partition_by_statement",
    [
        ([exp.to_column("ds")], "`ds`"),
        ([d.parse_one("DATE_TRUNC(ds, MONTH)", dialect="bigquery")], "DATE_TRUNC(`ds`, MONTH)"),
    ],
)
def test_create_table_date_partition(
    make_mocked_engine_adapter: t.Callable,
    partition_by_cols,
    partition_by_statement,
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("int"), "b": exp.DataType.build("int")},
        partitioned_by=partition_by_cols,
        partition_interval_unit=IntervalUnit.DAY,
        clustered_by=[exp.column("b")],
    )

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT64, `b` INT64) PARTITION BY {partition_by_statement} CLUSTER BY `b`"
    ]


@pytest.mark.parametrize(
    "partition_by_cols, partition_column_type, partition_interval_unit, partition_by_statement",
    [
        ([exp.to_column("ds")], "date", IntervalUnit.FIVE_MINUTE, "`ds`"),
        ([exp.to_column("ds")], "date", IntervalUnit.HOUR, "`ds`"),
        ([exp.to_column("ds")], "date", IntervalUnit.DAY, "`ds`"),
        ([exp.to_column("ds")], "date", IntervalUnit.MONTH, "DATE_TRUNC(`ds`, MONTH)"),
        ([exp.to_column("ds")], "date", IntervalUnit.YEAR, "DATE_TRUNC(`ds`, YEAR)"),
        ([exp.to_column("ds")], "datetime", IntervalUnit.HOUR, "DATETIME_TRUNC(`ds`, HOUR)"),
        ([exp.to_column("ds")], "datetime", IntervalUnit.DAY, "DATETIME_TRUNC(`ds`, DAY)"),
        ([exp.to_column("ds")], "datetime", IntervalUnit.MONTH, "DATETIME_TRUNC(`ds`, MONTH)"),
        ([exp.to_column("ds")], "datetime", IntervalUnit.YEAR, "DATETIME_TRUNC(`ds`, YEAR)"),
        ([exp.to_column("ds")], "timestamp", IntervalUnit.HOUR, "TIMESTAMP_TRUNC(`ds`, HOUR)"),
        ([exp.to_column("ds")], "timestamp", IntervalUnit.DAY, "TIMESTAMP_TRUNC(`ds`, DAY)"),
        ([exp.to_column("ds")], "timestamp", IntervalUnit.MONTH, "TIMESTAMP_TRUNC(`ds`, MONTH)"),
        ([exp.to_column("ds")], "timestamp", IntervalUnit.YEAR, "TIMESTAMP_TRUNC(`ds`, YEAR)"),
        (
            [d.parse_one("TIMESTAMP_TRUNC(ds, HOUR)", dialect="bigquery")],
            "timestamp",
            IntervalUnit.HOUR,
            "TIMESTAMP_TRUNC(`ds`, HOUR)",
        ),
        (
            [d.parse_one("TIMESTAMP_TRUNC(ds, HOUR)", dialect="bigquery")],
            "timestamp",
            IntervalUnit.DAY,
            "TIMESTAMP_TRUNC(`ds`, HOUR)",
        ),
        (
            [d.parse_one("TIMESTAMP_TRUNC(ds, DAY)", dialect="bigquery")],
            "timestamp",
            IntervalUnit.FIVE_MINUTE,
            "TIMESTAMP_TRUNC(`ds`, DAY)",
        ),
    ],
)
def test_create_table_time_partition(
    make_mocked_engine_adapter: t.Callable,
    partition_by_cols,
    partition_column_type,
    partition_interval_unit,
    partition_by_statement,
    mocker: MockerFixture,
):
    partition_column_sql_type = exp.DataType.build(partition_column_type, dialect="bigquery")

    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter.create_table(
        "test_table",
        {
            "a": exp.DataType.build("int"),
            "b": exp.DataType.build("int"),
            "ds": partition_column_sql_type,
        },
        partitioned_by=partition_by_cols,
        partition_interval_unit=partition_interval_unit,
    )

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT64, `b` INT64, `ds` {partition_column_sql_type.sql(dialect='bigquery')}) PARTITION BY {partition_by_statement}"
    ]


def test_ctas_time_partition(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter.ctas(
        "test_table",
        exp.select("*").from_("a"),
        partitioned_by=[exp.column("ds")],
        partition_interval_unit=IntervalUnit.HOUR,
    )

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE TABLE IF NOT EXISTS `test_table` PARTITION BY `ds` AS SELECT * FROM `a`",
    ]


def test_merge(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )
    adapter.merge(
        target_table="target",
        source_table=parse_one("SELECT id, ts, val FROM source"),
        target_columns_to_types={
            "id": exp.DataType.Type.INT,
            "ts": exp.DataType.Type.TIMESTAMP,
            "val": exp.DataType.Type.INT,
        },
        unique_key=[exp.to_identifier("id")],
    )
    sql_calls = _to_sql_calls(execute_mock, identify=False)
    assert sql_calls == [
        "MERGE INTO target AS __MERGE_TARGET__ USING (SELECT id, ts, val FROM source) AS __MERGE_SOURCE__ ON __MERGE_TARGET__.id = __MERGE_SOURCE__.id "
        "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.id = __MERGE_SOURCE__.id, __MERGE_TARGET__.ts = __MERGE_SOURCE__.ts, __MERGE_TARGET__.val = __MERGE_SOURCE__.val "
        "WHEN NOT MATCHED THEN INSERT (id, ts, val) VALUES (__MERGE_SOURCE__.id, __MERGE_SOURCE__.ts, __MERGE_SOURCE__.val)"
    ]


def test_merge_pandas(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    temp_table_exists_counter = 0

    def temp_table_exists(table: exp.Table) -> bool:
        nonlocal temp_table_exists_counter
        temp_table_exists_counter += 1
        if table.sql() == "project.dataset.temp_table" and temp_table_exists_counter == 1:
            return False
        return True

    mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.table_exists",
        side_effect=temp_table_exists,
    )

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    get_temp_bq_table = mocker.Mock()
    get_temp_bq_table.return_value = AttributeDict(
        {"project": "project", "dataset_id": "dataset", "table_id": "temp_table"}
    )
    adapter._BigQueryEngineAdapter__get_temp_bq_table = get_temp_bq_table
    db_call_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._db_call"
    )
    retry_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._BigQueryEngineAdapter__retry"
    )
    retry_resp = mocker.MagicMock()
    retry_resp_call = mocker.MagicMock()
    retry_resp.return_value = retry_resp_call
    retry_resp_call.errors = None
    retry_mock.return_value = retry_resp
    db_call_mock.return_value = AttributeDict({"errors": None})
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "ts": ["2025-01-01 00:00:00", "2025-01-01 00:00:00", "2025-01-01 00:00:00"],
            "val": [7, 8, 9],
        }
    )
    adapter.merge(
        target_table="target",
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "ts": exp.DataType.build("TIMESTAMP"),
            "val": exp.DataType.build("INT"),
        },
        unique_key=[exp.to_identifier("id")],
    )

    sql_calls = _to_sql_calls(execute_mock, identify=False)
    assert sql_calls == [
        "MERGE INTO target AS __MERGE_TARGET__ USING (SELECT CAST(`id` AS INT64) AS `id`, CAST(`ts` AS DATETIME) AS `ts`, CAST(`val` AS INT64) AS `val` FROM project.dataset.temp_table) AS __MERGE_SOURCE__ ON __MERGE_TARGET__.id = __MERGE_SOURCE__.id "
        "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.id = __MERGE_SOURCE__.id, __MERGE_TARGET__.ts = __MERGE_SOURCE__.ts, __MERGE_TARGET__.val = __MERGE_SOURCE__.val "
        "WHEN NOT MATCHED THEN INSERT (id, ts, val) VALUES (__MERGE_SOURCE__.id, __MERGE_SOURCE__.ts, __MERGE_SOURCE__.val)",
        "DROP TABLE IF EXISTS project.dataset.temp_table",
    ]
    assert retry_resp.call_count == 1
    assert db_call_mock.call_count == 1
    create_temp_table = db_call_mock.call_args_list[0]
    load_temp_table = retry_resp.call_args_list[0]
    assert create_temp_table.kwargs == {
        "exists_ok": False,
        "table": get_temp_bq_table.return_value,
    }
    assert sorted(load_temp_table.kwargs) == [
        "df",
        "job_config",
        "table",
    ]


def test_begin_end_session(mocker: MockerFixture):
    connection_mock = mocker.NonCallableMock()
    cursor_mock = mocker.Mock()
    cursor_mock.connection = connection_mock
    connection_mock.cursor.return_value = cursor_mock

    query_result_mock = mocker.Mock()
    query_result_mock.total_rows = 0
    job_mock = mocker.Mock()
    job_mock.result.return_value = query_result_mock
    connection_mock._client.query.return_value = job_mock

    adapter = BigQueryEngineAdapter(lambda: connection_mock, job_retries=0)

    # starting a session without session properties
    with adapter.session({}):
        assert adapter._connection_pool.get_attribute("session_id") is not None
        adapter.execute("SELECT 2;")

    assert adapter._connection_pool.get_attribute("session_id") is None
    adapter.execute("SELECT 3;")

    begin_session_call = connection_mock._client.query.call_args_list[0]
    assert begin_session_call[0][0] == "SELECT 1;"

    execute_a_call = connection_mock._client.query.call_args_list[1]
    assert execute_a_call[1]["query"] == "SELECT 2;"
    assert len(execute_a_call[1]["job_config"].connection_properties) == 1
    assert execute_a_call[1]["job_config"].connection_properties[0].key == "session_id"
    assert execute_a_call[1]["job_config"].connection_properties[0].value

    execute_b_call = connection_mock._client.query.call_args_list[2]
    assert execute_b_call[1]["query"] == "SELECT 3;"
    assert not execute_b_call[1]["job_config"].connection_properties

    # starting a new session with session property query_label and array value
    with adapter.session({"query_label": parse_one("[('key1', 'value1'), ('key2', 'value2')]")}):
        adapter.execute("SELECT 4;")
    begin_new_session_call = connection_mock._client.query.call_args_list[3]
    assert begin_new_session_call[0][0] == 'SET @@query_label = "key1:value1,key2:value2";SELECT 1;'

    # starting a new session with session property query_label and Paren value
    with adapter.session({"query_label": parse_one("(('key1', 'value1'))")}):
        adapter.execute("SELECT 5;")
    begin_new_session_call = connection_mock._client.query.call_args_list[5]
    assert begin_new_session_call[0][0] == 'SET @@query_label = "key1:value1";SELECT 1;'

    # test invalid query_label value
    with pytest.raises(
        SQLMeshError,
        match="Invalid value for `session_properties.query_label`. Must be an array or tuple.",
    ):
        with adapter.session({"query_label": parse_one("'key1:value1'")}):
            adapter.execute("SELECT 6;")


def _to_sql_calls(execute_mock: t.Any, identify: bool = True) -> t.List[str]:
    if isinstance(execute_mock, BigQueryEngineAdapter):
        execute_mock = execute_mock.execute
    output = []
    for call in execute_mock.call_args_list:
        values = ensure_list(call[0][0])
        for value in values:
            sql = (
                value.sql(dialect="bigquery", identify=identify)
                if isinstance(value, exp.Expr)
                else str(value)
            )
            output.append(sql)
    return output


def test_create_table_table_options(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("int"), "b": exp.DataType.build("int")},
        table_properties={
            "partition_expiration_days": exp.convert(7),
        },
    )

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT64, `b` INT64) OPTIONS (partition_expiration_days=7)"
    ]


def test_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    allowed_table_comment_length = BigQueryEngineAdapter.MAX_TABLE_COMMENT_LENGTH
    truncated_table_comment = "a" * allowed_table_comment_length
    long_table_comment = truncated_table_comment + "b"

    allowed_column_comment_length = BigQueryEngineAdapter.MAX_COLUMN_COMMENT_LENGTH
    truncated_column_comment = "c" * allowed_column_comment_length
    long_column_comment = truncated_column_comment + "d"

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description="\\",
        column_descriptions={"a": "\\"},
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter._create_table_comment(
        "test_table",
        long_table_comment,
    )

    # Only called if column comments are registered
    get_table_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._get_table"
    )

    db_call_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter._db_call"
    )

    adapter.create_view(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )
    assert get_table_mock.called

    # Bigquery doesn't support column comments for materialized views
    db_call_mock.reset_mock()

    adapter.create_view(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
        materialized=True,
    )
    assert not db_call_mock.called

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT64 OPTIONS (description='{truncated_column_comment}'), `b` INT64) OPTIONS (description='{truncated_table_comment}')",
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT64 OPTIONS (description='\\\\'), `b` INT64) OPTIONS (description='\\\\')",
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT64 OPTIONS (description='{truncated_column_comment}'), `b` INT64) OPTIONS (description='{truncated_table_comment}') AS SELECT CAST(`a` AS INT64) AS `a`, CAST(`b` AS INT64) AS `b` FROM (SELECT `a`, `b` FROM `source_table`) AS `_subquery`",
        f"ALTER TABLE `test_table` SET OPTIONS(description = '{truncated_table_comment}')",
        f"CREATE OR REPLACE VIEW `test_table` OPTIONS (description='{truncated_table_comment}') AS SELECT `a`, `b` FROM `source_table`",
        f"CREATE OR REPLACE MATERIALIZED VIEW `test_table` OPTIONS (description='{truncated_table_comment}') AS SELECT `a`, `b` FROM `source_table`",
    ]


def test_nested_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    allowed_column_comment_length = BigQueryEngineAdapter.MAX_COLUMN_COMMENT_LENGTH

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    nested_columns_to_types = {
        "record with space": exp.DataType.build(
            "STRUCT<`int_field` INT, `record_field` STRUCT<`sub_record_field` STRUCT<`nest_array` ARRAY<INT64>>>>",
            dialect="bigquery",
        ),
        "repeated_record": exp.DataType.build(
            "ARRAY<STRUCT<`nested_repeated_record` ARRAY<STRUCT<`int_field` INT, `array_field` ARRAY<INT64>, `struct_field with space` STRUCT<`nested_field` INT>>>>",
            dialect="bigquery",
        ),
        "same_name_": exp.DataType.build(
            "ARRAY<STRUCT<`same_name_` ARRAY<STRUCT<`same_name_` STRUCT<`same_name_` INT>>>>",
            dialect="bigquery",
        ),
    }

    long_column_descriptions = {
        "record with space": "Top Record",
        "record with space.int_field": "Record Int Field",
        "record with space.record_field": "Record Nested Record Field",
        "record with space.record_field.sub_record_field": "Record Nested Records Subfield",
        "record with space.record_field.sub_record_field.nest_array": "Record Nested Records Nested Array",
        "repeated_record": "Top Repeated Record",
        "repeated_record.nested_repeated_record": "Nested Repeated Record",
        "repeated_record.nested_repeated_record.int_field": "Nested Repeated Record Int Field",
        "repeated_record.nested_repeated_record.array_field": "Nested Repeated Array Field",
        "repeated_record.nested_repeated_record.struct_field with space": "Nested Repeated Struct Field",
        "repeated_record.nested_repeated_record.struct_field with space.nested_field": "Nested Repeated Record Nested Field",
        "same_name_": "Level 1",
        "same_name_.same_name_.same_name_": "Level 3",
        "same_name_.same_name_.same_name_.same_name_": "4" * allowed_column_comment_length + "X",
    }

    adapter.create_table(
        "test_table",
        target_columns_to_types=nested_columns_to_types,
        column_descriptions=long_column_descriptions,
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT * FROM source_table"),
        target_columns_to_types=nested_columns_to_types,
        column_descriptions=long_column_descriptions,
    )

    sql_calls = _to_sql_calls(execute_mock)

    # The comments should be added in the correct nested field with appropriate truncation
    assert sql_calls[0] == (
        "CREATE TABLE IF NOT EXISTS `test_table` ("
        "`record with space` STRUCT<"
        "`int_field` INT64 OPTIONS (description='Record Int Field'), "
        "`record_field` STRUCT<"
        "`sub_record_field` STRUCT<"
        "`nest_array` ARRAY<INT64> OPTIONS (description='Record Nested Records Nested Array')> "
        "OPTIONS (description='Record Nested Records Subfield')> "
        "OPTIONS (description='Record Nested Record Field')> "
        "OPTIONS (description='Top Record'), "
        "`repeated_record` ARRAY<STRUCT<"
        "`nested_repeated_record` ARRAY<STRUCT<"
        "`int_field` INT64 OPTIONS (description='Nested Repeated Record Int Field'), "
        "`array_field` ARRAY<INT64> OPTIONS (description='Nested Repeated Array Field'), "
        "`struct_field with space` STRUCT<"
        "`nested_field` INT64 OPTIONS (description='Nested Repeated Record Nested Field')> "
        "OPTIONS (description='Nested Repeated Struct Field')>> "
        "OPTIONS (description='Nested Repeated Record')>> "
        "OPTIONS (description='Top Repeated Record'), "
        "`same_name_` ARRAY<STRUCT<"
        "`same_name_` ARRAY<STRUCT<"
        "`same_name_` STRUCT<"
        f"`same_name_` INT64 OPTIONS (description='{'4' * allowed_column_comment_length}')> "
        "OPTIONS (description='Level 3')>>>> "
        "OPTIONS (description='Level 1'))"
    )

    assert sql_calls[1] == (
        "CREATE TABLE IF NOT EXISTS `test_table` ("
        "`record with space` STRUCT<"
        "`int_field` INT64 OPTIONS (description='Record Int Field'), "
        "`record_field` STRUCT<"
        "`sub_record_field` STRUCT<"
        "`nest_array` ARRAY<INT64> OPTIONS (description='Record Nested Records Nested Array')> "
        "OPTIONS (description='Record Nested Records Subfield')> "
        "OPTIONS (description='Record Nested Record Field')> "
        "OPTIONS (description='Top Record'), "
        "`repeated_record` ARRAY<STRUCT<"
        "`nested_repeated_record` ARRAY<STRUCT<"
        "`int_field` INT64 OPTIONS (description='Nested Repeated Record Int Field'), "
        "`array_field` ARRAY<INT64> OPTIONS (description='Nested Repeated Array Field'), "
        "`struct_field with space` STRUCT<"
        "`nested_field` INT64 OPTIONS (description='Nested Repeated Record Nested Field')> "
        "OPTIONS (description='Nested Repeated Struct Field')>> "
        "OPTIONS (description='Nested Repeated Record')>> "
        "OPTIONS (description='Top Repeated Record'), "
        "`same_name_` ARRAY<STRUCT<"
        "`same_name_` ARRAY<STRUCT<"
        "`same_name_` STRUCT<"
        f"`same_name_` INT64 OPTIONS (description='{'4' * allowed_column_comment_length}')> "
        "OPTIONS (description='Level 3')>>>> "
        "OPTIONS (description='Level 1'))"
        " AS SELECT CAST(`record with space` AS STRUCT<`int_field` INT64, `record_field` STRUCT<`sub_record_field` STRUCT<`nest_array` ARRAY<INT64>>>>) AS `record with space`, "
        "CAST(`repeated_record` AS ARRAY<STRUCT<`nested_repeated_record` ARRAY<STRUCT<`int_field` INT64, `array_field` ARRAY<INT64>, `struct_field with space` STRUCT<`nested_field` INT64>>>>>) AS `repeated_record`, "
        "CAST(`same_name_` AS ARRAY<STRUCT<`same_name_` ARRAY<STRUCT<`same_name_` STRUCT<`same_name_` INT64>>>>>) AS `same_name_` "
        "FROM (SELECT * FROM `source_table`) AS `_subquery`"
    )


def test_select_partitions_expr():
    assert (
        select_partitions_expr(
            "{{ adapter.resolve_schema(this) }}",
            "{{ adapter.resolve_identifier(this) }}",
            "date",
            granularity="day",
            catalog="{{ target.database }}",
        )
        == "SELECT MAX(PARSE_DATE('%Y%m%d', partition_id)) FROM `{{ target.database }}`.`{{ adapter.resolve_schema(this) }}`.`INFORMATION_SCHEMA.PARTITIONS` AS PARTITIONS WHERE table_name = '{{ adapter.resolve_identifier(this) }}' AND NOT partition_id IS NULL AND partition_id <> '__NULL__'"
    )

    assert (
        select_partitions_expr(
            "test_schema",
            "test_table",
            "int64",
        )
        == "SELECT MAX(CAST(partition_id AS INT64)) FROM `test_schema`.`INFORMATION_SCHEMA.PARTITIONS` AS PARTITIONS WHERE table_name = 'test_table' AND NOT partition_id IS NULL AND partition_id <> '__NULL__'"
    )


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (
            {
                "schema_name": "test_schema",
            },
            "DROP SCHEMA IF EXISTS `test_schema`",
        ),
        (
            {
                "schema_name": "test_schema",
                "ignore_if_not_exists": False,
            },
            "DROP SCHEMA `test_schema`",
        ),
        (
            {
                "schema_name": "test_schema",
                "cascade": True,
            },
            "DROP SCHEMA IF EXISTS `test_schema` CASCADE",
        ),
        (
            {
                "schema_name": "test_schema",
                "cascade": True,
                "ignore_if_not_exists": False,
            },
            "DROP SCHEMA `test_schema` CASCADE",
        ),
        (
            {
                "schema_name": "test_catalog.test_schema",
                "ignore_if_not_exists": True,
                "cascade": True,
            },
            "DROP SCHEMA IF EXISTS `test_catalog`.`test_schema` CASCADE",
        ),
    ],
)
def test_drop_schema(
    kwargs, expected, make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    adapter.drop_schema(**kwargs)

    sql_calls = _to_sql_calls(execute_mock)

    assert sql_calls == ensure_list(expected)


def test_view_properties(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        table_description="some description",
        view_properties={
            "labels": exp.array("('test-label', 'label-value')"),
        },
    )

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        table_description="some description",
        view_properties={
            "labels": exp.array("('test-view-label', 'label-view-value')"),
        },
    )

    adapter.create_view("test_table", parse_one("SELECT 1"), view_properties={})

    sql_calls = _to_sql_calls(execute_mock)
    assert sql_calls == [
        "CREATE OR REPLACE VIEW `test_table` OPTIONS (description='some description', labels=[('test-label', 'label-value')]) AS SELECT 1",
        "CREATE OR REPLACE VIEW `test_table` OPTIONS (description='some description', labels=[('test-view-label', 'label-view-value')]) AS SELECT 1",
        "CREATE OR REPLACE VIEW `test_table` AS SELECT 1",
    ]


def test_materialized_view_properties(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    execute_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.execute"
    )

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        materialized=True,
        materialized_properties={
            "partitioned_by": [exp.column("ds")],
            "clustered_by": [exp.column("a")],
            "partition_interval_unit": IntervalUnit.DAY,
        },
    )

    sql_calls = _to_sql_calls(execute_mock)
    # https://cloud.google.com/bigquery/docs/materialized-views-create#example_1
    assert sql_calls == [
        "CREATE OR REPLACE MATERIALIZED VIEW `test_table` PARTITION BY `ds` CLUSTER BY `a` AS SELECT 1",
    ]


def test_nested_fields_update(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)

    current_schema = [
        bigquery.SchemaField(
            "user",
            "RECORD",
            "NULLABLE",
            fields=(
                bigquery.SchemaField("name", "STRING", "NULLABLE"),
                bigquery.SchemaField(
                    "orders",
                    "RECORD",
                    "REPEATED",
                    fields=([bigquery.SchemaField("id", "INT64", "NULLABLE")]),
                ),
            ),
        )
    ]
    new_nested_fields = [("year", "INT64", ["user", "orders"]), ("active", "BOOL", ["user"])]
    expected = [
        bigquery.SchemaField(
            "user",
            "RECORD",
            "NULLABLE",
            fields=(
                bigquery.SchemaField("name", "STRING", "NULLABLE"),
                bigquery.SchemaField(
                    "orders",
                    "RECORD",
                    "REPEATED",
                    fields=(
                        bigquery.SchemaField("id", "INT64", "NULLABLE"),
                        bigquery.SchemaField("year", "INT64", "NULLABLE"),
                    ),
                ),
                bigquery.SchemaField("active", "BOOL", "NULLABLE"),
            ),
        )
    ]
    assert adapter._build_nested_fields(current_schema, new_nested_fields) == expected

    current_schema = [
        bigquery.SchemaField(
            "users",
            "RECORD",
            "REPEATED",
            fields=(
                [
                    bigquery.SchemaField(
                        "user",
                        "RECORD",
                        "NULLABLE",
                        fields=(bigquery.SchemaField("name", "STRING", "NULLABLE"),),
                    )
                ]
            ),
        )
    ]
    new_nested_fields = [
        ("orders", "ARRAY<INT64>", ["users", "user"]),
        ("tags", "STRING", ["users"]),
        ("details", "ARRAY<STRING>", []),
    ]
    expected = [
        bigquery.SchemaField(
            "users",
            "RECORD",
            "REPEATED",
            fields=(
                bigquery.SchemaField(
                    "user",
                    "RECORD",
                    "NULLABLE",
                    fields=(
                        bigquery.SchemaField("name", "STRING", "NULLABLE"),
                        bigquery.SchemaField("orders", "INT64", "REPEATED"),
                    ),
                ),
                bigquery.SchemaField(
                    "tags",
                    "STRING",
                    "NULLABLE",
                ),
            ),
        ),
        bigquery.SchemaField("details", "STRING", "REPEATED"),
    ]
    assert adapter._build_nested_fields(current_schema, new_nested_fields) == expected


def test_get_alter_expressions_includes_catalog(
    adapter: BigQueryEngineAdapter, mocker: MockerFixture
):
    adapter._default_catalog = "test_project"

    columns_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.columns"
    )
    columns_mock.return_value = {
        "a": exp.DataType.build("int"),
    }

    get_data_objects_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.bigquery.BigQueryEngineAdapter.get_data_objects"
    )
    get_data_objects_mock.return_value = []

    adapter.get_alter_operations("catalog1.foo.bar", "catalog2.bar.bing")

    assert get_data_objects_mock.call_count == 2

    schema, tables = get_data_objects_mock.call_args_list[0][0]
    assert isinstance(schema, exp.Table)
    assert isinstance(tables, set)
    assert schema.catalog == "catalog1"
    assert schema.db == "foo"
    assert schema.sql(dialect="bigquery") == "catalog1.foo"
    assert tables == {"bar"}

    schema, tables = get_data_objects_mock.call_args_list[1][0]
    assert isinstance(schema, exp.Table)
    assert isinstance(tables, set)
    assert schema.catalog == "catalog2"
    assert schema.db == "bar"
    assert schema.sql(dialect="bigquery") == "catalog2.bar"
    assert tables == {"bing"}


def test_job_cancellation_on_keyboard_interrupt_job_still_running(mocker: MockerFixture):
    # Create a mock connection
    connection_mock = mocker.NonCallableMock()
    cursor_mock = mocker.Mock()
    cursor_mock.connection = connection_mock
    connection_mock.cursor.return_value = cursor_mock

    # Mock the query job
    mock_job = mocker.Mock()
    mock_job.project = "test-project"
    mock_job.location = "us-central1"
    mock_job.job_id = "test-job-123"
    mock_job.done.return_value = False  # Job is still running
    mock_job.result.side_effect = KeyboardInterrupt()
    mock_job._query_results = mocker.Mock()
    mock_job._query_results.total_rows = 0
    mock_job._query_results.schema = []

    # Set up the client to return our mock job
    connection_mock._client.query.return_value = mock_job

    # Create adapter with the mocked connection
    adapter = BigQueryEngineAdapter(lambda: connection_mock, job_retries=0)

    # Execute a query and expect KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        adapter.execute("SELECT 1")

    # Ensure the adapter's closed, so that the job can be aborted
    adapter.close()

    # Verify the job was created
    connection_mock._client.query.assert_called_once()

    # Verify job status was checked and cancellation was called
    mock_job.done.assert_called_once()
    mock_job.cancel.assert_called_once()


def test_job_cancellation_on_keyboard_interrupt_job_already_done(mocker: MockerFixture):
    # Create a mock connection
    connection_mock = mocker.NonCallableMock()
    cursor_mock = mocker.Mock()
    cursor_mock.connection = connection_mock
    connection_mock.cursor.return_value = cursor_mock

    # Mock the query job
    mock_job = mocker.Mock()
    mock_job.project = "test-project"
    mock_job.location = "us-central1"
    mock_job.job_id = "test-job-456"
    mock_job.done.return_value = True  # Job is already done
    mock_job.result.side_effect = KeyboardInterrupt()
    mock_job._query_results = mocker.Mock()
    mock_job._query_results.total_rows = 0
    mock_job._query_results.schema = []

    # Set up the client to return our mock job
    connection_mock._client.query.return_value = mock_job

    # Create adapter with the mocked connection
    adapter = BigQueryEngineAdapter(lambda: connection_mock, job_retries=0)

    # Execute a query and expect KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        adapter.execute("SELECT 1")

    # Ensure the adapter's closed, so that the job can be aborted
    adapter.close()

    # Verify the job was created
    connection_mock._client.query.assert_called_once()

    # Verify job status was checked but cancellation was NOT called
    mock_job.done.assert_called_once()
    mock_job.cancel.assert_not_called()


def test_drop_cascade(adapter: BigQueryEngineAdapter):
    adapter.drop_table("foo", cascade=True)
    adapter.drop_table("foo", cascade=False)

    # BigQuery doesnt support DROP CASCADE for tables
    # ref: https://cloud.google.com/bigquery/docs/reference/standard-sql/data-definition-language#drop_table_statement
    assert _to_sql_calls(adapter) == ["DROP TABLE IF EXISTS `foo`", "DROP TABLE IF EXISTS `foo`"]
    adapter.execute.reset_mock()  # type: ignore

    # But, it does for schemas
    adapter.drop_schema("foo", cascade=True)
    adapter.drop_schema("foo", cascade=False)

    assert _to_sql_calls(adapter) == [
        "DROP SCHEMA IF EXISTS `foo` CASCADE",
        "DROP SCHEMA IF EXISTS `foo`",
    ]


def test_scd_type_2_by_partitioning(adapter: BigQueryEngineAdapter):
    adapter.scd_type_2_by_time(
        target_table="target",
        source_table=t.cast(
            exp.Select, parse_one("SELECT id, name, price, test_UPDATED_at FROM source")
        ),
        unique_key=[
            exp.to_column("id"),
        ],
        updated_at_col=exp.column("test_UPDATED_at", quoted=True),
        valid_from_col=exp.to_column("valid_from", quoted=True),
        valid_to_col=exp.to_column("valid_to", quoted=True),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_UPDATED_at": exp.DataType.build("TIMESTAMP"),
            "valid_from": exp.DataType.build("TIMESTAMP"),
            "valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
        partitioned_by=[parse_one("TIMESTAMP_TRUNC(valid_from, DAY)")],
    )

    calls = _to_sql_calls(adapter)

    # Initial call to create the table and then another to replace since it is self-referencing
    assert len(calls) == 2
    # Both calls should contain the partition logic (the scd logic is already covered by other tests)
    assert "PARTITION BY TIMESTAMP_TRUNC(`valid_from`, DAY)" in calls[0]
    assert "PARTITION BY TIMESTAMP_TRUNC(`valid_from`, DAY)" in calls[1]


def test_sync_grants_config(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    relation = exp.to_table("project.dataset.test_table", dialect="bigquery")
    new_grants_config = {
        "roles/bigquery.dataViewer": ["user:analyst@example.com", "group:data-team@example.com"],
        "roles/bigquery.dataEditor": ["user:admin@example.com"],
    }
    current_grants = [
        ("roles/bigquery.dataViewer", "user:old_analyst@example.com"),
        ("roles/bigquery.admin", "user:old_admin@example.com"),
    ]

    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)
    execute_mock = mocker.patch.object(adapter, "execute")
    mocker.patch.object(adapter, "get_current_catalog", return_value="project")
    mocker.patch.object(adapter.client, "location", "us-central1")

    mock_dataset = mocker.Mock()
    mock_dataset.location = "us-central1"
    mocker.patch.object(adapter, "_db_call", return_value=mock_dataset)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="bigquery")
    expected_sql = (
        "SELECT privilege_type, grantee FROM `project`.`region-us-central1`.`INFORMATION_SCHEMA.OBJECT_PRIVILEGES` AS OBJECT_PRIVILEGES "
        "WHERE object_schema = 'dataset' AND object_name = 'test_table' AND SPLIT(grantee, ':')[OFFSET(1)] <> SESSION_USER()"
    )
    assert executed_sql == expected_sql

    sql_calls = _to_sql_calls(execute_mock)

    assert len(sql_calls) == 4
    assert (
        "REVOKE `roles/bigquery.dataViewer` ON TABLE `project`.`dataset`.`test_table` FROM 'user:old_analyst@example.com'"
        in sql_calls
    )
    assert (
        "REVOKE `roles/bigquery.admin` ON TABLE `project`.`dataset`.`test_table` FROM 'user:old_admin@example.com'"
        in sql_calls
    )
    assert (
        "GRANT `roles/bigquery.dataViewer` ON TABLE `project`.`dataset`.`test_table` TO 'user:analyst@example.com', 'group:data-team@example.com'"
        in sql_calls
    )
    assert (
        "GRANT `roles/bigquery.dataEditor` ON TABLE `project`.`dataset`.`test_table` TO 'user:admin@example.com'"
        in sql_calls
    )


def test_sync_grants_config_with_overlaps(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    relation = exp.to_table("project.dataset.test_table", dialect="bigquery")
    new_grants_config = {
        "roles/bigquery.dataViewer": [
            "user:analyst1@example.com",
            "user:analyst2@example.com",
            "user:analyst3@example.com",
        ],
        "roles/bigquery.dataEditor": ["user:analyst2@example.com", "user:editor@example.com"],
    }
    current_grants = [
        ("roles/bigquery.dataViewer", "user:analyst1@example.com"),  # Keep
        ("roles/bigquery.dataViewer", "user:old_analyst@example.com"),  # Remove
        ("roles/bigquery.dataEditor", "user:analyst2@example.com"),  # Keep
        ("roles/bigquery.admin", "user:admin@example.com"),  # Remove
    ]

    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)
    execute_mock = mocker.patch.object(adapter, "execute")
    mocker.patch.object(adapter, "get_current_catalog", return_value="project")
    mocker.patch.object(adapter.client, "location", "us-central1")

    mock_dataset = mocker.Mock()
    mock_dataset.location = "us-central1"
    mocker.patch.object(adapter, "_db_call", return_value=mock_dataset)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="bigquery")
    expected_sql = (
        "SELECT privilege_type, grantee FROM `project`.`region-us-central1`.`INFORMATION_SCHEMA.OBJECT_PRIVILEGES` AS OBJECT_PRIVILEGES "
        "WHERE object_schema = 'dataset' AND object_name = 'test_table' AND SPLIT(grantee, ':')[OFFSET(1)] <> SESSION_USER()"
    )
    assert executed_sql == expected_sql

    sql_calls = _to_sql_calls(execute_mock)

    assert len(sql_calls) == 4
    assert (
        "REVOKE `roles/bigquery.dataViewer` ON TABLE `project`.`dataset`.`test_table` FROM 'user:old_analyst@example.com'"
        in sql_calls
    )
    assert (
        "REVOKE `roles/bigquery.admin` ON TABLE `project`.`dataset`.`test_table` FROM 'user:admin@example.com'"
        in sql_calls
    )
    assert (
        "GRANT `roles/bigquery.dataViewer` ON TABLE `project`.`dataset`.`test_table` TO 'user:analyst2@example.com', 'user:analyst3@example.com'"
        in sql_calls
    )
    assert (
        "GRANT `roles/bigquery.dataEditor` ON TABLE `project`.`dataset`.`test_table` TO 'user:editor@example.com'"
        in sql_calls
    )


@pytest.mark.parametrize(
    "table_type, expected_keyword",
    [
        (DataObjectType.TABLE, "TABLE"),
        (DataObjectType.VIEW, "VIEW"),
        (DataObjectType.MATERIALIZED_VIEW, "MATERIALIZED VIEW"),
    ],
)
def test_sync_grants_config_object_kind(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    table_type: DataObjectType,
    expected_keyword: str,
) -> None:
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    relation = exp.to_table("project.dataset.test_object", dialect="bigquery")

    mocker.patch.object(adapter, "fetchall", return_value=[])
    execute_mock = mocker.patch.object(adapter, "execute")
    mocker.patch.object(adapter, "get_current_catalog", return_value="project")
    mocker.patch.object(adapter.client, "location", "us-central1")

    mock_dataset = mocker.Mock()
    mock_dataset.location = "us-central1"
    mocker.patch.object(adapter, "_db_call", return_value=mock_dataset)

    adapter.sync_grants_config(
        relation, {"roles/bigquery.dataViewer": ["user:test@example.com"]}, table_type
    )

    executed_exprs = execute_mock.call_args[0][0]
    sql_calls = [expr.sql(dialect="bigquery") for expr in executed_exprs]
    assert sql_calls == [
        f"GRANT `roles/bigquery.dataViewer` ON {expected_keyword} project.dataset.test_object TO 'user:test@example.com'"
    ]


def test_sync_grants_config_no_schema(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(BigQueryEngineAdapter)
    relation = exp.to_table("test_table", dialect="bigquery")
    new_grants_config = {
        "roles/bigquery.dataViewer": ["user:analyst@example.com"],
        "roles/bigquery.dataEditor": ["user:editor@example.com"],
    }

    with pytest.raises(ValueError, match="Table test_table does not have a schema \\(dataset\\)"):
        adapter.sync_grants_config(relation, new_grants_config)
