import re
import typing as t
from datetime import date
from decimal import Decimal

import numpy as np  # noqa: TID253
import pandas as pd  # noqa: TID253
import pytest
from pytest_mock import MockFixture
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one
from sqlglot.helper import ensure_list

from sqlmesh.core.engine_adapter import PostgresEngineAdapter
from sqlmesh.utils.errors import SQLMeshError
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.engine, pytest.mark.postgres]


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (
            {
                "schema_name": "test_schema",
            },
            'DROP SCHEMA IF EXISTS "test_schema"',
        ),
        (
            {
                "schema_name": "test_schema",
                "ignore_if_not_exists": False,
            },
            'DROP SCHEMA "test_schema"',
        ),
        (
            {
                "schema_name": "test_schema",
                "cascade": True,
            },
            'DROP SCHEMA IF EXISTS "test_schema" CASCADE',
        ),
        (
            {
                "schema_name": "test_schema",
                "cascade": True,
                "ignore_if_not_exists": False,
            },
            'DROP SCHEMA "test_schema" CASCADE',
        ),
    ],
)
def test_drop_schema(kwargs, expected, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)

    adapter.drop_schema(**kwargs)

    assert to_sql_calls(adapter) == ensure_list(expected)


def test_drop_schema_with_catalog(make_mocked_engine_adapter: t.Callable, mocker: MockFixture):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)

    adapter.get_current_catalog = mocker.MagicMock(return_value="other_catalog")

    with pytest.raises(
        SQLMeshError, match="requires that all catalog operations be against a single catalog"
    ):
        adapter.drop_schema("test_catalog.test_schema")


def test_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description="\\",
        column_descriptions={"a": "\\"},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TABLE IF NOT EXISTS "test_table" ("a" INT, "b" INT)',
        """COMMENT ON TABLE "test_table" IS '\\'""",
        """COMMENT ON COLUMN "test_table"."a" IS '\\'""",
    ]


def test_create_table_like(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)

    adapter.create_table_like("target_table", "source_table")
    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "target_table" (LIKE "source_table" INCLUDING ALL)'
    )


def test_merge_version_gte_15(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    adapter.server_version = (15, 0)

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        """MERGE INTO "target" AS "__MERGE_TARGET__" USING (SELECT "ID", "ts", "val" FROM "source") AS "__MERGE_SOURCE__" ON "__MERGE_TARGET__"."ID" = "__MERGE_SOURCE__"."ID" WHEN MATCHED THEN UPDATE SET "ID" = "__MERGE_SOURCE__"."ID", "ts" = "__MERGE_SOURCE__"."ts", "val" = "__MERGE_SOURCE__"."val" WHEN NOT MATCHED THEN INSERT ("ID", "ts", "val") VALUES ("__MERGE_SOURCE__"."ID", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")"""
    ]


def test_merge_version_lt_15(
    make_mocked_engine_adapter: t.Callable, make_temp_table_name: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    adapter.server_version = (14, 0)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "test"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TABLE "__temp_test_abcdefgh" AS SELECT CAST("ID" AS INT) AS "ID", CAST("ts" AS TIMESTAMP) AS "ts", CAST("val" AS INT) AS "val" FROM (SELECT "ID", "ts", "val" FROM "source") AS "_subquery"',
        'DELETE FROM "target" WHERE "ID" IN (SELECT "ID" FROM "__temp_test_abcdefgh")',
        'INSERT INTO "target" ("ID", "ts", "val") SELECT DISTINCT ON ("ID") "ID", "ts", "val" FROM "__temp_test_abcdefgh"',
        'DROP TABLE IF EXISTS "__temp_test_abcdefgh"',
    ]


def test_alter_table_drop_column_cascade(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)

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


def test_server_version(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)

    fetchone_mock = mocker.patch.object(adapter, "fetchone")
    fetchone_mock.return_value = ("14.0",)
    assert adapter.server_version == (14, 0)

    del adapter.server_version
    fetchone_mock.return_value = ("15.8",)
    assert adapter.server_version == (15, 8)

    del adapter.server_version
    fetchone_mock.return_value = ("15.13 (Debian 15.13-1.pgdg120+1)",)
    assert adapter.server_version == (15, 13)


