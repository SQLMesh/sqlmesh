# type: ignore
import typing as t

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from unittest.mock import PropertyMock
from sqlglot import expressions as exp
from sqlglot import parse_one

import sqlmesh.core.dialect as d
from sqlmesh.core.engine_adapter import RedshiftEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObject, DataObjectType
from sqlmesh.core.schema_diff import TableAlterDropColumnOperation
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.model.definition import SqlModel
from sqlmesh.core.snapshot.evaluator import SCDType2Strategy
from sqlmesh.utils.errors import SQLMeshError
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.engine, pytest.mark.redshift]


@pytest.fixture
def adapter(make_mocked_engine_adapter):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    adapter.cursor.fetchall.return_value = []
    return adapter


def test_columns(adapter: t.Callable):
    adapter.cursor.fetchall.return_value = [("col", "INT")]
    resp = adapter.columns("db.table")
    adapter.cursor.execute.assert_called_once_with(
        """SELECT "column_name", "data_type", "character_maximum_length", "numeric_precision", "numeric_scale" FROM "svv_columns" WHERE "table_name" = 'table' AND "table_schema" = 'db'"""
    )
    assert resp == {"col": exp.DataType.build("INT")}


def test_create_table_physical_properties(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    adapter.create_table(
        "test_schema.test_table",
        {
            "id_file": exp.DataType.build("INT"),
            "batch_time": exp.DataType.build("TIMESTAMP"),
        },
        table_properties={
            "diststyle": exp.column("key"),
            "distkey": exp.to_column("id_file"),
            "sortkey": exp.to_column("batch_time"),
        },
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER, "batch_time" TIMESTAMP) DISTSTYLE KEY DISTKEY("id_file") SORTKEY("batch_time")',
    ]


