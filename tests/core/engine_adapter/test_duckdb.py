import typing as t

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one
from sqlmesh.core.engine_adapter import DuckDBEngineAdapter, EngineAdapter
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.duckdb, pytest.mark.engine]


@pytest.fixture
def adapter(duck_conn):
    duck_conn.execute("CREATE VIEW tbl AS SELECT 1 AS a")
    return DuckDBEngineAdapter(lambda: duck_conn)


def test_create_view(adapter: EngineAdapter, duck_conn):
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"))  # type: ignore
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"))  # type: ignore
    assert duck_conn.execute("SELECT * FROM test_view").fetchall() == [(1,)]

    with pytest.raises(Exception):
        adapter.create_view("test_view", parse_one("SELECT a FROM tbl"), replace=False)  # type: ignore


def test_create_schema(adapter: EngineAdapter, duck_conn):
    adapter.create_schema("test_schema")
    assert duck_conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'test_schema'"
    ).fetchall() == [(1,)]
    with pytest.raises(Exception):
        adapter.create_schema("test_schema", ignore_if_exists=False, warn_on_error=False)


def test_table_exists(adapter: EngineAdapter, duck_conn):
    assert not adapter.table_exists("test_table")
    assert adapter.table_exists("tbl")


def test_create_table(adapter: EngineAdapter, duck_conn):
    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }
    expected_columns = [
        ("cola", "INTEGER", "YES", None, None, None),
        ("colb", "VARCHAR", "YES", None, None, None),
    ]
    adapter.create_table("test_table", columns_to_types)
    assert duck_conn.execute("DESCRIBE test_table").fetchall() == expected_columns
    adapter.create_table(
        "test_table2",
        columns_to_types,
        storage_format="ICEBERG",
        partitioned_by=[exp.to_column("colb")],
    )
    assert duck_conn.execute("DESCRIBE test_table").fetchall() == expected_columns


def test_replace_query_pandas(adapter: EngineAdapter, duck_conn):
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.replace_query(
        "test_table", df, {"a": exp.DataType.build("long"), "b": exp.DataType.build("long")}
    )
    pd.testing.assert_frame_equal(adapter.fetchdf("SELECT * FROM test_table"), df)


def test_set_current_catalog(make_mocked_engine_adapter: t.Callable, duck_conn):
    adapter = make_mocked_engine_adapter(DuckDBEngineAdapter)
    adapter.set_current_catalog("test_catalog")

    assert to_sql_calls(adapter) == [
        'USE "test_catalog"',
    ]


@pytest.mark.parametrize("as_keyword", [True, False])
def test_metadata_error_restores_catalog(
    adapter: EngineAdapter, duck_conn, mocker: MockerFixture, as_keyword: bool
):
    duck_conn.execute("ATTACH ':memory:' AS secondary")
    original_catalog = adapter.get_current_catalog()
    schema = exp.table_("", db="main", catalog="secondary")
    original_schema = schema.copy()
    error = RuntimeError("metadata query failed")

    mocker.patch.object(adapter, "fetchdf", side_effect=error)
    with pytest.raises(RuntimeError) as exc_info:
        if as_keyword:
            adapter._get_data_objects(schema_name=schema)
        else:
            adapter._get_data_objects(schema)

    assert exc_info.value is error
    assert schema == original_schema
    assert adapter.get_current_catalog() == original_catalog
    duck_conn.execute("CREATE TABLE subsequent_table (id INT)")
    assert duck_conn.execute(
        "SELECT database_name FROM duckdb_tables() WHERE table_name = 'subsequent_table'"
    ).fetchall() == [(original_catalog,)]


def test_temporary_table(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(DuckDBEngineAdapter)

    mocker.patch.object(adapter, "get_current_catalog", return_value="test_catalog")
    mocker.patch.object(adapter, "fetchone", return_value=("test_catalog",))

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_properties={"creatable_type": exp.Column(this=exp.Identifier(this="Temporary"))},
    )

    assert to_sql_calls(adapter) == [
        'CREATE TEMPORARY TABLE IF NOT EXISTS "test_table" ("a" INT, "b" INT)',
    ]


def test_create_catalog(make_mocked_engine_adapter: t.Callable) -> None:
    adapter: DuckDBEngineAdapter = make_mocked_engine_adapter(DuckDBEngineAdapter)
    adapter.create_catalog(exp.to_identifier("foo"))

    assert to_sql_calls(adapter) == ["ATTACH IF NOT EXISTS 'foo.db' AS \"foo\""]


def test_create_catalog_motherduck(make_mocked_engine_adapter: t.Callable) -> None:
    adapter: DuckDBEngineAdapter = make_mocked_engine_adapter(
        DuckDBEngineAdapter, is_motherduck=True
    )
    adapter.create_catalog(exp.to_identifier("foo"))

    assert to_sql_calls(adapter) == ['CREATE DATABASE IF NOT EXISTS "foo"']


def test_drop_catalog(make_mocked_engine_adapter: t.Callable) -> None:
    adapter: DuckDBEngineAdapter = make_mocked_engine_adapter(DuckDBEngineAdapter)
    adapter.drop_catalog(exp.to_identifier("foo"))

    assert to_sql_calls(adapter) == ['DETACH DATABASE IF EXISTS "foo"']


def test_drop_catalog_motherduck(make_mocked_engine_adapter: t.Callable) -> None:
    adapter: DuckDBEngineAdapter = make_mocked_engine_adapter(
        DuckDBEngineAdapter, is_motherduck=True
    )
    adapter.drop_catalog(exp.to_identifier("foo"))

    assert to_sql_calls(adapter) == ['DROP DATABASE IF EXISTS "foo" CASCADE']


def test_ducklake_partitioning(adapter: EngineAdapter, duck_conn, tmp_path):
    catalog = "a_ducklake_db"

    duck_conn.install_extension("ducklake")
    duck_conn.load_extension("ducklake")
    duck_conn.execute(
        f"ATTACH 'ducklake:{tmp_path}/{catalog}.ducklake' AS {catalog} (DATA_PATH '{tmp_path}');"
    )

    # no partitions on catalog creation
    partition_info = duck_conn.execute(
        f"SELECT * FROM __ducklake_metadata_{catalog}.main.ducklake_partition_info"
    ).fetchdf()
    assert partition_info.empty

    adapter.set_current_catalog(catalog)
    adapter.create_schema("test_schema")
    adapter.create_table(
        "test_schema.test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        partitioned_by=[exp.to_column("a"), exp.to_column("b")],
    )

    # 1 partition after table creation
    partition_info = duck_conn.execute(
        f"SELECT * FROM __ducklake_metadata_{catalog}.main.ducklake_partition_info"
    ).fetchdf()
    assert partition_info.shape[0] == 1
