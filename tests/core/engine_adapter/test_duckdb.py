import typing as t
from datetime import date

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


@pytest.mark.parametrize("by_time", [True, False])
@pytest.mark.parametrize("use_dataframe", [True, False])
def test_scd_type_2_preserves_nulls(adapter: EngineAdapter, by_time: bool, use_dataframe: bool):
    columns_to_types = {
        name: exp.DataType.build(data_type)
        for name, data_type in {
            "id": "INT",
            "value": "TEXT",
            "tracked_value": "TEXT",
            "updated_at": "TIMESTAMP",
            "valid_from": "TIMESTAMP",
            "valid_to": "TIMESTAMP",
        }.items()
    }
    source_columns_to_types = {
        name: data_type
        for name, data_type in columns_to_types.items()
        if name not in {"valid_from", "valid_to"}
    }
    adapter.create_table("history", columns_to_types)
    scd_kwargs: t.Dict[str, t.Any] = {
        "unique_key": [exp.column("id")],
        "valid_from_col": exp.column("valid_from"),
        "valid_to_col": exp.column("valid_to"),
        "target_columns_to_types": columns_to_types,
    }
    loads: t.List[t.Tuple[str, t.List[t.Tuple[t.Any, ...]]]] = [
        (
            "2026-09-29",
            [
                (1, None, "initial", "2026-09-29"),
                (2, None, "stable", "2026-09-29"),
                (3, "C", "initial", "2026-09-29"),
                (4, "deleted", "stable", "2026-09-29"),
                (None, "null-key", "stable", "2026-09-29"),
            ],
        ),
        (
            "2026-09-30",
            [
                (1, "A", "next", "2026-09-30"),
                (2, "ignored", "stable", "2026-09-29"),
                (3, None, "next", "2026-09-30"),
                (5, "new", "stable", "2026-09-30"),
            ],
        ),
        (
            "2026-10-01",
            [
                (1, "B", "final", "2026-10-01"),
                (2, "ignored", "stable", "2026-09-29"),
                (3, None, "next", "2026-09-30"),
                (5, "new", "stable", "2026-09-30"),
            ],
        ),
    ]
    for execution_time, rows in loads:
        source = (
            pd.DataFrame(rows, columns=list(source_columns_to_types))
            if use_dataframe
            else adapter._values_to_sql(
                rows,
                target_columns_to_types=source_columns_to_types,
                batch_start=0,
                batch_end=len(rows),
            )
        )
        if by_time:
            adapter.scd_type_2_by_time(
                "history",
                source,
                execution_time=execution_time,
                updated_at_col=exp.column("updated_at"),
                updated_at_as_valid_from=True,
                **scd_kwargs,
            )
        else:
            adapter.scd_type_2_by_column(
                "history",
                source,
                execution_time=execution_time,
                check_columns=[exp.column("tracked_value")],
                execution_time_as_valid_from=True,
                **scd_kwargs,
            )

    assert adapter.fetchall(
        "SELECT id, value, CAST(valid_from AS DATE), CAST(valid_to AS DATE) "
        "FROM history ORDER BY id NULLS LAST, valid_from"
    ) == [
        (1, None, date(2026, 9, 29), date(2026, 9, 30)),
        (1, "A", date(2026, 9, 30), date(2026, 10, 1)),
        (1, "B", date(2026, 10, 1), None),
        (2, None, date(2026, 9, 29), None),
        (3, "C", date(2026, 9, 29), date(2026, 9, 30)),
        (3, None, date(2026, 9, 30), None),
        (4, "deleted", date(2026, 9, 29), date(2026, 9, 30)),
        (5, "new", date(2026, 9, 30), None),
        (None, "null-key", date(2026, 9, 29), date(2026, 9, 30)),
    ]