@pytest.mark.parametrize(
    ("diststyle", "expected"),
    [
        ("auto", "AUTO"),
        ("even", "EVEN"),
        ("key", "KEY"),
        ("all", "ALL"),
    ],
)
def test_create_table_physical_properties_diststyle_values(
    make_mocked_engine_adapter: t.Callable,
    diststyle: str,
    expected: str,
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    table_properties = {"diststyle": exp.column(diststyle)}
    if diststyle == "key":
        table_properties["distkey"] = exp.to_column("id_file")

    adapter.create_table(
        "test_schema.test_table",
        {"id_file": exp.DataType.build("INT")},
        table_properties=table_properties,
    )

    expected_distkey = ' DISTKEY("id_file")' if diststyle == "key" else ""
    assert to_sql_calls(adapter) == [
        f'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER) DISTSTYLE {expected}{expected_distkey}',
    ]


def test_create_table_physical_properties_distkey_without_diststyle(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    adapter.create_table(
        "test_schema.test_table",
        {"id_file": exp.DataType.build("INT")},
        table_properties={"distkey": exp.to_column("id_file")},
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER) DISTKEY("id_file")',
    ]


def test_create_table_physical_properties_multi_column_sortkey(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    adapter.create_table(
        "test_schema.test_table",
        {
            "id_file": exp.DataType.build("INT"),
            "batch_time": exp.DataType.build("TIMESTAMP"),
            "event_time": exp.DataType.build("TIMESTAMP"),
        },
        table_properties={
            "diststyle": exp.column("key"),
            "distkey": exp.to_column("id_file"),
            "sortkey": exp.Tuple(
                expressions=[exp.to_column("batch_time"), exp.to_column("event_time")]
            ),
        },
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER, "batch_time" TIMESTAMP, "event_time" TIMESTAMP) DISTSTYLE KEY DISTKEY("id_file") SORTKEY("batch_time", "event_time")',
    ]


def test_create_table_physical_properties_with_string_columns(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    adapter.create_table(
        "test_schema.test_table",
        {
            "id_file": exp.DataType.build("INT"),
            "batch_time": exp.DataType.build("TIMESTAMP"),
        },
        table_properties={
            "diststyle": exp.Literal.string("key"),
            "distkey": exp.Literal.string("id_file"),
            "sortkey": exp.Literal.string("batch_time"),
        },
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER, "batch_time" TIMESTAMP) DISTSTYLE KEY DISTKEY("id_file") SORTKEY("batch_time")',
    ]


@pytest.mark.parametrize(
    "sortkey, expected_sortkey",
    [
        ('"batch_time"', 'SORTKEY("batch_time")'),
        ("batch_time", 'SORTKEY("batch_time")'),
        ('("batch_time")', 'SORTKEY("batch_time")'),
        ("(batch_time)", 'SORTKEY("batch_time")'),
        ('("batch_time", "id_file")', 'SORTKEY("batch_time", "id_file")'),
        ('["batch_time", "id_file"]', 'SORTKEY("batch_time", "id_file")'),
    ],
)
def test_create_table_physical_properties_from_model_definition(
    make_mocked_engine_adapter: t.Callable, sortkey: str, expected_sortkey: str
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    model: SqlModel = t.cast(
        SqlModel,
        load_sql_based_model(
            d.parse(
                f"""
MODEL (
    name test_schema.test_table,
    kind full,
    physical_properties (
        diststyle = key,
        distkey = "id_file",
        sortkey = {sortkey}
    )
);
SELECT id_file::INT, batch_time::TIMESTAMP;
    """
            )
        ),
    )

    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    assert to_sql_calls(adapter) == [
        f'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER, "batch_time" TIMESTAMP) DISTSTYLE KEY DISTKEY("id_file") {expected_sortkey}',
    ]


@pytest.mark.parametrize(
    "distkey",
    ['"id_file"', "id_file", "'id_file'", '("id_file")', "(id_file)"],
)
def test_create_table_physical_properties_distkey_from_model_definition(
    make_mocked_engine_adapter: t.Callable, distkey: str
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    model: SqlModel = t.cast(
        SqlModel,
        load_sql_based_model(
            d.parse(
                f"""
MODEL (
    name test_schema.test_table,
    kind full,
    physical_properties (
        diststyle = key,
        distkey = {distkey}
    )
);
SELECT id_file::INT;
    """
            )
        ),
    )

    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_schema"."test_table" ("id_file" INTEGER) DISTSTYLE KEY DISTKEY("id_file")',
    ]


def test_varchar_size_workaround(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    columns = {
        "char": exp.DataType.build("CHAR", dialect=adapter.dialect),
        "char1": exp.DataType.build("CHAR(1)", dialect=adapter.dialect),
        "char2": exp.DataType.build("CHAR(2)", dialect=adapter.dialect),
        "varchar": exp.DataType.build("VARCHAR", dialect=adapter.dialect),
        "varchar256": exp.DataType.build("VARCHAR(256)", dialect=adapter.dialect),
        "varchar2": exp.DataType.build("VARCHAR(2)", dialect=adapter.dialect),
    }

    assert adapter._default_precision_to_max(columns) == {
        "char": exp.DataType.build("CHAR", dialect=adapter.dialect),
        "char1": exp.DataType.build("CHAR(max)", dialect=adapter.dialect),
        "char2": exp.DataType.build("CHAR(2)", dialect=adapter.dialect),
        "varchar": exp.DataType.build("VARCHAR", dialect=adapter.dialect),
        "varchar256": exp.DataType.build("VARCHAR(max)", dialect=adapter.dialect),
        "varchar2": exp.DataType.build("VARCHAR(2)", dialect=adapter.dialect),
    }

    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="test_random_id",
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=True,
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.columns",
        return_value=columns,
    )

    adapter.ctas(
        table_name="test_schema.test_table",
        query_or_df=parse_one(
            "SELECT char, char1 + 1 AS char1, char2 AS char2, varchar, varchar256, varchar2 FROM (SELECT * FROM table WHERE FALSE LIMIT 0) WHERE d > 0 AND FALSE LIMIT 0"
        ),
        exists=False,
    )

    assert to_sql_calls(adapter) == [
        'CREATE VIEW "__temp_ctas_test_random_id" AS SELECT "char", "char1" + 1 AS "char1", "char2" AS "char2", "varchar", "varchar256", "varchar2" FROM (SELECT * FROM "table") WITH NO SCHEMA BINDING',
        'DROP VIEW IF EXISTS "__temp_ctas_test_random_id" CASCADE',
        'CREATE TABLE "test_schema"."test_table" ("char" CHAR, "char1" CHAR(max), "char2" CHAR(2), "varchar" VARCHAR, "varchar256" VARCHAR(max), "varchar2" VARCHAR(2))',
    ]


def test_sync_grants_config(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    relation = exp.to_table("test_schema.test_table", dialect="redshift")
    new_grants_config = {"SELECT": ["user1", "user2"], "INSERT": ["user3"]}

    current_grants = [("SELECT", "old_user"), ("UPDATE", "legacy_user")]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="redshift")
    expected_sql = (
        "SELECT privilege_type, grantee FROM information_schema.table_privileges "
        "WHERE table_schema = 'test_schema' AND table_name = 'test_table' "
        "AND grantor = CURRENT_USER AND grantee <> CURRENT_USER"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 4
    assert 'REVOKE SELECT ON "test_schema"."test_table" FROM "old_user"' in sql_calls
    assert 'REVOKE UPDATE ON "test_schema"."test_table" FROM "legacy_user"' in sql_calls
    assert 'GRANT SELECT ON "test_schema"."test_table" TO "user1", "user2"' in sql_calls
    assert 'GRANT INSERT ON "test_schema"."test_table" TO "user3"' in sql_calls


def test_sync_grants_config_with_overlaps(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    relation = exp.to_table("test_schema.test_table", dialect="redshift")
    new_grants_config = {
        "SELECT": ["user_shared", "user_new"],
        "INSERT": ["user_shared", "user_writer"],
    }

    current_grants = [
        ("SELECT", "user_shared"),
        ("SELECT", "user_legacy"),
        ("INSERT", "user_shared"),
    ]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="redshift")
    expected_sql = (
        "SELECT privilege_type, grantee FROM information_schema.table_privileges "
        "WHERE table_schema = 'test_schema' AND table_name = 'test_table' "
        "AND grantor = CURRENT_USER AND grantee <> CURRENT_USER"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 3
    assert 'REVOKE SELECT ON "test_schema"."test_table" FROM "user_legacy"' in sql_calls
    assert 'GRANT SELECT ON "test_schema"."test_table" TO "user_new"' in sql_calls
    assert 'GRANT INSERT ON "test_schema"."test_table" TO "user_writer"' in sql_calls


@pytest.mark.parametrize(
    "table_type",
    [
        (DataObjectType.TABLE),
        (DataObjectType.VIEW),
        (DataObjectType.MATERIALIZED_VIEW),
    ],
)
def test_sync_grants_config_object_kind(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    table_type: DataObjectType,
) -> None:
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    relation = exp.to_table("test_schema.test_object", dialect="redshift")

    mocker.patch.object(adapter, "fetchall", return_value=[])

    adapter.sync_grants_config(relation, {"SELECT": ["user_test"]}, table_type)

    sql_calls = to_sql_calls(adapter)
    # we don't need to explicitly specify object_type for tables and views
    assert sql_calls == [f'GRANT SELECT ON "test_schema"."test_object" TO "user_test"']


def test_sync_grants_config_quotes(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    relation = exp.to_table('"TestSchema"."TestTable"', dialect="redshift")
    new_grants_config = {"SELECT": ["user1", "user2"], "INSERT": ["user3"]}

    current_grants = [("SELECT", "user_old"), ("UPDATE", "user_legacy")]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="redshift")
    expected_sql = (
        "SELECT privilege_type, grantee FROM information_schema.table_privileges "
        "WHERE table_schema = 'TestSchema' AND table_name = 'TestTable' "
        "AND grantor = CURRENT_USER AND grantee <> CURRENT_USER"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 4
    assert 'REVOKE SELECT ON "TestSchema"."TestTable" FROM "user_old"' in sql_calls
    assert 'REVOKE UPDATE ON "TestSchema"."TestTable" FROM "user_legacy"' in sql_calls
    assert 'GRANT SELECT ON "TestSchema"."TestTable" TO "user1", "user2"' in sql_calls
    assert 'GRANT INSERT ON "TestSchema"."TestTable" TO "user3"' in sql_calls


def test_sync_grants_config_no_schema(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    relation = exp.to_table("test_table", dialect="redshift")
    new_grants_config = {"SELECT": ["user1"], "INSERT": ["user2"]}

    current_grants = [("UPDATE", "user_old")]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)
    get_schema_mock = mocker.patch.object(adapter, "_get_current_schema", return_value="public")

    adapter.sync_grants_config(relation, new_grants_config)

    get_schema_mock.assert_called_once()

    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="redshift")
    expected_sql = (
        "SELECT privilege_type, grantee FROM information_schema.table_privileges "
        "WHERE table_schema = 'public' AND table_name = 'test_table' "
        "AND grantor = CURRENT_USER AND grantee <> CURRENT_USER"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 3
    assert 'REVOKE UPDATE ON "test_table" FROM "user_old"' in sql_calls
    assert 'GRANT SELECT ON "test_table" TO "user1"' in sql_calls
    assert 'GRANT INSERT ON "test_table" TO "user2"' in sql_calls


def test_create_table_from_query_exists_no_if_not_exists(
    adapter: t.Callable, mocker: MockerFixture
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="test_random_id",
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=True,
    )

    columns_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.columns",
        return_value={
            "a": exp.DataType.build("VARCHAR(MAX)", dialect="redshift"),
            "b": exp.DataType.build("VARCHAR(60)", dialect="redshift"),
            "c": exp.DataType.build("VARCHAR(MAX)", dialect="redshift"),
            "d": exp.DataType.build("VARCHAR(MAX)", dialect="redshift"),
            "e": exp.DataType.build("TIMESTAMP", dialect="redshift"),
        },
    )

    adapter.ctas(
        table_name="test_schema.test_table",
        query_or_df=parse_one(
            "SELECT a, b, x + 1 AS c, d AS d, e FROM (SELECT * FROM table WHERE FALSE LIMIT 0) WHERE d > 0 AND FALSE LIMIT 0"
        ),
        exists=False,
    )

    assert to_sql_calls(adapter) == [
        'CREATE VIEW "__temp_ctas_test_random_id" AS SELECT "a", "b", "x" + 1 AS "c", "d" AS "d", "e" FROM (SELECT * FROM "table") WITH NO SCHEMA BINDING',
        'DROP VIEW IF EXISTS "__temp_ctas_test_random_id" CASCADE',
        'CREATE TABLE "test_schema"."test_table" ("a" VARCHAR(MAX), "b" VARCHAR(60), "c" VARCHAR(MAX), "d" VARCHAR(MAX), "e" TIMESTAMP)',
    ]

    columns_mock.assert_called_once_with(exp.table_("__temp_ctas_test_random_id", quoted=True))


def test_create_table_recursive_cte(adapter: t.Callable, mocker: MockerFixture):
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="test_random_id",
    )

    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=True,
    )

    columns_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.columns",
        return_value={
            "a": exp.DataType.build("VARCHAR(MAX)", dialect="redshift"),
            "b": exp.DataType.build("VARCHAR(60)", dialect="redshift"),
            "c": exp.DataType.build("VARCHAR(MAX)", dialect="redshift"),
            "d": exp.DataType.build("VARCHAR(MAX)", dialect="redshift"),
            "e": exp.DataType.build("TIMESTAMP", dialect="redshift"),
        },
    )

    adapter.ctas(
        table_name="test_schema.test_table",
        query_or_df=parse_one(
            "WITH RECURSIVE cte AS (SELECT * FROM table WHERE FALSE LIMIT 0) SELECT a, b, x + 1 AS c, d AS d, e FROM cte WHERE d > 0 AND FALSE LIMIT 0",
            dialect="redshift",
        ),
        exists=False,
    )

    assert to_sql_calls(adapter) == [
        'CREATE VIEW "__temp_ctas_test_random_id" AS WITH RECURSIVE "cte" AS (SELECT * FROM "table") SELECT "a", "b", "x" + 1 AS "c", "d" AS "d", "e" FROM "cte"',
        'DROP VIEW IF EXISTS "__temp_ctas_test_random_id" CASCADE',
        'CREATE TABLE "test_schema"."test_table" ("a" VARCHAR(MAX), "b" VARCHAR(60), "c" VARCHAR(MAX), "d" VARCHAR(MAX), "e" TIMESTAMP)',
    ]

    columns_mock.assert_called_once_with(exp.table_("__temp_ctas_test_random_id", quoted=True))


def test_create_table_from_query_exists_and_if_not_exists(
    adapter: t.Callable, mocker: MockerFixture
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=True,
    )
    adapter.ctas(
        table_name="test_table",
        query_or_df=parse_one("SELECT cola FROM table"),
        exists=True,
    )

    adapter.cursor.execute.assert_not_called()


def test_create_table_from_query_not_exists_if_not_exists(
    adapter: t.Callable, mocker: MockerFixture
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=False,
    )
    adapter.ctas(
        table_name="test_table",
        query_or_df=parse_one("SELECT cola FROM table"),
        exists=True,
    )

    adapter.cursor.execute.assert_called_with(
        'CREATE TABLE "test_table" AS SELECT "cola" FROM "table"'
    )


def test_create_table_from_query_not_exists_no_if_not_exists(
    adapter: t.Callable, mocker: MockerFixture
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=False,
    )
    adapter.ctas(
        table_name="test_table",
        query_or_df=parse_one("SELECT cola FROM table"),
        exists=False,
    )

    adapter.cursor.execute.assert_called_with(
        'CREATE TABLE "test_table" AS SELECT "cola" FROM "table"'
    )


def test_values_to_sql(adapter: t.Callable, mocker: MockerFixture):
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    result = adapter._values_to_sql(
        values=list(df.itertuples(index=False, name=None)),
        target_columns_to_types={"a": exp.DataType.build("int"), "b": exp.DataType.build("int")},
        batch_start=0,
        batch_end=2,
    )
    # 3,6 is missing since the batch range excluded it
    assert (
        result.sql(dialect="redshift")
        == "SELECT CAST(a AS INTEGER) AS a, CAST(b AS INTEGER) AS b FROM (SELECT 1 AS a, 4 AS b UNION ALL SELECT 2, 5) AS t"
    )


def test_replace_query_with_query(adapter: t.Callable, mocker: MockerFixture):
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=False,
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.columns",
        return_value={"cola": exp.DataType(this=exp.DataType.Type.INT)},
    )

    adapter.replace_query(table_name="test_table", query_or_df=parse_one("SELECT cola FROM table"))

    assert to_sql_calls(adapter) == [
        'CREATE TABLE "test_table" AS SELECT "cola" FROM "table"',
    ]


