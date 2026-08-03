# type: ignore

import typing as t

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock import MockerFixture
from sqlglot import exp, parse_one

from sqlmesh.core.engine_adapter import FabricEngineAdapter
from tests.core.engine_adapter import to_sql_calls
from sqlmesh.core.engine_adapter.shared import DataObject

pytestmark = [pytest.mark.engine, pytest.mark.fabric]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable) -> FabricEngineAdapter:
    return make_mocked_engine_adapter(FabricEngineAdapter)


def test_get_current_catalog_uses_only_explicit_target_catalog(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        database="default_catalog",
    )

    assert adapter.get_current_catalog() is None

    adapter._target_catalog = "switched_catalog"

    assert adapter.get_current_catalog() == "switched_catalog"

    adapter._connection_pool.close()

    assert adapter._connection_pool.get_attribute("target_catalog") is None
    assert adapter.get_current_catalog() is None
    adapter.cursor.execute.assert_not_called()


def test_get_current_catalog_returns_none_without_target_or_database(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(FabricEngineAdapter)

    assert adapter.get_current_catalog() is None
    adapter.cursor.execute.assert_not_called()


def test_set_current_catalog_does_not_query_database(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        database="default_catalog",
    )

    adapter.set_current_catalog("new_catalog")

    assert adapter.get_current_catalog() == "new_catalog"
    adapter.cursor.execute.assert_not_called()


def test_set_current_catalog_to_default_clears_explicit_target(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        default_catalog="core",
        database="core",
    )

    adapter.set_current_catalog("planning")
    adapter.set_current_catalog("core")

    assert adapter.get_current_catalog() is None
    adapter.cursor.execute.assert_not_called()


def test_catalog_scoped_call_restores_to_neutral_without_close(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    """Decorator's restore-to-neutral must not close the existing connection."""
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        default_catalog="core",
        database="core",
    )
    close_spy = mocker.spy(adapter._connection_pool, "close")
    adapter.cursor.fetchone.return_value = (1,)

    adapter.table_exists("planning.db.table")

    # Decorator calls set_current_catalog("planning") then set_current_catalog(None).
    # Only the first call (None→planning) should trigger a connection close.
    assert close_spy.call_count == 1
    assert adapter._connected_catalog == "planning"
    assert adapter.get_current_catalog() is None


def test_default_catalog_after_non_default_catalog_reconnects(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        default_catalog="core",
        database="core",
    )
    close_spy = mocker.spy(adapter._connection_pool, "close")
    adapter.cursor.fetchone.return_value = (1,)

    adapter.table_exists("planning.db.table")
    adapter.table_exists("core.db.table")

    assert close_spy.call_count == 2
    assert adapter._connected_catalog is None
    assert adapter.get_current_catalog() is None


def test_repeated_same_catalog_reuses_connection(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    """Two consecutive operations on the same catalog share one connection."""
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        default_catalog="core",
        database="core",
    )
    close_spy = mocker.spy(adapter._connection_pool, "close")
    adapter.cursor.fetchone.return_value = (1,)

    adapter.table_exists("planning.db.table")
    adapter.table_exists("planning.db.table")

    # Only the very first switch (None→planning) should close.
    # The restore to neutral keeps the connection alive and the second
    # planning operation reuses it without another close.
    assert close_spy.call_count == 1
    assert adapter._connected_catalog == "planning"


def test_switching_between_catalogs_closes_each_time(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    """Switching to a different catalog always triggers a connection close."""
    adapter = make_mocked_engine_adapter(
        FabricEngineAdapter,
        default_catalog="core",
        database="core",
    )
    close_spy = mocker.spy(adapter._connection_pool, "close")
    adapter.cursor.fetchone.return_value = (1,)

    adapter.table_exists("safran.db.table")  # None→safran: 1 close
    adapter.table_exists("planning.db.table")  # safran→planning: 2nd close

    assert close_spy.call_count == 2
    assert adapter._connected_catalog == "planning"


def test_columns(adapter: FabricEngineAdapter):
    adapter.cursor.fetchall.return_value = [
        ("decimal_ps", "decimal", None, 5, 4),
        ("decimal", "decimal", None, 18, 0),
        ("float", "float", None, 53, None),
        ("char_n", "char", 10, None, None),
        ("varchar_n", "varchar", 10, None, None),
        ("nvarchar_max", "nvarchar", -1, None, None),
    ]

    assert adapter.columns("db.table") == {
        "decimal_ps": exp.DataType.build("decimal(5, 4)", dialect=adapter.dialect),
        "decimal": exp.DataType.build("decimal(18, 0)", dialect=adapter.dialect),
        "float": exp.DataType.build("float(53)", dialect=adapter.dialect),
        "char_n": exp.DataType.build("char(10)", dialect=adapter.dialect),
        "varchar_n": exp.DataType.build("varchar(10)", dialect=adapter.dialect),
        "nvarchar_max": exp.DataType.build("nvarchar(max)", dialect=adapter.dialect),
    }

    # Verify that the adapter queries the uppercase INFORMATION_SCHEMA
    adapter.cursor.execute.assert_called_once_with(
        """SELECT [COLUMN_NAME], [DATA_TYPE], [CHARACTER_MAXIMUM_LENGTH], [NUMERIC_PRECISION], [NUMERIC_SCALE] FROM [INFORMATION_SCHEMA].[COLUMNS] WHERE [TABLE_NAME] = 'table' AND [TABLE_SCHEMA] = 'db';"""
    )


def test_table_exists(adapter: FabricEngineAdapter):
    adapter.cursor.fetchone.return_value = (1,)
    assert adapter.table_exists("db.table")
    # Verify that the adapter queries the uppercase INFORMATION_SCHEMA
    adapter.cursor.execute.assert_called_once_with(
        """SELECT 1 FROM [INFORMATION_SCHEMA].[TABLES] WHERE [TABLE_NAME] = 'table' AND [TABLE_SCHEMA] = 'db';"""
    )

    adapter.cursor.fetchone.return_value = None
    assert not adapter.table_exists("db.table")


def test_insert_overwrite_by_time_partition(adapter: FabricEngineAdapter):
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, b FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(x.strftime("%Y-%m-%d")),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
    )

    # Fabric adapter should use DELETE/INSERT strategy, not MERGE.
    assert to_sql_calls(adapter) == [
        """DELETE FROM [test_table] WHERE [b] BETWEEN '2022-01-01' AND '2022-01-02';""",
        """INSERT INTO [test_table] ([a], [b]) SELECT [a], [b] FROM (SELECT [a] AS [a], [b] AS [b] FROM [tbl]) AS [_subquery] WHERE [b] BETWEEN '2022-01-01' AND '2022-01-02';""",
    ]


def test_replace_query(adapter: FabricEngineAdapter, mocker: MockerFixture):
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test_table", type="table")],
    )
    adapter.replace_query(
        "test_table", parse_one("SELECT a FROM tbl"), {"a": exp.DataType.build("int")}
    )

    # This behavior is inherited from MSSQLEngineAdapter and should be TRUNCATE + INSERT
    assert to_sql_calls(adapter) == [
        "TRUNCATE TABLE [test_table];",
        "INSERT INTO [test_table] ([a]) SELECT [a] FROM [tbl];",
    ]