def test_sync_grants_config(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    relation = exp.to_table("test_schema.test_table", dialect="postgres")
    new_grants_config = {"SELECT": ["user1", "user2"], "INSERT": ["user3"]}

    current_grants = [("SELECT", "old_user"), ("UPDATE", "admin_user")]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="postgres")

    assert executed_sql == (
        "SELECT privilege_type, grantee FROM information_schema.role_table_grants "
        "WHERE table_schema = 'test_schema' AND table_name = 'test_table' "
        "AND grantor = current_role AND grantee <> current_role"
    )

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 4

    assert 'GRANT SELECT ON "test_schema"."test_table" TO "user1", "user2"' in sql_calls
    assert 'GRANT INSERT ON "test_schema"."test_table" TO "user3"' in sql_calls
    assert 'REVOKE SELECT ON "test_schema"."test_table" FROM "old_user"' in sql_calls
    assert 'REVOKE UPDATE ON "test_schema"."test_table" FROM "admin_user"' in sql_calls


def test_sync_grants_config_with_overlaps(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    relation = exp.to_table("test_schema.test_table", dialect="postgres")
    new_grants_config = {"SELECT": ["user1", "user2", "user3"], "INSERT": ["user2", "user4"]}

    current_grants = [
        ("SELECT", "user1"),
        ("SELECT", "user5"),
        ("INSERT", "user2"),
        ("UPDATE", "user3"),
    ]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="postgres")

    assert executed_sql == (
        "SELECT privilege_type, grantee FROM information_schema.role_table_grants "
        "WHERE table_schema = 'test_schema' AND table_name = 'test_table' "
        "AND grantor = current_role AND grantee <> current_role"
    )

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 4

    assert 'GRANT SELECT ON "test_schema"."test_table" TO "user2", "user3"' in sql_calls
    assert 'GRANT INSERT ON "test_schema"."test_table" TO "user4"' in sql_calls
    assert 'REVOKE SELECT ON "test_schema"."test_table" FROM "user5"' in sql_calls
    assert 'REVOKE UPDATE ON "test_schema"."test_table" FROM "user3"' in sql_calls


def test_diff_grants_configs(make_mocked_engine_adapter: t.Callable):
    new_grants = {"select": ["USER1", "USER2"], "insert": ["user3"]}
    old_grants = {"SELECT": ["user1", "user4"], "UPDATE": ["user5"]}

    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    additions, removals = adapter._diff_grants_configs(new_grants, old_grants)

    assert additions["select"] == ["USER2"]
    assert additions["insert"] == ["user3"]

    assert removals["SELECT"] == ["user4"]
    assert removals["UPDATE"] == ["user5"]


def test_sync_grants_config_with_default_schema(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    relation = exp.to_table("test_table", dialect="postgres")  # No schema
    new_grants_config = {"SELECT": ["user1"], "INSERT": ["user2"]}

    currrent_grants = [("UPDATE", "old_user")]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=currrent_grants)
    get_schema_mock = mocker.patch.object(adapter, "_get_current_schema", return_value="public")

    adapter.sync_grants_config(relation, new_grants_config)

    get_schema_mock.assert_called_once()

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="postgres")

    assert executed_sql == (
        "SELECT privilege_type, grantee FROM information_schema.role_table_grants "
        "WHERE table_schema = 'public' AND table_name = 'test_table' "
        "AND grantor = current_role AND grantee <> current_role"
    )


# --------------------------------------------------------------------------------------------
# DataFrame bulk loading via COPY
# --------------------------------------------------------------------------------------------

_NULL_MARKER_RE = re.compile(r"NULL '([^']+)'")


def _capture_copy_calls(adapter: PostgresEngineAdapter) -> t.List[t.Tuple[str, str]]:
    """Records (sql, payload) for every `copy_expert` call; the payload is read at call time."""
    calls: t.List[t.Tuple[str, str]] = []

    def _copy_expert(sql: str, file: t.Any) -> None:
        calls.append((sql, file.read()))

    adapter.cursor.copy_expert.side_effect = _copy_expert
    return calls