def test_replace_query_with_df_table_exists(adapter: t.Callable, mocker: MockerFixture):
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=True,
    )
    call_counter = 0

    def mock_table(*args, **kwargs):
        nonlocal call_counter
        call_counter += 1
        return f"temp_table_{call_counter}"

    mock_temp_table = mocker.MagicMock(side_effect=mock_table)
    mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table", mock_temp_table)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test_table", type="table")],
    )

    adapter.replace_query(
        table_name="test_table",
        query_or_df=df,
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "b": exp.DataType.build("int"),
        },
    )

    adapter.cursor.begin.assert_called_once()
    adapter.cursor.commit.assert_called_once()

    assert to_sql_calls(adapter) == [
        'CREATE TABLE "temp_table_1" ("a" INTEGER, "b" INTEGER)',
        'INSERT INTO "temp_table_1" ("a", "b") SELECT CAST("a" AS INTEGER) AS "a", CAST("b" AS INTEGER) AS "b" FROM (SELECT 1 AS "a", 4 AS "b" UNION ALL SELECT 2, 5 UNION ALL SELECT 3, 6) AS "t"',
        'ALTER TABLE "test_table" RENAME TO "temp_table_2"',
        'ALTER TABLE "temp_table_1" RENAME TO "test_table"',
        'DROP TABLE IF EXISTS "temp_table_2"',
    ]