def test_alter_table_column_type_workaround(adapter: FabricEngineAdapter, mocker: MockerFixture):
    """
    Tests the alter_table method's workaround for changing a column's data type.
    """
    # Mock set_current_catalog to avoid connection pool side effects
    set_catalog_mock = mocker.patch.object(adapter, "set_current_catalog")
    # Mock random_id to have a predictable temporary column name
    mocker.patch("sqlmesh.core.engine_adapter.fabric.random_id", return_value="abcdef")

    alter_expression = exp.Alter(
        this=exp.to_table("my_db.my_schema.my_table"),
        actions=[
            exp.AlterColumn(
                this=exp.to_column("col_a"),
                dtype=exp.DataType.build("BIGINT"),
            )
        ],
    )

    adapter.alter_table([alter_expression])

    set_catalog_mock.assert_called_once_with("my_db")

    expected_calls = [
        "ALTER TABLE [my_schema].[my_table] ADD [col_a__abcdef] BIGINT;",
        "UPDATE [my_schema].[my_table] SET [col_a__abcdef] = CAST([col_a] AS BIGINT);",
        "ALTER TABLE [my_schema].[my_table] DROP COLUMN [col_a];",
        "EXEC sp_rename 'my_schema.my_table.col_a__abcdef', 'col_a', 'COLUMN'",
    ]

    assert to_sql_calls(adapter) == expected_calls