@pytest.fixture
def copy_adapter(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
) -> PostgresEngineAdapter:
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    mocker.patch.object(adapter, "table_exists", return_value=False)
    mocker.patch(
        "sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table",
        return_value=make_temp_table_name("tbl", "abcdefgh"),
    )
    return adapter


def test_df_copy_chunks_sql_and_payload(copy_adapter: PostgresEngineAdapter):
    calls = _capture_copy_calls(copy_adapter)
    df = pd.DataFrame(
        {
            "a": [1, 2, 3],
            "b": ["x", None, 'q"uote, comma'],
            "c": [1.5, np.nan, 0.1],
            "d": [True, False, True],
            "e": pd.to_datetime(
                ["2020-01-01 00:00:00.123456", None, "1960-01-01 12:00:00"],  # type: ignore
                format="ISO8601",
            ),
        }
    )
    types = {
        "a": exp.DataType.build("int"),
        "b": exp.DataType.build("text"),
        "c": exp.DataType.build("double"),
        "d": exp.DataType.build("boolean"),
        "e": exp.DataType.build("timestamp"),
    }

    copy_adapter.DEFAULT_BATCH_SIZE = 2
    copy_adapter.insert_append("tbl", df, target_columns_to_types=types)

    # One COPY per chunk of `batch_size` rows
    assert len(calls) == 2
    marker = _NULL_MARKER_RE.search(calls[0][0]).group(1)  # type: ignore
    assert len(marker) <= 32  # pandas truncates longer `na_rep` values in float columns
    expected_sql = (
        'COPY "__temp_tbl_abcdefgh" ("a", "b", "c", "d", "e") '
        f"FROM STDIN WITH (FORMAT csv, NULL '{marker}')"
    )
    assert [sql for sql, _ in calls] == [expected_sql, expected_sql]
    assert (
        calls[0][1]
        == f"1,x,1.5,True,2020-01-01 00:00:00.123456\n2,{marker},{marker},False,{marker}\n"
    )
    assert calls[1][1] == '3,"q""uote, comma",0.1,True,1960-01-01 12:00:00\n'

    assert to_sql_calls(copy_adapter) == [
        'CREATE TABLE IF NOT EXISTS "__temp_tbl_abcdefgh" '
        '("a" BIGINT, "b" TEXT, "c" DECIMAL, "d" BOOLEAN, "e" TIMESTAMP)',
        'INSERT INTO "tbl" ("a", "b", "c", "d", "e") '
        'SELECT CAST("a" AS INT) AS "a", CAST("b" AS TEXT) AS "b", CAST("c" AS DOUBLE PRECISION) AS "c", '
        'CAST("d" AS BOOLEAN) AS "d", CAST("e" AS TIMESTAMP) AS "e" FROM "__temp_tbl_abcdefgh"',
        'DROP TABLE IF EXISTS "__temp_tbl_abcdefgh"',
    ]


def test_df_copy_batch_size_zero_is_single_chunk(copy_adapter: PostgresEngineAdapter):
    calls = _capture_copy_calls(copy_adapter)
    df = pd.DataFrame({"a": [1, 2, 3]})
    copy_adapter.DEFAULT_BATCH_SIZE = 0
    copy_adapter.insert_append("tbl", df, target_columns_to_types={"a": exp.DataType.build("int")})
    assert len(calls) == 1
    assert calls[0][1] == "1\n2\n3\n"