def test_replace_query_with_df_table_not_exists(adapter: t.Callable, mocker: MockerFixture):
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=False,
    )
    adapter.replace_query(
        table_name="test_table",
        query_or_df=df,
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "b": exp.DataType.build("int"),
        },
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE "test_table" AS SELECT CAST("a" AS INTEGER) AS "a", CAST("b" AS INTEGER) AS "b" FROM (SELECT 1 AS "a", 4 AS "b" UNION ALL SELECT 2, 5 UNION ALL SELECT 3, 6) AS "t"',
    ]


def test_table_exists_db_table(mocker: MockerFixture):
    connection_mock = mocker.NonCallableMock()
    cursor_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock
    connection_mock.cursor.return_value.fetchone.return_value = (1,)

    adapter = RedshiftEngineAdapter(lambda: connection_mock)
    assert adapter.table_exists(table_name=exp.to_table("some_db.some_table"))

    cursor_mock.execute.assert_called_once_with(
        """SELECT 1 FROM "information_schema"."tables" WHERE "table_name" = 'some_table' AND "table_schema" = 'some_db'"""
    )


def test_table_exists_table_only(mocker: MockerFixture):
    connection_mock = mocker.NonCallableMock()
    cursor_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock
    connection_mock.cursor.return_value.fetchone.return_value = None

    adapter = RedshiftEngineAdapter(lambda: connection_mock)
    assert not adapter.table_exists(table_name=exp.to_table("some_table"))

    cursor_mock.execute.assert_called_once_with(
        """SELECT 1 FROM "information_schema"."tables" WHERE "table_name" = 'some_table'"""
    )


def test_create_view(adapter: t.Callable):
    adapter.create_view(
        view_name="test_view",
        query_or_df=parse_one("SELECT cola FROM table"),
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "b": exp.DataType.build("int"),
        },
    )

    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "test_view" CASCADE',
        'CREATE VIEW "test_view" ("a", "b") AS SELECT "cola" FROM "table" WITH NO SCHEMA BINDING',
    ]


def test_alter_table_drop_column_cascade(adapter: t.Callable):
    current_table_name = "test_table"
    target_table_name = "target_table"

    def table_columns(table_name: str) -> t.Dict[str, exp.DataType]:
        if table_name == current_table_name:
            return {"id": exp.DataType.build("int"), "test_column": exp.DataType.build("int")}
        return {"id": exp.DataType.build("int")}

    adapter.columns = table_columns

    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))
    assert to_sql_calls(adapter) == [
        'ALTER TABLE "test_table" DROP COLUMN "test_column" CASCADE',
    ]


def test_alter_table_precision_increase_varchar(adapter: t.Callable):
    current_table_name = "test_table"
    target_table_name = "target_table"

    def table_columns(table_name: str) -> t.Dict[str, exp.DataType]:
        if table_name == current_table_name:
            return {
                "id": exp.DataType.build("int"),
                "test_column": exp.DataType.build("VARCHAR(10)"),
            }
        return {
            "id": exp.DataType.build("int"),
            "test_column": exp.DataType.build("VARCHAR(20)"),
        }

    adapter.columns = table_columns

    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))
    assert to_sql_calls(adapter) == [
        'ALTER TABLE "test_table" ALTER COLUMN "test_column" TYPE VARCHAR(20)',
    ]


def test_alter_table_precision_increase_decimal(adapter: t.Callable):
    current_table_name = "test_table"
    target_table_name = "target_table"

    def table_columns(table_name: str) -> t.Dict[str, exp.DataType]:
        if table_name == current_table_name:
            return {
                "id": exp.DataType.build("int"),
                "test_column": exp.DataType.build("DECIMAL(10, 10)"),
            }
        return {
            "id": exp.DataType.build("int"),
            "test_column": exp.DataType.build("DECIMAL(25, 10)"),
        }

    adapter.columns = table_columns

    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))
    assert to_sql_calls(adapter) == [
        'ALTER TABLE "test_table" DROP COLUMN "test_column" CASCADE',
        'ALTER TABLE "test_table" ADD COLUMN "test_column" DECIMAL(25, 10)',
    ]


def test_merge(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.enable_merge",
        new_callable=PropertyMock(return_value=True),
    )

    adapter.merge(
        target_table=exp.to_table("target_table_name"),
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
    )

    # Test additional predicates in the merge_filter
    adapter.merge(
        target_table=exp.to_table("target_table_name"),
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
        merge_filter=exp.and_(
            exp.and_(exp.column("ID", "__MERGE_SOURCE__") > 0),
            exp.column("ts", "__MERGE_TARGET__") < exp.column("ts", "__MERGE_SOURCE__"),
        ),
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'MERGE INTO "target_table_name" USING (SELECT "ID", "ts", "val" FROM "source") AS "__MERGE_SOURCE__" ON "target_table_name"."ID" = "__MERGE_SOURCE__"."ID" WHEN MATCHED THEN UPDATE SET "ID" = "__MERGE_SOURCE__"."ID", "ts" = "__MERGE_SOURCE__"."ts", "val" = "__MERGE_SOURCE__"."val" WHEN NOT MATCHED THEN INSERT ("ID", "ts", "val") VALUES ("__MERGE_SOURCE__"."ID", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")',
        'MERGE INTO "target_table_name" USING (SELECT "ID", "ts", "val" FROM "source") AS "__MERGE_SOURCE__" ON ("__MERGE_SOURCE__"."ID" > 0 AND "target_table_name"."ts" < "__MERGE_SOURCE__"."ts") AND "target_table_name"."ID" = "__MERGE_SOURCE__"."ID" WHEN MATCHED THEN UPDATE SET "ID" = "__MERGE_SOURCE__"."ID", "ts" = "__MERGE_SOURCE__"."ts", "val" = "__MERGE_SOURCE__"."val" WHEN NOT MATCHED THEN INSERT ("ID", "ts", "val") VALUES ("__MERGE_SOURCE__"."ID", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")',
    ]


