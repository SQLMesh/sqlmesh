import typing as t

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


def _make_psycopg2_like_adapter(
    make_mocked_engine_adapter: t.Callable, **kwargs: t.Any
) -> PostgresEngineAdapter:
    adapter = make_mocked_engine_adapter(PostgresEngineAdapter, pre_ping=True, **kwargs)
    # psycopg2 has no `begin`: the first statement of a transaction starts it implicitly
    connection = adapter._connection_pool.get()
    del connection.begin
    del connection.cursor.return_value.begin
    del connection.cursor.return_value.commit
    del connection.cursor.return_value.rollback
    return adapter


@pytest.mark.parametrize("role", [None, "test_role"])
def test_pre_ping_commits_its_transaction(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, role: t.Optional[str]
):
    adapter = _make_psycopg2_like_adapter(
        make_mocked_engine_adapter,
        cursor_init=(lambda cursor: cursor.execute(f"SET ROLE {role}")) if role else None,
    )
    connection = adapter._connection_pool.get()

    adapter.execute("SELECT 'test'")

    call = mocker.call
    new_cursor = [call.cursor(), *([call.cursor().execute(f"SET ROLE {role}")] if role else [])]
    assert connection.mock_calls == [
        *new_cursor,
        call.cursor().execute("SELECT 1"),
        call.cursor().close(),
        # The ping closed the cursor, so `cursor_init` runs again before the commit
        *new_cursor,
        call.commit(),
        call.cursor().execute("SELECT 'test'"),
        call.commit(),
    ]


def test_pre_ping_failure_reconnects_without_commit(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = _make_psycopg2_like_adapter(make_mocked_engine_adapter)
    connection = adapter._connection_pool.get()
    connection.cursor.return_value.execute.side_effect = [RuntimeError("connection lost"), None]

    adapter.execute("SELECT 'test'")

    call = mocker.call
    assert connection.mock_calls == [
        call.cursor(),
        call.cursor().execute("SELECT 1"),
        call.cursor().close(),
        call.close(),
        call.cursor(),
        call.cursor().execute("SELECT 'test'"),
        call.commit(),
    ]


def test_ping_inside_transaction_does_not_commit(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = _make_psycopg2_like_adapter(make_mocked_engine_adapter)
    connection = adapter._connection_pool.get()

    with adapter.transaction():
        adapter.ping()
        adapter.execute("SELECT 'test'")

    call = mocker.call
    assert connection.mock_calls == [
        call.cursor(),
        call.cursor().execute("SELECT 1"),
        call.cursor().close(),
        call.cursor(),
        call.commit(),
        call.cursor().execute("SELECT 1"),
        call.cursor().close(),
        call.cursor(),
        call.cursor().execute("SELECT 'test'"),
        call.commit(),
    ]


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