def test_df_copy_reorders_columns_and_skips_missing_source_columns(
    copy_adapter: PostgresEngineAdapter,
):
    calls = _capture_copy_calls(copy_adapter)
    df = pd.DataFrame({"b": ["x", "y"], "a": [1, 2], "unused": [9, 9]})
    copy_adapter.insert_append(
        "tbl",
        df,
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "b": exp.DataType.build("text"),
            "c": exp.DataType.build("int"),
        },
        source_columns=["b", "a"],
    )

    # Only the source columns are copied, in the source order
    assert len(calls) == 1
    assert calls[0][0].startswith('COPY "__temp_tbl_abcdefgh" ("b", "a") FROM STDIN')
    assert calls[0][1] == "x,1\ny,2\n"
    assert to_sql_calls(copy_adapter)[1] == (
        'INSERT INTO "tbl" ("a", "b", "c") SELECT CAST("a" AS INT) AS "a", CAST("b" AS TEXT) AS "b", '
        'CAST(NULL AS INT) AS "c" FROM "__temp_tbl_abcdefgh"'
    )


def test_df_copy_staging_table_is_created_once(
    copy_adapter: PostgresEngineAdapter, mocker: MockerFixture
):
    calls = _capture_copy_calls(copy_adapter)
    df = pd.DataFrame({"a": [1, 2]})
    source_queries = copy_adapter._df_to_source_queries(
        df, {"a": exp.DataType.build("int")}, 10000, "tbl"
    )
    assert len(source_queries) == 1

    # Once the staging table exists, calling the factory again must not reload the data
    with source_queries[0]:
        pass
    copy_adapter.table_exists.return_value = True  # type: ignore
    with source_queries[0]:
        pass
    assert len(calls) == 1


@pytest.mark.parametrize(
    "df, remove_copy_expert",
    [
        (pd.DataFrame({"a": [1, 2]}), True),  # driver without copy_expert (e.g. pg8000)
        (pd.DataFrame({"a": [{"k": 1}, {"k": 2}]}), False),  # dict values
        (pd.DataFrame({"a": [[1], [2]]}), False),  # list values
        (pd.DataFrame({"a": ["x", 1]}), False),  # mixed str / int
        (pd.DataFrame({"a": [b"x", b"y"]}), False),  # bytes
        (pd.DataFrame({"a": [Decimal("1.5"), Decimal("2")]}), False),  # Decimal
        (pd.DataFrame({"a": [date(2020, 1, 1), date(2020, 1, 2)]}), False),  # date objects
        (pd.DataFrame({"a": pd.Categorical(["x", "y"])}), False),  # unsupported dtype
        (pd.DataFrame({"a": pd.to_timedelta([1, 2], unit="s")}), False),  # unsupported dtype
        (pd.DataFrame({"a": np.array([1, 2], dtype="uint64")}), False),  # may exceed BIGINT
        (
            pd.DataFrame({"a": np.array([1, 2], dtype="float32")}),
            False,
        ),  # repr differs from float64
        (pd.DataFrame({"a": ["x", "\\."]}), False),  # `\.` can end COPY data in CSV format
    ],
)
def test_df_copy_falls_back_to_values(
    copy_adapter: PostgresEngineAdapter, df: pd.DataFrame, remove_copy_expert: bool
):
    if remove_copy_expert:
        del copy_adapter.cursor.copy_expert

    copy_adapter.insert_append("tbl", df, target_columns_to_types={"a": exp.DataType.build("text")})

    if not remove_copy_expert:
        copy_adapter.cursor.copy_expert.assert_not_called()
    sql_calls = to_sql_calls(copy_adapter)
    assert len(sql_calls) == 1
    assert "VALUES" in sql_calls[0]
    assert "__temp_" not in sql_calls[0]


def test_df_copy_falls_back_on_null_marker_collision(
    copy_adapter: PostgresEngineAdapter, mocker: MockerFixture
):
    mocker.patch("sqlmesh.core.engine_adapter.postgres.random_id", return_value="collide")
    marker = "__sqlmesh_null_collide__"
    df = pd.DataFrame({"a": ["x", marker]})

    copy_adapter.insert_append("tbl", df, target_columns_to_types={"a": exp.DataType.build("text")})

    copy_adapter.cursor.copy_expert.assert_not_called()
    sql_calls = to_sql_calls(copy_adapter)
    assert len(sql_calls) == 1
    assert "VALUES" in sql_calls[0]