def test_merge_when_matched_error(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.enable_merge",
        new_callable=PropertyMock(return_value=True),
    )

    with pytest.raises(
        SQLMeshError,
        match=r".*Redshift only supports a single WHEN MATCHED and WHEN NOT MATCHED clause*",
    ):
        adapter.merge(
            target_table=exp.to_table("target_table_name"),
            source_table=t.cast(exp.Select, parse_one('SELECT "ID", val FROM source')),
            target_columns_to_types={
                "ID": exp.DataType.build("int"),
                "val": exp.DataType.build("int"),
            },
            unique_key=[exp.to_identifier("ID", quoted=True)],
            when_matched=exp.Whens(
                expressions=[
                    exp.When(
                        matched=True,
                        condition=exp.column("ID", "__MERGE_SOURCE__").eq(exp.Literal.number(1)),
                        then=exp.Update(
                            expressions=[
                                exp.column("val", "__MERGE_TARGET__").eq(
                                    exp.column("val", "__MERGE_SOURCE__")
                                ),
                            ],
                        ),
                    ),
                    exp.When(
                        matched=True,
                        source=False,
                        then=exp.Update(
                            expressions=[
                                exp.column("val", "__MERGE_TARGET__").eq(
                                    exp.column("val", "__MERGE_SOURCE__")
                                ),
                            ],
                        ),
                    ),
                ]
            ),
        )


def test_merge_logical_filter_error(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.enable_merge",
        new_callable=PropertyMock(return_value=False),
    )

    with pytest.raises(
        SQLMeshError,
        match=r".*This engine does not support MERGE expressions and therefore `merge_filter` is not supported.*",
    ):
        adapter.merge(
            target_table=exp.to_table("target_table_name_2"),
            source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts FROM source')),
            target_columns_to_types={
                "ID": exp.DataType.build("int"),
                "ts": exp.DataType.build("timestamp"),
            },
            unique_key=[exp.to_identifier("ID", quoted=True)],
            merge_filter=exp.and_(
                exp.and_(exp.column("ID", "__MERGE_SOURCE__") > 0),
                exp.column("ts", "__MERGE_TARGET__") < exp.column("ts", "__MERGE_SOURCE__"),
            ),
        )


def test_merge_logical(
    make_mocked_engine_adapter: t.Callable, make_temp_table_name: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "test"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    adapter.merge(
        target_table=exp.to_table("target"),
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TABLE "__temp_test_abcdefgh" AS SELECT CAST("ID" AS INTEGER) AS "ID", CAST("ts" AS TIMESTAMP) AS "ts" FROM (SELECT "ID", "ts" FROM "source") AS "_subquery"',
        'DELETE FROM "target" WHERE "ID" IN (SELECT "ID" FROM "__temp_test_abcdefgh")',
        'INSERT INTO "target" ("ID", "ts") SELECT "ID", "ts" FROM (SELECT "ID" AS "ID", "ts" AS "ts", ROW_NUMBER() OVER (PARTITION BY "ID" ORDER BY "ID") AS _row_number FROM "__temp_test_abcdefgh") AS _t WHERE _row_number = 1',
        'DROP TABLE IF EXISTS "__temp_test_abcdefgh"',
    ]


def test_redshift_no_clustering_operations_on_column_drop(
    make_mocked_engine_adapter: t.Callable,
) -> None:
    """Dropping a column on Redshift never emits clustering statements."""
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    adapter.cursor.fetchall.return_value = []

    current_columns = {"TENANT_ID": "INT", "PARTITION_MONTH": "INT"}
    target_columns = {"TENANT_ID": "INT"}
    adapter.columns = lambda table_name, **kwargs: {
        k: exp.DataType.build(v)
        for k, v in (current_columns if "current" in str(table_name) else target_columns).items()
    }

    operations = adapter.get_alter_operations("current_table", "target_table")

    assert len(operations) == 1
    assert all(isinstance(op, TableAlterDropColumnOperation) for op in operations)


def test_redshift_multiple_column_drops_no_clustering_operations(
    make_mocked_engine_adapter: t.Callable,
) -> None:
    """Dropping multiple columns on Redshift preserves the schema-diff order.

    No clustering operations must be injected between or after the column drops.
    """
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)
    adapter.cursor.fetchall.return_value = []

    current_columns = {"EXTRA": "INT", "PARTITION_MONTH": "INT", "TENANT_ID": "INT"}
    target_columns = {"TENANT_ID": "INT"}
    adapter.columns = lambda table_name, **kwargs: {
        k: exp.DataType.build(v)
        for k, v in (current_columns if "current" in str(table_name) else target_columns).items()
    }

    operations = adapter.get_alter_operations("current_table", "target_table")

    assert len(operations) == 2
    assert all(isinstance(op, TableAlterDropColumnOperation) for op in operations)


@pytest.mark.parametrize(
    "mocked_columns",
    [
        {},
        {
            "id": exp.DataType.build("UNKNOWN"),
            "valid_from": exp.DataType.build("TIMESTAMP"),
        },
    ],
    ids=["empty_columns", "unknown_column_type"],
)
def test_ctas_varchar_workaround_empty_or_unknown_columns_fallback(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    mocked_columns: t.Dict[str, exp.DataType],
) -> None:
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="test_random_id",
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.table_exists",
        return_value=False,
    )
    mocker.patch(
        "sqlmesh.core.engine_adapter.redshift.RedshiftEngineAdapter.columns",
        return_value=mocked_columns,
    )

    adapter.ctas(
        table_name="sqlmesh_xxx.my_example",
        query_or_df=parse_one(
            "SELECT id, updated_at, trashed_at, CAST(NULL AS TIMESTAMP) AS valid_from, CAST(NULL AS TIMESTAMP) AS valid_to FROM my_table WHERE FALSE LIMIT 0"
        ),
        exists=False,
    )

    sql_calls = to_sql_calls(adapter)
    assert 'CREATE TABLE "sqlmesh_xxx"."my_example"' not in sql_calls
    assert all("UNKNOWN" not in sql for sql in sql_calls)
    assert sql_calls == [
        'CREATE VIEW "__temp_ctas_test_random_id" AS SELECT "id", "updated_at", "trashed_at", CAST(NULL AS TIMESTAMP) AS "valid_from", CAST(NULL AS TIMESTAMP) AS "valid_to" FROM "my_table" WITH NO SCHEMA BINDING',
        'DROP VIEW IF EXISTS "__temp_ctas_test_random_id" CASCADE',
        'CREATE TABLE "sqlmesh_xxx"."my_example" AS SELECT "id", "updated_at", "trashed_at", CAST(NULL AS TIMESTAMP) AS "valid_from", CAST(NULL AS TIMESTAMP) AS "valid_to" FROM "my_table" WHERE FALSE LIMIT 0',
    ]
    for sql in sql_calls:
        parse_one(sql, read="redshift")