@pytest.mark.parametrize("by_time", [True, False])
@pytest.mark.parametrize("use_dataframe", [True, False])
@pytest.mark.parametrize("column_name", ["t__exists", "T__EXISTS"])
@pytest.mark.parametrize("data_type,value", [("TEXT", "keep"), ("BOOLEAN", False)])
def test_scd_type_2_marker_collision(
    adapter: EngineAdapter,
    by_time: bool,
    use_dataframe: bool,
    column_name: str,
    data_type: str,
    value: t.Any,
):
    columns_to_types = {
        name: exp.DataType.build(column_type)
        for name, column_type in {
            "id": "INT",
            column_name: data_type,
            "t__exists_2": data_type,
            "_exists_3": data_type,
            "updated_at": "TIMESTAMP",
            "valid_from": "TIMESTAMP",
            "valid_to": "TIMESTAMP",
        }.items()
    }
    source_columns_to_types = {
        name: column_type
        for name, column_type in columns_to_types.items()
        if name not in {"valid_from", "valid_to"}
    }
    adapter.create_table("history", columns_to_types)
    for execution_time, rows in [
        ("2026-09-29", [(1, value, value, value, "2026-09-29")]),
        (
            "2026-09-30",
            [
                (1, None, value, value, "2026-09-30"),
                (2, value, value, value, "2026-09-30"),
            ],
        ),
    ]:
        source = (
            pd.DataFrame(rows, columns=list(source_columns_to_types))
            if use_dataframe
            else adapter._values_to_sql(
                rows,
                target_columns_to_types=source_columns_to_types,
                batch_start=0,
                batch_end=len(rows),
            )
        )
        kwargs: t.Dict[str, t.Any] = {
            "unique_key": [exp.column("id")],
            "valid_from_col": exp.column("valid_from"),
            "valid_to_col": exp.column("valid_to"),
            "target_columns_to_types": columns_to_types,
        }
        if by_time:
            adapter.scd_type_2_by_time(
                "history",
                source,
                execution_time=execution_time,
                updated_at_col=exp.column("updated_at"),
                updated_at_as_valid_from=True,
                **kwargs,
            )
        else:
            adapter.scd_type_2_by_column(
                "history",
                source,
                execution_time=execution_time,
                check_columns=[exp.column(column_name)],
                execution_time_as_valid_from=True,
                **kwargs,
            )
    assert adapter.fetchall(
        f'SELECT id, "{column_name}", t__exists_2, _exists_3 FROM history ORDER BY id, valid_from'
    ) == [(1, value, value, value), (1, None, value, value), (2, value, value, value)]


@pytest.mark.parametrize("by_time", [True, False])
@pytest.mark.parametrize("use_dataframe", [True, False])
def test_scd_type_2_null_valid_from(adapter: EngineAdapter, by_time: bool, use_dataframe: bool):
    columns_to_types = {
        name: exp.DataType.build(data_type)
        for name, data_type in {
            "id": "INT",
            "value": "TEXT",
            "updated_at": "TIMESTAMP",
            "valid_from": "TIMESTAMP",
            "valid_to": "TIMESTAMP",
        }.items()
    }
    source_columns_to_types = {
        name: data_type
        for name, data_type in columns_to_types.items()
        if name not in {"valid_from", "valid_to"}
    }
    adapter.create_table("history", columns_to_types)
    scd_kwargs: t.Dict[str, t.Any] = {
        "unique_key": [exp.column("id")],
        "valid_from_col": exp.column("valid_from"),
        "valid_to_col": exp.column("valid_to"),
        "target_columns_to_types": columns_to_types,
    }
    for is_initial in [True, False]:
        rows: t.List[t.Tuple[t.Any, ...]] = (
            [(1, "keep", None)] if is_initial else [(2, "new", "2026-09-30")]
        )
        source = (
            pd.DataFrame(rows, columns=list(source_columns_to_types))
            if use_dataframe
            else adapter._values_to_sql(
                rows,
                target_columns_to_types=source_columns_to_types,
                batch_start=0,
                batch_end=1,
            )
        )
        if by_time:
            adapter.scd_type_2_by_time(
                "history",
                source,
                execution_time="2026-09-29" if is_initial else "2026-09-30",
                updated_at_col=exp.column("updated_at"),
                updated_at_as_valid_from=True,
                **scd_kwargs,
            )
        else:
            adapter.scd_type_2_by_column(
                "history",
                source,
                execution_time=exp.column("updated_at") if is_initial else "2026-09-30",
                check_columns=[exp.column("value")],
                execution_time_as_valid_from=True,
                **scd_kwargs,
            )
        assert adapter.fetchall("SELECT id, value FROM history ORDER BY id") == (
            [(1, "keep")] if is_initial else [(1, "keep"), (2, "new")]
        )


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