def test_df_copy_not_used_for_empty_dataframe(copy_adapter: PostgresEngineAdapter):
    # The adapter itself must not create a staging table for an empty DataFrame
    df = pd.DataFrame({"a": pd.Series([], dtype="int64")})
    source_queries = copy_adapter._df_to_source_queries(
        df, {"a": exp.DataType.build("int")}, 10000, "tbl"
    )
    assert source_queries == []
    copy_adapter.cursor.copy_expert.assert_not_called()
    assert to_sql_calls(copy_adapter) == []


def test_df_copy_driver_call_is_isolated_in_copy_df(
    copy_adapter: PostgresEngineAdapter, mocker: MockerFixture
):
    """Only `_copy_df` may touch the driver's COPY API (keeps a psycopg3 migration small)."""
    copy_df = mocker.patch.object(copy_adapter, "_copy_df")
    df = pd.DataFrame({"a": [1, 2]})
    copy_adapter.insert_append("tbl", df, target_columns_to_types={"a": exp.DataType.build("int")})
    copy_df.assert_called_once()
    copy_adapter.cursor.copy_expert.assert_not_called()


def test_temp_table_name_respects_max_identifier_length(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    table = f"sqlmesh__schema.{'x' * 30}__{'y' * 28}"  # 60 character table name
    assert len(exp.to_table(table).name) == 60

    first = adapter._get_temp_table(table)
    second = adapter._get_temp_table(table)

    assert first.db == "sqlmesh__schema"
    for temp in (first, second):
        assert len(temp.name) <= 63
        assert temp.name.startswith("__temp_xxxxxx")  # head of the name is kept
        assert temp.name.rsplit("_", 1)[0].endswith("yyyyyy")  # tail of the name is kept
    assert first.name != second.name  # random suffix keeps names unique
    # Identifier-length validation on DDL must accept the staging name
    adapter._check_identifier_length(exp.Create(this=first, kind="TABLE"))


def test_temp_table_name_unchanged_when_short(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter)
    name = "z" * 47  # 7 prefix + 47 + 1 + 8 suffix == 63
    temp = adapter._get_temp_table(f"s.{name}")
    assert temp.name.startswith(f"__temp_{name}_")
    assert len(temp.name) == 63


class _FakeDbError(Exception):
    def __init__(self, pgcode: t.Optional[str]) -> None:
        super().__init__("boom")
        self.pgcode = pgcode


@pytest.mark.parametrize(
    "pgcode, in_transaction, raises",
    [
        ("25P02", True, False),  # aborted transaction: its rollback removes the staging table
        ("25P02", False, True),
        ("42501", True, True),
        (None, True, True),
    ],
)
def test_drop_copy_staging_table_error_handling(
    copy_adapter: PostgresEngineAdapter,
    mocker: MockerFixture,
    pgcode: t.Optional[str],
    in_transaction: bool,
    raises: bool,
):
    mocker.patch.object(copy_adapter, "drop_table", side_effect=_FakeDbError(pgcode))
    mocker.patch.object(
        type(copy_adapter._connection_pool),
        "is_transaction_active",
        new_callable=mocker.PropertyMock,
        return_value=in_transaction,
    )
    table = exp.to_table("tbl")
    if raises:
        with pytest.raises(_FakeDbError):
            copy_adapter._drop_copy_staging_table(table)
    else:
        copy_adapter._drop_copy_staging_table(table)


def test_df_copy_failure_drops_staging_table_and_reraises(
    copy_adapter: PostgresEngineAdapter, mocker: MockerFixture
):
    mocker.patch.object(copy_adapter, "_copy_df", side_effect=ValueError("copy failed"))
    df = pd.DataFrame({"a": [1, 2]})
    source_queries = copy_adapter._df_to_source_queries(
        df, {"a": exp.DataType.build("int")}, 10000, "tbl"
    )
    # `replace_query` calls the factory outside of `with source_query`, so the factory must clean up
    with pytest.raises(ValueError, match="copy failed"):
        source_queries[0].query_factory()
    assert to_sql_calls(copy_adapter)[-1] == 'DROP TABLE IF EXISTS "__temp_tbl_abcdefgh"'