def test_columns_late_binding_view_and_pg_catalog_fallback(
    make_mocked_engine_adapter: t.Callable,
) -> None:
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    # Subcase 2a: svv_columns returns [] -> fallback to pg_get_late_binding_view_cols() (without schema)
    adapter.cursor.fetchall.side_effect = [
        [],
        [
            ("id", "character varying(36)"),
            ("name", "character varying(256)"),
            ("updated_at", "timestamp without time zone"),
            ("deleted_at", "timestamp with time zone"),
        ],
    ]
    resp = adapter.columns("__temp_ctas_abc")
    assert to_sql_calls(adapter) == [
        """SELECT "column_name", "data_type", "character_maximum_length", "numeric_precision", "numeric_scale" FROM "svv_columns" WHERE "table_name" = '__temp_ctas_abc'""",
        """SELECT "col_name", "col_type" FROM PG_GET_LATE_BINDING_VIEW_COLS() AS "cols"("view_schema" NAME, "view_name" NAME, "col_name" NAME, "col_type" VARCHAR, "col_num" INTEGER) WHERE "view_name" = '__temp_ctas_abc' ORDER BY "col_num\"""",
    ]
    assert list(resp.keys()) == ["id", "name", "updated_at", "deleted_at"]
    assert resp == {
        "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
        "name": exp.DataType.build("VARCHAR(256)", dialect="redshift"),
        "updated_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
        "deleted_at": exp.DataType.build("TIMESTAMPTZ", dialect="redshift"),
    }
    assert adapter._default_precision_to_max(resp) == {
        "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
        "name": exp.DataType.build("VARCHAR(max)", dialect="redshift"),
        "updated_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
        "deleted_at": exp.DataType.build("TIMESTAMPTZ", dialect="redshift"),
    }

    # Subcase 2a (with schema): includes AND "view_schema" = 'my_schema'
    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.side_effect = [
        [],
        [("id", "character varying(36)")],
    ]
    resp_schema = adapter.columns("my_schema.my_lbv")
    assert to_sql_calls(adapter) == [
        """SELECT "column_name", "data_type", "character_maximum_length", "numeric_precision", "numeric_scale" FROM "svv_columns" WHERE "table_name" = 'my_lbv' AND "table_schema" = 'my_schema'""",
        """SELECT "col_name", "col_type" FROM PG_GET_LATE_BINDING_VIEW_COLS() AS "cols"("view_schema" NAME, "view_name" NAME, "col_name" NAME, "col_type" VARCHAR, "col_num" INTEGER) WHERE "view_name" = 'my_lbv' AND "view_schema" = 'my_schema' ORDER BY "col_num\"""",
    ]
    assert resp_schema == {"id": exp.DataType.build("VARCHAR(36)", dialect="redshift")}

    # Subcase 2b: svv_columns and pg_get_late_binding_view_cols() both return [] -> super().columns() pg_catalog.pg_attribute fallback
    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.side_effect = [
        [],
        [],
        [("id", "integer"), ("ts", "timestamp without time zone")],
    ]
    resp_pg = adapter.columns("db.local_tbl")
    sql_calls_pg = to_sql_calls(adapter)
    assert len(sql_calls_pg) == 3
    assert '"pg_catalog"."pg_attribute"' in sql_calls_pg[2]
    assert resp_pg == {
        "id": exp.DataType.build("INT", dialect="redshift"),
        "ts": exp.DataType.build("TIMESTAMP", dialect="redshift"),
    }

    # Subcase 2c: all three catalog queries return [] -> catches SQLMeshError and returns {}
    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.side_effect = [[], [], []]
    assert adapter.columns("db.missing") == {}


def test_create_view_explicit_no_schema_binding_override(
    make_mocked_engine_adapter: t.Callable,
) -> None:
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter)

    adapter.create_view(
        view_name="test_view",
        query_or_df=parse_one("SELECT cola FROM tbl"),
        no_schema_binding=False,
    )
    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "test_view" CASCADE',
        'CREATE VIEW "test_view" AS SELECT "cola" FROM "tbl"',
    ]

    adapter.cursor.execute.reset_mock()
    adapter.create_view(
        view_name="test_view",
        query_or_df=parse_one("SELECT cola FROM tbl"),
    )
    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "test_view" CASCADE',
        'CREATE VIEW "test_view" AS SELECT "cola" FROM "tbl" WITH NO SCHEMA BINDING',
    ]

    adapter.cursor.execute.reset_mock()
    adapter.create_view(
        view_name="test_view",
        query_or_df=parse_one(
            "WITH RECURSIVE cte AS (SELECT 1 AS cola) SELECT cola FROM cte",
            dialect="redshift",
        ),
    )
    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "test_view" CASCADE',
        'CREATE VIEW "test_view" AS WITH RECURSIVE "cte" AS (SELECT 1 AS "cola") SELECT "cola" FROM "cte"',
    ]


