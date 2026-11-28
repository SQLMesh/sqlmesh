# type: ignore
import typing as t
from unittest.mock import call

import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one

from sqlmesh.core.engine_adapter.base_postgres import BasePostgresEngineAdapter

pytestmark = [pytest.mark.engine, pytest.mark.postgres, pytest.mark.redshift]


def test_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(BasePostgresEngineAdapter)
    adapter.cursor.fetchall.return_value = [("col", "INT")]

    resp = adapter.columns("db.table")
    adapter.cursor.execute.assert_called_once_with(
        'SELECT "attname" AS "column_name", '
        '"pg_catalog".format_type("atttypid", "atttypmod") AS "data_type" '
        'FROM "pg_catalog"."pg_attribute" '
        'JOIN "pg_catalog"."pg_class" ON "pg_class"."oid" = "attrelid" '
        'JOIN "pg_catalog"."pg_namespace" ON "pg_namespace"."oid" = "relnamespace" '
        """WHERE ("attnum" > 0 AND NOT "attisdropped" AND "relname" = 'table') AND "nspname" = 'db'"""
        ' ORDER BY "attnum"'
    )
    assert resp == {"col": exp.DataType.build("INT")}


def test_table_exists(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(BasePostgresEngineAdapter)
    adapter.cursor.fetchone.return_value = (1,)

    resp = adapter.table_exists("db.table")
    adapter.cursor.execute.assert_called_once_with(
        """SELECT 1 FROM "information_schema"."tables" WHERE "table_name" = 'table' AND "table_schema" = 'db'"""
    )
    assert resp
    adapter.cursor.fetchone.return_value = None
    resp = adapter.table_exists("db.table")
    assert not resp


def test_create_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(BasePostgresEngineAdapter)

    adapter.create_view("db.view", parse_one("SELECT 1"), replace=True)
    adapter.create_view("db.view", parse_one("SELECT 1"), replace=False)

    adapter.cursor.execute.assert_has_calls(
        [
            # 1st call
            call('DROP VIEW IF EXISTS "db"."view" CASCADE'),
            call('CREATE VIEW "db"."view" AS SELECT 1'),
            # 2nd call
            call('CREATE VIEW "db"."view" AS SELECT 1'),
        ]
    )


def test_drop_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(BasePostgresEngineAdapter)
    adapter.SUPPORTS_MATERIALIZED_VIEWS = True

    adapter.drop_view("db.view")
    adapter.drop_view("db.view", materialized=True)
    adapter.drop_view("db.view", cascade=False)

    adapter.cursor.execute.assert_has_calls(
        [
            # 1st call
            call('DROP VIEW IF EXISTS "db"."view" CASCADE'),
            # 2nd call
            call('DROP MATERIALIZED VIEW IF EXISTS "db"."view" CASCADE'),
            # 3rd call
            call('DROP VIEW IF EXISTS "db"."view"'),
        ]
    )


def test_get_current_schema(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(BasePostgresEngineAdapter)

    fetchone_mock = mocker.patch.object(adapter, "fetchone", return_value=("test_schema",))
    result = adapter._get_current_schema()

    assert result == "test_schema"
    fetchone_mock.assert_called_once()
    executed_query = fetchone_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="postgres")
    assert executed_sql == "SELECT CURRENT_SCHEMA"

    fetchone_mock.reset_mock()
    fetchone_mock.return_value = None
    result = adapter._get_current_schema()
    assert result == "public"

    fetchone_mock.reset_mock()
    fetchone_mock.return_value = (None,)  # search_path = '' or 'nonexistent_schema'
    result = adapter._get_current_schema()
    assert result == "public"