def test_alter_table_direct_alteration(adapter: FabricEngineAdapter, mocker: MockerFixture):
    """
    Tests the alter_table method for direct alterations like adding a column.
    """
    set_catalog_mock = mocker.patch.object(adapter, "set_current_catalog")

    alter_expression = exp.Alter(
        this=exp.to_table("my_db.my_schema.my_table"),
        actions=[exp.ColumnDef(this=exp.to_column("new_col"), kind=exp.DataType.build("INT"))],
    )

    adapter.alter_table([alter_expression])

    set_catalog_mock.assert_called_once_with("my_db")

    expected_calls = [
        "ALTER TABLE [my_schema].[my_table] ADD [new_col] INT;",
    ]

    assert to_sql_calls(adapter) == expected_calls


def test_merge_pandas(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.fabric.FabricEngineAdapter.table_exists",
        return_value=False,
    )

    adapter = make_mocked_engine_adapter(FabricEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    df = pd.DataFrame({"id": [1, 2, 3], "ts": [1, 2, 3], "val": [4, 5, 6]})

    # 1 key
    adapter.merge(
        target_table=table_name,
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("TIMESTAMP"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id")],
    )
    adapter._connection_pool.get().bulk_copy.assert_called_with(
        f"__temp_target_{temp_table_id}", [(1, 1, 4), (2, 2, 5), (3, 3, 6)]
    )

    assert to_sql_calls(adapter) == [
        f"""IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '__temp_target_{temp_table_id}') EXEC('CREATE TABLE [__temp_target_{temp_table_id}] ([id] INT, [ts] DATETIME2(6), [val] INT)');""",
        f"MERGE INTO [target] AS [__MERGE_TARGET__] USING (SELECT CAST([id] AS INT) AS [id], CAST([ts] AS DATETIME2(6)) AS [ts], CAST([val] AS INT) AS [val] FROM [__temp_target_{temp_table_id}]) AS [__MERGE_SOURCE__] ON [__MERGE_TARGET__].[id] = [__MERGE_SOURCE__].[id] WHEN MATCHED THEN UPDATE SET [__MERGE_TARGET__].[ts] = [__MERGE_SOURCE__].[ts], [__MERGE_TARGET__].[val] = [__MERGE_SOURCE__].[val] WHEN NOT MATCHED THEN INSERT ([id], [ts], [val]) VALUES ([__MERGE_SOURCE__].[id], [__MERGE_SOURCE__].[ts], [__MERGE_SOURCE__].[val]);",
        f"DROP TABLE IF EXISTS [__temp_target_{temp_table_id}];",
    ]

    # 2 keys
    adapter.cursor.reset_mock()
    adapter._connection_pool.get().reset_mock()
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)
    adapter.merge(
        target_table=table_name,
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("TIMESTAMP"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id"), exp.to_column("ts")],
    )
    adapter._connection_pool.get().bulk_copy.assert_called_with(
        f"__temp_target_{temp_table_id}", [(1, 1, 4), (2, 2, 5), (3, 3, 6)]
    )

    assert to_sql_calls(adapter) == [
        f"""IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '__temp_target_{temp_table_id}') EXEC('CREATE TABLE [__temp_target_{temp_table_id}] ([id] INT, [ts] DATETIME2(6), [val] INT)');""",
        f"MERGE INTO [target] AS [__MERGE_TARGET__] USING (SELECT CAST([id] AS INT) AS [id], CAST([ts] AS DATETIME2(6)) AS [ts], CAST([val] AS INT) AS [val] FROM [__temp_target_{temp_table_id}]) AS [__MERGE_SOURCE__] ON [__MERGE_TARGET__].[id] = [__MERGE_SOURCE__].[id] AND [__MERGE_TARGET__].[ts] = [__MERGE_SOURCE__].[ts] WHEN MATCHED THEN UPDATE SET [__MERGE_TARGET__].[val] = [__MERGE_SOURCE__].[val] WHEN NOT MATCHED THEN INSERT ([id], [ts], [val]) VALUES ([__MERGE_SOURCE__].[id], [__MERGE_SOURCE__].[ts], [__MERGE_SOURCE__].[val]);",
        f"DROP TABLE IF EXISTS [__temp_target_{temp_table_id}];",
    ]


def test_merge_exists(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.fabric.FabricEngineAdapter.table_exists",
        return_value=False,
    )

    adapter = make_mocked_engine_adapter(FabricEngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "target"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    df = pd.DataFrame({"id": [1, 2, 3], "ts": [1, 2, 3], "val": [4, 5, 6]})

    # regular implementation
    adapter.merge(
        target_table=table_name,
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("TIMESTAMP"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id")],
    )

    assert to_sql_calls(adapter) == [
        f"""IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '__temp_target_{temp_table_id}') EXEC('CREATE TABLE [__temp_target_{temp_table_id}] ([id] INT, [ts] DATETIME2(6), [val] INT)');""",
        f"MERGE INTO [target] AS [__MERGE_TARGET__] USING (SELECT CAST([id] AS INT) AS [id], CAST([ts] AS DATETIME2(6)) AS [ts], CAST([val] AS INT) AS [val] FROM [__temp_target_{temp_table_id}]) AS [__MERGE_SOURCE__] ON [__MERGE_TARGET__].[id] = [__MERGE_SOURCE__].[id] WHEN MATCHED THEN UPDATE SET [__MERGE_TARGET__].[ts] = [__MERGE_SOURCE__].[ts], [__MERGE_TARGET__].[val] = [__MERGE_SOURCE__].[val] WHEN NOT MATCHED THEN INSERT ([id], [ts], [val]) VALUES ([__MERGE_SOURCE__].[id], [__MERGE_SOURCE__].[ts], [__MERGE_SOURCE__].[val]);",
        f"DROP TABLE IF EXISTS [__temp_target_{temp_table_id}];",
    ]

    # merge exists implementation
    adapter.cursor.reset_mock()
    adapter._connection_pool.get().reset_mock()
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)
    adapter.merge(
        target_table=table_name,
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("TIMESTAMP"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id")],
        physical_properties={"mssql_merge_exists": True},
    )

    assert to_sql_calls(adapter) == [
        f"""IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '__temp_target_{temp_table_id}') EXEC('CREATE TABLE [__temp_target_{temp_table_id}] ([id] INT, [ts] DATETIME2(6), [val] INT)');""",
        f"MERGE INTO [target] AS [__MERGE_TARGET__] USING (SELECT CAST([id] AS INT) AS [id], CAST([ts] AS DATETIME2(6)) AS [ts], CAST([val] AS INT) AS [val] FROM [__temp_target_{temp_table_id}]) AS [__MERGE_SOURCE__] ON [__MERGE_TARGET__].[id] = [__MERGE_SOURCE__].[id] WHEN MATCHED AND EXISTS(SELECT [__MERGE_TARGET__].[ts], [__MERGE_TARGET__].[val] EXCEPT SELECT [__MERGE_SOURCE__].[ts], [__MERGE_SOURCE__].[val]) THEN UPDATE SET [__MERGE_TARGET__].[ts] = [__MERGE_SOURCE__].[ts], [__MERGE_TARGET__].[val] = [__MERGE_SOURCE__].[val] WHEN NOT MATCHED THEN INSERT ([id], [ts], [val]) VALUES ([__MERGE_SOURCE__].[id], [__MERGE_SOURCE__].[ts], [__MERGE_SOURCE__].[val]);",
        f"DROP TABLE IF EXISTS [__temp_target_{temp_table_id}];",
    ]

    # merge exists and all model columns are keys
    adapter.cursor.reset_mock()
    adapter._connection_pool.get().reset_mock()
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)
    adapter.merge(
        target_table=table_name,
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("TIMESTAMP"),
        },
        unique_key=[exp.to_identifier("id"), exp.to_column("ts")],
        physical_properties={"mssql_merge_exists": True},
    )

    assert to_sql_calls(adapter) == [
        f"""IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '__temp_target_{temp_table_id}') EXEC('CREATE TABLE [__temp_target_{temp_table_id}] ([id] INT, [ts] DATETIME2(6))');""",
        f"MERGE INTO [target] AS [__MERGE_TARGET__] USING (SELECT CAST([id] AS INT) AS [id], CAST([ts] AS DATETIME2(6)) AS [ts] FROM [__temp_target_{temp_table_id}]) AS [__MERGE_SOURCE__] ON [__MERGE_TARGET__].[id] = [__MERGE_SOURCE__].[id] AND [__MERGE_TARGET__].[ts] = [__MERGE_SOURCE__].[ts] WHEN NOT MATCHED THEN INSERT ([id], [ts]) VALUES ([__MERGE_SOURCE__].[id], [__MERGE_SOURCE__].[ts]);",
        f"DROP TABLE IF EXISTS [__temp_target_{temp_table_id}];",
    ]


def test_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(FabricEngineAdapter)
    comment = "\\"

    create_table_comment_mock = mocker.patch.object(adapter, "_create_table_comment")
    create_column_comments_mock = mocker.patch.object(adapter, "_create_column_comments")
    mocker.patch.object(adapter, "_create_table")

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=comment,
        column_descriptions={"a": comment},
    )

    create_table_comment_mock.assert_not_called()
    create_column_comments_mock.assert_not_called()
    assert to_sql_calls(adapter) == []