def test_scd_type_2_by_time_unannotated_lifecycle_issue_5787(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
) -> None:
    model = load_sql_based_model(
        d.parse(
            """
            MODEL (
              name sqlmesh_xxx.my_example,
              kind SCD_TYPE_2_BY_TIME (
                unique_key id
              )
            );
            SELECT
              id,
              updated_at,
              trashed_at
            FROM my_table
            """
        ),
        dialect="redshift",
    )
    assert not model.annotated

    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter, default_catalog="dev")
    adapter.cursor.fetchone.side_effect = None
    adapter.cursor.fetchone.return_value = None
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="test_random_id",
    )

    strategy = SCDType2Strategy(adapter)
    target_table = "sqlmesh__sqlmesh_xxx.sqlmesh_xxx__my_example__1234567890"
    truncated_ddl = 'CREATE TABLE "sqlmesh__sqlmesh_xxx"."sqlmesh_xxx__my_example__1234567890"'

    # Step 1a: strategy.create when svv_columns returns [] and pg_get_late_binding_view_cols() resolves columns
    adapter.cursor.fetchall.side_effect = [
        [],
        [
            ("id", "character varying(36)"),
            ("updated_at", "timestamp without time zone"),
            ("trashed_at", "timestamp without time zone"),
            ("valid_from", "timestamp without time zone"),
            ("valid_to", "timestamp without time zone"),
        ],
    ]
    strategy.create(
        table_name=target_table,
        model=model,
        is_table_deployable=True,
        render_kwargs={},
        skip_grants=True,
    )
    create_calls_lbv = to_sql_calls(adapter)
    assert truncated_ddl not in create_calls_lbv
    assert (
        'CREATE TABLE "sqlmesh__sqlmesh_xxx"."sqlmesh_xxx__my_example__1234567890" ("id" VARCHAR(36), "updated_at" TIMESTAMP, "trashed_at" TIMESTAMP, "valid_from" TIMESTAMP, "valid_to" TIMESTAMP)'
        in create_calls_lbv
    )
    for sql in create_calls_lbv:
        parse_one(sql, read="redshift")

    # Step 1b: strategy.create when columns("__temp_ctas_*") returns {} completely (all catalog queries empty)
    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.side_effect = [[], [], []]
    strategy.create(
        table_name=target_table,
        model=model,
        is_table_deployable=True,
        render_kwargs={},
        skip_grants=True,
    )
    create_calls_fallback = to_sql_calls(adapter)
    assert truncated_ddl not in create_calls_fallback
    assert (
        'CREATE TABLE "sqlmesh__sqlmesh_xxx"."sqlmesh_xxx__my_example__1234567890" AS SELECT "id" AS "id", "updated_at" AS "updated_at", "trashed_at" AS "trashed_at", CAST(NULL AS TIMESTAMP) AS "valid_from", CAST(NULL AS TIMESTAMP) AS "valid_to" FROM "my_table" AS "my_table" WHERE FALSE LIMIT 0'
        in create_calls_fallback
    )
    for sql in create_calls_fallback:
        parse_one(sql, read="redshift")

    # Step 2 (maiden load: is_first_insert=True) & Step 3 (incremental load: is_first_insert=False)
    adapter.cursor.fetchone.return_value = ("dev",)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[
            DataObject(
                catalog="dev",
                schema="sqlmesh__sqlmesh_xxx",
                name="sqlmesh_xxx__my_example__1234567890",
                type=DataObjectType.TABLE,
            )
        ],
    )
    mocker.patch.object(
        adapter,
        "columns",
        return_value={
            "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
            "updated_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            "trashed_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
        },
    )

    for is_first_insert in (True, False):
        adapter.cursor.execute.reset_mock()
        strategy.insert(
            table_name=target_table,
            query_or_df=model.render_query_or_raise(),
            model=model,
            is_first_insert=is_first_insert,
            render_kwargs={},
            execution_time="2024-01-02 00:00:00",
        )
        insert_calls = to_sql_calls(adapter)
        for sql in insert_calls:
            parse_one(sql, read="redshift")
            assert "DISTINCT ON" not in sql
            assert "TRUNCATE" not in sql

        assert any(
            sql.startswith(
                'CREATE TABLE "dev"."sqlmesh__sqlmesh_xxx"."__temp_sqlmesh_xxx__my_example__1234567890_test_random_id" AS SELECT '
            )
            for sql in insert_calls
        )
        assert (
            'DELETE FROM "sqlmesh__sqlmesh_xxx"."sqlmesh_xxx__my_example__1234567890" WHERE TRUE'
            in insert_calls
        )
        assert (
            'DROP TABLE IF EXISTS "sqlmesh__sqlmesh_xxx"."__temp_sqlmesh_xxx__my_example__1234567890_test_random_id"'
            in insert_calls
        )

        scd_insert_sql = [
            sql
            for sql in insert_calls
            if sql.startswith(
                'INSERT INTO "sqlmesh__sqlmesh_xxx"."sqlmesh_xxx__my_example__1234567890" ("id", "updated_at", "trashed_at", "valid_from", "valid_to") WITH "source" AS ('
            )
        ]
        assert len(scd_insert_sql) == 1
        assert (
            'ROW_NUMBER() OVER (PARTITION BY "id" ORDER BY "id") AS _row_number'
            in scd_insert_sql[0]
        )
        if is_first_insert:
            assert 'WHERE NOT "valid_to" IS NULL LIMIT 0' in scd_insert_sql[0]
            assert 'WHERE "valid_to" IS NULL LIMIT 0' in scd_insert_sql[0]
        else:
            assert "LIMIT 0" not in scd_insert_sql[0]


