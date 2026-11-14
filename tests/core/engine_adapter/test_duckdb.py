import typing as t

from unittest.mock import MagicMock

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one
from sqlmesh.core.engine_adapter import DuckDBEngineAdapter, EngineAdapter
from sqlmesh.utils.errors import SQLMeshError
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


@pytest.fixture
def ducklake_conn(duck_conn):
    # Don't call install_extension here: on Windows it fails with "Access is denied"
    # once an earlier test in this process has loaded DuckLake. LOAD auto-installs it
    # when missing, and Windows CI pre-installs it before pytest.
    duck_conn.load_extension("ducklake")
    return duck_conn


@pytest.mark.xdist_group("ducklake_extension")
def test_ducklake_partitioning(adapter: EngineAdapter, ducklake_conn, tmp_path):
    catalog = "a_ducklake_db"

    ducklake_conn.execute(
        f"ATTACH 'ducklake:{tmp_path}/{catalog}.ducklake' AS {catalog} (DATA_PATH '{tmp_path}');"
    )

    # no partitions on catalog creation
    partition_info = ducklake_conn.execute(
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
    partition_info = ducklake_conn.execute(
        f"SELECT * FROM __ducklake_metadata_{catalog}.main.ducklake_partition_info"
    ).fetchdf()
    assert partition_info.shape[0] == 1


@pytest.mark.xdist_group("ducklake_extension")
def test_drop_table_ducklake_no_cascade(adapter: EngineAdapter, ducklake_conn, tmp_path):
    # DuckLake does not implement DROP TABLE/VIEW ... CASCADE, so the adapter must
    # omit CASCADE for objects in a DuckLake catalog while keeping it for native catalogs.
    catalog = "a_ducklake_db"

    ducklake_conn.execute(
        f"ATTACH 'ducklake:{tmp_path}/{catalog}.ducklake' AS {catalog} (DATA_PATH '{tmp_path}');"
    )

    ducklake_conn.execute(f"CREATE SCHEMA {catalog}.phys")
    ducklake_conn.execute(f"CREATE SCHEMA {catalog}.virt")
    ducklake_conn.execute(f"CREATE TABLE {catalog}.phys.t (i INTEGER)")
    ducklake_conn.execute(f"CREATE VIEW {catalog}.virt.v AS SELECT * FROM {catalog}.phys.t")

    # native catalog, cascade is passed through
    ducklake_conn.execute("CREATE TABLE memory.main.native_t (i INTEGER)")
    ducklake_conn.execute("CREATE VIEW memory.main.native_v AS SELECT * FROM memory.main.native_t")

    adapter.drop_table(f"{catalog}.phys.t", cascade=True)
    adapter.drop_view(f"{catalog}.virt.v", cascade=True)
    adapter.drop_table("memory.main.native_t", cascade=True)
    adapter.drop_view("memory.main.native_v", cascade=True)

    assert not adapter.table_exists(f"{catalog}.phys.t")
    assert not adapter.table_exists(f"{catalog}.virt.v")
    assert not adapter.table_exists("memory.main.native_t")
    assert not adapter.table_exists("memory.main.native_v")


@pytest.mark.xdist_group("ducklake_extension")
def test_drop_table_ducklake_no_cascade_mixed_case_alias(
    adapter: EngineAdapter, ducklake_conn, tmp_path
):
    # Catalog aliases keep their case in duckdb_databases(), but SQLMesh normalizes
    # table names to lowercase, so the catalog type lookup must be case-insensitive.
    ducklake_conn.execute(
        f"ATTACH 'ducklake:{tmp_path}/my_lake.ducklake' AS \"MyLake\" (DATA_PATH '{tmp_path}');"
    )
    ducklake_conn.execute("CREATE SCHEMA MyLake.phys")
    ducklake_conn.execute("CREATE TABLE MyLake.phys.t (i INTEGER)")
    ducklake_conn.execute("CREATE VIEW MyLake.phys.v AS SELECT * FROM MyLake.phys.t")

    adapter.drop_table("mylake.phys.t", cascade=True)
    adapter.drop_view("mylake.phys.v", cascade=True)

    assert not adapter.table_exists("mylake.phys.t")
    assert not adapter.table_exists("mylake.phys.v")


def test_drop_object_cascade_by_catalog_type(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DuckDBEngineAdapter)
    adapter.fetchone = lambda *_args, **_kwargs: ("ducklake",)  # type: ignore

    adapter.drop_table("lake.phys.t", cascade=True)
    adapter.drop_view("lake.virt.v", cascade=True)
    # schema cascade is supported by DuckLake and must be preserved
    adapter.drop_schema("lake.virt", cascade=True)

    adapter.fetchone = lambda *_args, **_kwargs: ("duckdb",)  # type: ignore
    adapter.drop_table("native.phys.t", cascade=True)

    assert to_sql_calls(adapter) == [
        'DROP TABLE IF EXISTS "lake"."phys"."t"',
        'DROP VIEW IF EXISTS "lake"."virt"."v"',
        'DROP SCHEMA IF EXISTS "lake"."virt" CASCADE',
        'DROP TABLE IF EXISTS "native"."phys"."t" CASCADE',
    ]


# ---------------------------------------------------------------------------
# Native MERGE
# ---------------------------------------------------------------------------

MERGE_COLUMNS = {
    "id": exp.DataType.build("int"),
    "name": exp.DataType.build("text"),
    "value": exp.DataType.build("int"),
}


def _installed_duckdb_supports_merge() -> bool:
    import duckdb
    from packaging import version

    return version.parse(duckdb.__version__) >= version.parse("1.4.0")


@pytest.fixture
def native_merge_adapter(duck_conn, mocker: MockerFixture) -> DuckDBEngineAdapter:
    if not _installed_duckdb_supports_merge():
        pytest.skip("The installed duckdb doesn't support MERGE (requires >= 1.4.0)")
    mocker.patch("duckdb.__version__", "1.4.0")
    return DuckDBEngineAdapter(lambda: duck_conn)


def _set_up_merge_tables(duck_conn, target_rows: str = "", source_rows: str = "") -> None:
    duck_conn.execute("CREATE TABLE tgt (id INT, name TEXT, value INT)")
    duck_conn.execute("CREATE TABLE src (id INT, name TEXT, value INT)")
    if target_rows:
        duck_conn.execute(f"INSERT INTO tgt VALUES {target_rows}")
    if source_rows:
        duck_conn.execute(f"INSERT INTO src VALUES {source_rows}")


def _merge(adapter: DuckDBEngineAdapter, **kwargs: t.Any) -> None:
    kwargs.setdefault("target_table", "tgt")
    kwargs.setdefault("source_table", parse_one("SELECT id, name, value FROM src"))
    kwargs.setdefault("target_columns_to_types", MERGE_COLUMNS)
    kwargs.setdefault("unique_key", [exp.to_column("id")])
    adapter.merge(**kwargs)


def _rows(duck_conn, table: str = "tgt") -> t.List[t.Tuple]:
    return duck_conn.execute(f"SELECT * FROM {table} ORDER BY id NULLS LAST, name").fetchall()


def _when_matched(sql: str) -> exp.Whens:
    return t.cast(
        exp.Whens,
        parse_one(
            f"MERGE INTO x USING y ON 1 = 1 {sql}",
            dialect="duckdb",
        ).args["whens"],
    )


def _mocked_adapter(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    duckdb_version: str,
    catalog_type: t.Optional[str] = "duckdb",
    **kwargs: t.Any,
) -> DuckDBEngineAdapter:
    mocker.patch("duckdb.__version__", duckdb_version)
    adapter = make_mocked_engine_adapter(DuckDBEngineAdapter, **kwargs)
    mocker.patch.object(adapter, "get_current_catalog", return_value="memory")
    mocker.patch.object(adapter, "_get_catalog_type", return_value=catalog_type)
    return adapter


def test_merge_native_sql(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = _mocked_adapter(make_mocked_engine_adapter, mocker, "1.4.0")

    adapter.merge(
        target_table="tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
    )

    assert to_sql_calls(adapter) == [
        'MERGE INTO "tgt" AS "__MERGE_TARGET__" USING '
        '(SELECT DISTINCT ON ("id") * FROM (SELECT "id", "name", "value" FROM "src") AS "_src") AS "__MERGE_SOURCE__" '
        'ON "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id" '
        'WHEN MATCHED THEN UPDATE SET "id" = "__MERGE_SOURCE__"."id", "name" = "__MERGE_SOURCE__"."name", "value" = "__MERGE_SOURCE__"."value" '
        'WHEN NOT MATCHED THEN INSERT ("id", "name", "value") '
        'VALUES ("__MERGE_SOURCE__"."id", "__MERGE_SOURCE__"."name", "__MERGE_SOURCE__"."value")'
    ]


def test_merge_native_upsert(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10), (2, 'b', 20)", "(2, 'b2', 21), (3, 'c', 30)")
    _merge(native_merge_adapter)
    assert _rows(duck_conn) == [(1, "a", 10), (2, "b2", 21), (3, "c", 30)]


def test_merge_native_when_matched_condition(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10), (2, 'b', 20)", "(1, 'a2', 5), (2, 'b2', 25)")
    _merge(
        native_merge_adapter,
        when_matched=_when_matched(
            "WHEN MATCHED AND __MERGE_SOURCE__.value > __MERGE_TARGET__.value "
            "THEN UPDATE SET __MERGE_TARGET__.value = __MERGE_SOURCE__.value"
        ),
    )
    # Only the second row satisfies the condition; the name column is not in the SET list.
    assert _rows(duck_conn) == [(1, "a", 10), (2, "b", 25)]


def test_merge_native_when_matched_delete_and_multiple_whens(native_merge_adapter, duck_conn):
    _set_up_merge_tables(
        duck_conn,
        "(1, 'a', 10), (2, 'b', 20), (3, 'c', 30)",
        "(1, 'a2', 0), (2, 'b2', 21), (4, 'd', 40)",
    )
    _merge(
        native_merge_adapter,
        when_matched=_when_matched(
            "WHEN MATCHED AND __MERGE_SOURCE__.value = 0 THEN DELETE "
            "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.value = __MERGE_SOURCE__.value"
        ),
    )
    assert _rows(duck_conn) == [(2, "b", 21), (3, "c", 30), (4, "d", 40)]


def test_merge_native_when_not_matched_by_source(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10), (2, 'b', 20)", "(2, 'b2', 21)")
    _merge(
        native_merge_adapter,
        when_matched=_when_matched(
            "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.value = __MERGE_SOURCE__.value "
            "WHEN NOT MATCHED BY SOURCE THEN DELETE"
        ),
    )
    assert _rows(duck_conn) == [(2, "b", 21)]


def test_merge_native_merge_filter(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, native_merge_adapter, duck_conn
):
    merge_filter = parse_one("__MERGE_SOURCE__.value > __MERGE_TARGET__.value", dialect="duckdb")

    # The filter is AND-ed into the ON clause
    mocked = _mocked_adapter(make_mocked_engine_adapter, mocker, "1.4.0")
    mocked.merge(
        target_table="tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
        merge_filter=merge_filter.copy(),
    )
    assert (
        'ON "__MERGE_SOURCE__"."value" > "__MERGE_TARGET__"."value" '
        'AND "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id"'
    ) in to_sql_calls(mocked)[0]

    # Row 1 passes the filter and is updated, row 2 does not and is inserted again as a new row
    _set_up_merge_tables(duck_conn, "(1, 'a', 10), (2, 'b', 20)", "(1, 'a2', 11), (2, 'b2', 5)")
    _merge(native_merge_adapter, merge_filter=merge_filter)
    assert _rows(duck_conn) == [(1, "a2", 11), (2, "b", 20), (2, "b2", 5)]


def test_merge_native_deduplicates_source_keys(native_merge_adapter, duck_conn):
    _set_up_merge_tables(
        duck_conn,
        "(1, 'a', 10)",
        "(1, 'a2', 11), (1, 'a3', 12), (5, 'e', 50), (5, 'e2', 51)",
    )
    _merge(native_merge_adapter)
    rows = _rows(duck_conn)
    assert [r[0] for r in rows] == [1, 5]
    assert rows[0] in {(1, "a2", 11), (1, "a3", 12)}
    assert rows[1] in {(5, "e", 50), (5, "e2", 51)}


def test_merge_native_null_key_and_empty_source(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10), (NULL, 'n', 0)", "(NULL, 'n2', 1)")
    _merge(native_merge_adapter)
    # NULL never matches, so the NULL-keyed source row is inserted as a new row
    assert _rows(duck_conn) == [(1, "a", 10), (None, "n", 0), (None, "n2", 1)]

    duck_conn.execute("DELETE FROM src")
    _merge(native_merge_adapter)
    assert len(_rows(duck_conn)) == 3


def test_merge_native_dataframe_source(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10)")
    df = pd.DataFrame({"id": [1, 2], "name": ["a2", "b"], "value": [11, 20]})
    _merge(native_merge_adapter, source_table=df)
    assert _rows(duck_conn) == [(1, "a2", 11), (2, "b", 20)]
    # The temp table that backs the DataFrame is dropped afterwards
    assert duck_conn.execute("SELECT COUNT(*) FROM duckdb_tables()").fetchone() == (2,)


def test_merge_native_dataframe_source_cleanup_on_failure(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10)")
    df = pd.DataFrame({"id": [1], "name": ["a2"], "value": [11]})
    with pytest.raises(Exception):
        _merge(
            native_merge_adapter,
            source_table=df,
            when_matched=_when_matched("WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.nope = 1"),
        )
    assert duck_conn.execute("SELECT COUNT(*) FROM duckdb_tables()").fetchone() == (2,)


def test_merge_native_quoted_case_sensitive_identifiers(native_merge_adapter, duck_conn):
    duck_conn.execute('CREATE SCHEMA "MySchema"')
    duck_conn.execute('CREATE TABLE "MySchema"."MyTarget" ("ID" INT, "Name" TEXT)')
    duck_conn.execute('CREATE TABLE "MySchema"."MySource" ("ID" INT, "Name" TEXT)')
    duck_conn.execute('INSERT INTO "MySchema"."MyTarget" VALUES (1, \'a\')')
    duck_conn.execute("INSERT INTO \"MySchema\".\"MySource\" VALUES (1, 'a2'), (2, 'b')")
    native_merge_adapter.merge(
        target_table=exp.to_table('"MySchema"."MyTarget"'),
        source_table=parse_one('SELECT "ID", "Name" FROM "MySchema"."MySource"'),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "Name": exp.DataType.build("text"),
        },
        unique_key=[exp.to_column('"ID"')],
    )
    assert duck_conn.execute('SELECT * FROM "MySchema"."MyTarget" ORDER BY 1').fetchall() == [
        (1, "a2"),
        (2, "b"),
    ]


def test_merge_native_unique_key_expression(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10)", "(1, 'a', 11), (1, 'a', 12), (1, 'b', 13)")
    _merge(
        native_merge_adapter,
        unique_key=[parse_one("COALESCE(id, 0) || '|' || name", dialect="duckdb")],
    )
    # The existing 'a' row is matched through the key expression and updated (one of the two
    # duplicate source rows wins), 'b' is inserted. A mis-qualified key in ON would insert instead.
    rows = _rows(duck_conn)
    assert len(rows) == 2
    assert rows[0] in {(1, "a", 11), (1, "a", 12)}
    assert rows[1] == (1, "b", 13)


def test_merge_version_gate_falls_back_to_delete_insert(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = _mocked_adapter(make_mocked_engine_adapter, mocker, "1.3.2")

    adapter.merge(
        target_table="tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
    )
    sql_calls = to_sql_calls(adapter)
    assert not any(call.startswith("MERGE") for call in sql_calls)
    assert any(call.startswith('DELETE FROM "tgt"') for call in sql_calls)
    assert any(call.startswith('INSERT INTO "tgt"') for call in sql_calls)

    with pytest.raises(SQLMeshError, match="`when_matched` is not supported"):
        adapter.merge(
            target_table="tgt",
            source_table=parse_one("SELECT id, name, value FROM src"),
            target_columns_to_types=MERGE_COLUMNS,
            unique_key=[exp.to_column("id")],
            when_matched=_when_matched("WHEN MATCHED THEN DELETE"),
        )


@pytest.mark.parametrize("catalog_type", ["postgres", "sqlite", "mysql", None])
def test_merge_catalog_gate_falls_back_to_delete_insert(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, catalog_type
):
    adapter = _mocked_adapter(make_mocked_engine_adapter, mocker, "1.5.0", catalog_type)
    mocker.patch.object(adapter, "get_current_catalog", return_value="pg")

    adapter.merge(
        target_table="pg.s.tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
    )
    sql_calls = to_sql_calls(adapter)
    assert not any(call.startswith("MERGE") for call in sql_calls)
    assert any(call.startswith('DELETE FROM "pg"."s"."tgt"') for call in sql_calls)
    # The catalog type is looked up for the target's own catalog
    adapter._get_catalog_type.assert_called_with("pg")  # type: ignore


def test_merge_catalog_gate_uses_target_catalog_when_qualified(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = _mocked_adapter(make_mocked_engine_adapter, mocker, "1.5.0", "ducklake")
    get_type = t.cast(MagicMock, adapter._get_catalog_type)

    adapter.merge(
        target_table="lake.s.tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
    )
    get_type.assert_called_once_with("lake")
    assert to_sql_calls(adapter)[0].startswith('MERGE INTO "lake"."s"."tgt"')


def test_merge_motherduck_uses_native_without_catalog_lookup(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = _mocked_adapter(
        make_mocked_engine_adapter, mocker, "1.4.1", "motherduck", is_motherduck=True
    )
    get_type = t.cast(MagicMock, adapter._get_catalog_type)

    adapter.merge(
        target_table="md.s.tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
    )
    get_type.assert_not_called()
    assert to_sql_calls(adapter)[0].startswith('MERGE INTO "md"."s"."tgt"')


def test_merge_motherduck_old_client_falls_back(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = _mocked_adapter(make_mocked_engine_adapter, mocker, "1.3.2", is_motherduck=True)

    adapter.merge(
        target_table="md.s.tgt",
        source_table=parse_one("SELECT id, name, value FROM src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
    )
    assert not any(call.startswith("MERGE") for call in to_sql_calls(adapter))


def test_merge_does_not_strip_qualifiers_from_right_hand_side(native_merge_adapter, duck_conn):
    _set_up_merge_tables(duck_conn, "(1, 'a', 10)", "(1, 'a', 1)")
    _merge(
        native_merge_adapter,
        when_matched=_when_matched(
            "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.value = __MERGE_TARGET__.value + __MERGE_SOURCE__.value"
        ),
    )
    assert _rows(duck_conn) == [(1, "a", 11)]


@pytest.mark.isolated
@pytest.mark.xdist_group("ducklake_extension")
@pytest.mark.skipif(
    not _installed_duckdb_supports_merge(), reason="Installed duckdb doesn't support MERGE"
)
def test_merge_native_ducklake(ducklake_conn, tmp_path, mocker: MockerFixture):
    mocker.patch("duckdb.__version__", "1.4.0")
    adapter = DuckDBEngineAdapter(lambda: ducklake_conn)
    ducklake_conn.execute(
        f"ATTACH 'ducklake:{tmp_path}/lake.ducklake' AS lake (DATA_PATH '{tmp_path}')"
    )
    ducklake_conn.execute("CREATE SCHEMA lake.s")
    ducklake_conn.execute("CREATE TABLE lake.s.tgt (id INT, name TEXT, value INT)")
    ducklake_conn.execute("CREATE TABLE lake.s.src (id INT, name TEXT, value INT)")
    ducklake_conn.execute("INSERT INTO lake.s.tgt VALUES (1, 'a', 10), (2, 'b', 20)")
    ducklake_conn.execute(
        "INSERT INTO lake.s.src VALUES (2, 'b2', 21), (3, 'c', 30), (3, 'c2', 31)"
    )

    spy = mocker.spy(adapter, "_merge")
    adapter.merge(
        target_table="lake.s.tgt",
        source_table=parse_one("SELECT id, name, value FROM lake.s.src"),
        target_columns_to_types=MERGE_COLUMNS,
        unique_key=[exp.to_column("id")],
        when_matched=_when_matched(
            "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.value = __MERGE_SOURCE__.value"
        ),
    )
    spy.assert_called_once()
    rows = ducklake_conn.execute("SELECT * FROM lake.s.tgt ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[1] == (2, "b", 21)