@pytest.mark.parametrize(
    "scenario",
    [
        "reserved_from_by_time",
        "reserved_select_by_time",
        "hyphenated_col_by_time",
        "reserved_from_by_column",
        "reserved_select_by_column",
        "hyphenated_col_by_column",
        "composite_key_hard_deletes_timestamptz",
        "expression_key_no_hard_deletes",
        "dataframe_source_by_time",
        "by_column_strategy_star_and_explicit",
    ],
)
def test_scd_type_2_redshift_edge_cases(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    scenario: str,
) -> None:
    adapter = make_mocked_engine_adapter(RedshiftEngineAdapter, default_catalog="dev")
    adapter.cursor.fetchone.return_value = ("dev",)
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.random_id",
        return_value="test_random_id",
    )
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[
            DataObject(
                catalog="dev",
                schema="s",
                name="t",
                type=DataObjectType.TABLE,
            )
        ],
    )

    if scenario in (
        "reserved_from_by_time",
        "reserved_select_by_time",
        "hyphenated_col_by_time",
    ):
        special_col = {
            "reserved_from_by_time": "from",
            "reserved_select_by_time": "select",
            "hyphenated_col_by_time": "col-name",
        }[scenario]
        adapter.scd_type_2_by_time(
            target_table="s.t",
            source_table=parse_one(
                f'SELECT id, "{special_col}", updated_at FROM src',
                read="redshift",
            ),
            unique_key=[exp.column("id")],
            valid_from_col=exp.column("valid_from", quoted=True),
            valid_to_col=exp.column("valid_to", quoted=True),
            execution_time="2024-01-02 00:00:00",
            updated_at_col=exp.column("updated_at", quoted=True),
            target_columns_to_types={
                "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                special_col: exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                "updated_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            },
        )
        sql_calls = to_sql_calls(adapter)
        insert_sql = [s for s in sql_calls if s.startswith('INSERT INTO "s"."t"')][0]
        assert '"col" - "name"' not in insert_sql
        assert "AS _col" not in insert_sql
        assert f'"inserted_rows" AS (SELECT "id", "{special_col}", "updated_at"' in insert_sql
        assert f'SELECT "_exists", "id", "{special_col}", "updated_at" FROM' in insert_sql

    elif scenario in (
        "reserved_from_by_column",
        "reserved_select_by_column",
        "hyphenated_col_by_column",
    ):
        special_col = {
            "reserved_from_by_column": "from",
            "reserved_select_by_column": "select",
            "hyphenated_col_by_column": "col-name",
        }[scenario]
        check_cols: t.Union[exp.Star, t.Sequence[exp.Expr]] = (
            [exp.column(special_col, quoted=True)]
            if scenario == "reserved_select_by_column"
            else exp.Star()
        )
        adapter.scd_type_2_by_column(
            target_table="s.t",
            source_table=parse_one(
                f'SELECT id, "{special_col}" FROM src',
                read="redshift",
            ),
            unique_key=[exp.column("id")],
            valid_from_col=exp.column("valid_from", quoted=True),
            valid_to_col=exp.column("valid_to", quoted=True),
            execution_time="2024-01-02 00:00:00",
            check_columns=check_cols,
            target_columns_to_types={
                "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                special_col: exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            },
        )
        sql_calls = to_sql_calls(adapter)
        insert_sql = [s for s in sql_calls if s.startswith('INSERT INTO "s"."t"')][0]
        assert '"col" - "name"' not in insert_sql
        assert "AS _col" not in insert_sql
        assert f'"inserted_rows" AS (SELECT "id", "{special_col}",' in insert_sql
        assert f'SELECT "_exists", "id", "{special_col}" FROM' in insert_sql

    elif scenario == "composite_key_hard_deletes_timestamptz":
        adapter.scd_type_2_by_time(
            target_table="s.t",
            source_table=parse_one("SELECT id, sub_id, val, updated_at FROM src", read="redshift"),
            unique_key=[exp.column("id"), exp.column("sub_id")],
            valid_from_col=exp.column("valid_from", quoted=True),
            valid_to_col=exp.column("valid_to", quoted=True),
            execution_time="2024-01-02 00:00:00+00:00",
            updated_at_col=exp.column("updated_at", quoted=True),
            invalidate_hard_deletes=True,
            updated_at_as_valid_from=True,
            target_columns_to_types={
                "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                "sub_id": exp.DataType.build("INT", dialect="redshift"),
                "val": exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                "updated_at": exp.DataType.build("TIMESTAMPTZ", dialect="redshift"),
                "valid_from": exp.DataType.build("TIMESTAMPTZ", dialect="redshift"),
                "valid_to": exp.DataType.build("TIMESTAMPTZ", dialect="redshift"),
            },
        )
        sql_calls = to_sql_calls(adapter)
        insert_sql = [s for s in sql_calls if s.startswith('INSERT INTO "s"."t"')][0]
        assert 'PARTITION BY "id", "sub_id" ORDER BY "id", "sub_id"' in insert_sql
        assert "TIMESTAMP WITH TIME ZONE" in insert_sql
        assert 'WHEN "joined"."_exists" IS NULL THEN' in insert_sql
        assert "1970-01-01" not in insert_sql

    elif scenario == "expression_key_no_hard_deletes":
        adapter.scd_type_2_by_time(
            target_table="s.t",
            source_table=parse_one("SELECT id, val, updated_at FROM src", read="redshift"),
            unique_key=[parse_one("COALESCE(id, '')", dialect="redshift")],
            valid_from_col=exp.column("valid_from", quoted=True),
            valid_to_col=exp.column("valid_to", quoted=True),
            execution_time="2024-01-02 00:00:00",
            updated_at_col=exp.column("updated_at", quoted=True),
            invalidate_hard_deletes=False,
            updated_at_as_valid_from=False,
            target_columns_to_types={
                "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                "val": exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                "updated_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            },
        )
        sql_calls = to_sql_calls(adapter)
        insert_sql = [s for s in sql_calls if s.startswith('INSERT INTO "s"."t"')][0]
        assert "COALESCE(\"id\", '')" in insert_sql
        assert 'WHEN "joined"."_exists" IS NULL THEN' not in insert_sql
        assert "CAST('1970-01-01 00:00:00' AS TIMESTAMP)" in insert_sql

    elif scenario == "dataframe_source_by_time":
        df = pd.DataFrame(
            {
                "id": ["a", "b"],
                "val": ["x", "y"],
                "updated_at": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
            }
        )
        adapter.scd_type_2_by_time(
            target_table="s.t",
            source_table=df,
            unique_key=[exp.column("id")],
            valid_from_col=exp.column("valid_from", quoted=True),
            valid_to_col=exp.column("valid_to", quoted=True),
            execution_time="2024-01-02 00:00:00",
            updated_at_col=exp.column("updated_at", quoted=True),
            target_columns_to_types={
                "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                "val": exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                "updated_at": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            },
        )
        sql_calls = to_sql_calls(adapter)
        insert_sql = [s for s in sql_calls if s.startswith('INSERT INTO "s"."t"')][0]
        assert "UNION ALL SELECT" in insert_sql
        assert "VALUES" not in insert_sql

    elif scenario == "by_column_strategy_star_and_explicit":
        model_star = load_sql_based_model(
            d.parse(
                """
                MODEL (
                  name s.t,
                  kind SCD_TYPE_2_BY_COLUMN (
                    unique_key id,
                    columns *,
                    execution_time_as_valid_from true
                  )
                );
                SELECT id, val FROM src
                """
            ),
            dialect="redshift",
        )
        assert not model_star.annotated
        strategy = SCDType2Strategy(adapter)

        mocker.patch.object(adapter, "table_exists", return_value=False)
        mocker.patch.object(adapter, "columns", return_value={})
        strategy.create(
            table_name="s.t",
            model=model_star,
            is_table_deployable=True,
            render_kwargs={},
            skip_grants=True,
        )
        create_calls = to_sql_calls(adapter)
        assert 'CREATE TABLE "s"."t"' not in create_calls
        assert (
            'CREATE TABLE "s"."t" AS SELECT "id" AS "id", "val" AS "val", CAST(NULL AS TIMESTAMP) AS "valid_from", CAST(NULL AS TIMESTAMP) AS "valid_to" FROM "src" AS "src" WHERE FALSE LIMIT 0'
            in create_calls
        )

        mocker.patch.object(
            adapter,
            "columns",
            return_value={
                "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                "val": exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
            },
        )
        for exec_as_valid_from, check_cols_arg in (
            (True, exp.Star()),
            (False, [exp.column("val")]),
        ):
            adapter.cursor.execute.reset_mock()
            adapter.scd_type_2_by_column(
                target_table="s.t",
                source_table=parse_one("SELECT id, val FROM src", read="redshift"),
                unique_key=[exp.column("id")],
                valid_from_col=exp.column("valid_from", quoted=True),
                valid_to_col=exp.column("valid_to", quoted=True),
                execution_time="2024-01-02 00:00:00",
                check_columns=check_cols_arg,
                execution_time_as_valid_from=exec_as_valid_from,
                truncate=True,
                target_columns_to_types={
                    "id": exp.DataType.build("VARCHAR(36)", dialect="redshift"),
                    "val": exp.DataType.build("VARCHAR(100)", dialect="redshift"),
                    "valid_from": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                    "valid_to": exp.DataType.build("TIMESTAMP", dialect="redshift"),
                },
            )
            sub_calls = to_sql_calls(adapter)
            for sql in sub_calls:
                parse_one(sql, read="redshift")
            sub_insert = [s for s in sub_calls if s.startswith('INSERT INTO "s"."t"')][0]
            if exec_as_valid_from:
                assert "1970-01-01" not in sub_insert
            else:
                assert "CAST('1970-01-01 00:00:00' AS TIMESTAMP)" in sub_insert
        sql_calls = create_calls

    for sql in sql_calls:
        parse_one(sql, read="redshift")
