import typing as t
from datetime import datetime, timezone

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

import sqlmesh.core.dialect as d
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.engine_adapter import SnowflakeEngineAdapter
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.engine_adapter.mixins import (
    TableAlterChangeClusterKeyOperation,
    TableAlterDropClusterKeyOperation,
)
from sqlmesh.core.engine_adapter.shared import DataObject, DataObjectType
from sqlmesh.core.schema_diff import TableAlterAddColumnOperation, TableAlterDropColumnOperation
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.model.definition import SqlModel
from sqlmesh.core.node import IntervalUnit
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils import optional_import
from tests.core.engine_adapter import to_sql_calls
from sqlmesh.core.model.kind import ViewKind

pytestmark = [pytest.mark.engine, pytest.mark.snowflake]


@pytest.fixture
def snowflake_mocked_engine_adapter(
    make_mocked_engine_adapter: t.Callable,
) -> SnowflakeEngineAdapter:
    return make_mocked_engine_adapter(SnowflakeEngineAdapter)


def test_get_temp_table(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    mocker.patch("sqlmesh.core.engine_adapter.base.random_id", return_value="abcdefgh")

    value = adapter._get_temp_table(
        normalize_model_name("catalog.db.test_table", default_catalog=None, dialect=adapter.dialect)
    )

    assert value.sql(dialect=adapter.dialect) == '"CATALOG"."DB"."__temp_TEST_TABLE_abcdefgh"'


def test_get_data_objects_lowercases_columns(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter, patch_get_data_objects=False)

    adapter.get_current_catalog = mocker.Mock(return_value="TEST_CATALOG")

    adapter.fetchdf = mocker.Mock(
        return_value=pd.DataFrame(  # type: ignore[assignment]
            [
                {
                    "CATALOG": "TEST_CATALOG",
                    "NAME": "MY_TABLE",
                    "SCHEMA_NAME": "PUBLIC",
                    "TYPE": "TABLE",
                    "CLUSTERING_KEY": "ID",
                }
            ]
        )
    )

    data_objects = adapter._get_data_objects("TEST_CATALOG.PUBLIC")

    assert len(data_objects) == 1
    data_object = data_objects[0]
    assert data_object.catalog == "TEST_CATALOG"
    assert data_object.schema_name == "PUBLIC"
    assert data_object.name == "MY_TABLE"
    assert data_object.type == DataObjectType.TABLE
    assert data_object.clustering_key == "ID"


def test_get_table_last_modified_ts_multiple_catalogs(
    snowflake_mocked_engine_adapter: SnowflakeEngineAdapter,
) -> None:
    adapter = snowflake_mocked_engine_adapter
    adapter.cursor.fetchall.side_effect = [
        [
            (datetime(2025, 1, 1, tzinfo=timezone.utc),),
            (datetime(2025, 1, 2, tzinfo=timezone.utc),),
        ],
        [(datetime(2025, 1, 3, tzinfo=timezone.utc),)],
    ]

    result = adapter.get_table_last_modified_ts(
        [
            '"ANALYTICS"."STAGING"."ORDERS"',
            '"RAW"."SHOPIFY"."ORDERS"',
            '"ANALYTICS"."STAGING"."CUSTOMERS"',
        ]
    )

    # INFORMATION_SCHEMA only covers the database it belongs to, so each database is queried separately
    assert to_sql_calls(adapter) == [
        """SELECT LAST_ALTERED FROM "ANALYTICS".INFORMATION_SCHEMA.TABLES WHERE (TABLE_NAME = 'ORDERS' AND TABLE_SCHEMA = 'STAGING') OR (TABLE_NAME = 'CUSTOMERS' AND TABLE_SCHEMA = 'STAGING')""",
        """SELECT LAST_ALTERED FROM "RAW".INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'ORDERS' AND TABLE_SCHEMA = 'SHOPIFY'""",
    ]
    assert result == [
        to_timestamp("2025-01-01"),
        to_timestamp("2025-01-02"),
        to_timestamp("2025-01-03"),
    ]


@pytest.mark.parametrize(
    "current_warehouse, current_warehouse_exp, configured_warehouse, configured_warehouse_exp, should_change",
    [
        (
            "test_warehouse",
            '"test_warehouse"',
            exp.to_identifier("test_warehouse", quoted=True),
            '"test_warehouse"',
            False,
        ),
        ("test_warehouse", '"test_warehouse"', "test_warehouse", '"TEST_WAREHOUSE"', True),
        ("TEST_WAREHOUSE", '"TEST_WAREHOUSE"', "test_warehouse", '"TEST_WAREHOUSE"', False),
        ("test warehouse", '"test warehouse"', "test warehouse", '"test warehouse"', False),
        ("test warehouse", '"test warehouse"', "another warehouse", '"another warehouse"', True),
        (
            "test warehouse",
            '"test warehouse"',
            exp.column("another warehouse"),
            '"another warehouse"',
            True,
        ),
        ("test warehouse", '"test warehouse"', "another_warehouse", '"ANOTHER_WAREHOUSE"', True),
        ("TEST_WAREHOUSE", '"TEST_WAREHOUSE"', "another_warehouse", '"ANOTHER_WAREHOUSE"', True),
        ("test_warehouse", '"test_warehouse"', "another_warehouse", '"ANOTHER_WAREHOUSE"', True),
        (
            "test_warehouse",
            '"test_warehouse"',
            exp.column("another_warehouse"),
            '"ANOTHER_WAREHOUSE"',
            True,
        ),
        (
            "test_warehouse",
            '"test_warehouse"',
            exp.to_identifier("another_warehouse"),
            '"ANOTHER_WAREHOUSE"',
            True,
        ),
        (
            "test_warehouse",
            '"test_warehouse"',
            exp.to_identifier("another_warehouse", quoted=True),
            '"another_warehouse"',
            True,
        ),
    ],
)
def test_session(
    mocker: MockerFixture,
    make_mocked_engine_adapter: t.Callable,
    current_warehouse: t.Union[str, exp.Expr],
    current_warehouse_exp: str,
    configured_warehouse: t.Optional[str],
    configured_warehouse_exp: t.Optional[str],
    should_change: bool,
):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    adapter.cursor.fetchone.return_value = (current_warehouse,)

    # Test normal execution
    with adapter.session({"warehouse": configured_warehouse}):
        pass

    expected_calls = []
    if configured_warehouse:
        expected_calls.append("SELECT CURRENT_WAREHOUSE()")
    if should_change:
        expected_calls.extend(
            [
                f"USE WAREHOUSE {configured_warehouse_exp}",
                f"USE WAREHOUSE {current_warehouse_exp}",
            ]
        )

    assert to_sql_calls(adapter) == expected_calls

    # Test exception handling - warehouse should still be reset
    if should_change:
        adapter.cursor.execute.reset_mock()
        adapter.cursor.fetchone.return_value = (current_warehouse,)

        try:
            with adapter.session({"warehouse": configured_warehouse}):
                adapter.execute("SELECT 1")
                raise RuntimeError("Test exception")
        except RuntimeError:
            pass

        expected_exception_calls = [
            "SELECT CURRENT_WAREHOUSE()",
            f"USE WAREHOUSE {configured_warehouse_exp}",
            "SELECT 1",
            f"USE WAREHOUSE {current_warehouse_exp}",
        ]

        assert to_sql_calls(adapter) == expected_exception_calls


def test_get_table_last_modified_ts_quoted_and_unquoted_catalogs(
    snowflake_mocked_engine_adapter: SnowflakeEngineAdapter,
) -> None:
    # unquoted raw resolves to the database RAW, quoted "raw" is a different database,
    # so the two must be looked up in two different information schemas
    adapter = snowflake_mocked_engine_adapter
    adapter.cursor.fetchall.side_effect = [
        [(datetime(2025, 1, 1, tzinfo=timezone.utc),)],
        [(datetime(2025, 1, 2, tzinfo=timezone.utc),)],
    ]

    result = adapter.get_table_last_modified_ts(['"raw".shopify.orders', "raw.shopify.customers"])

    assert result == [
        to_timestamp(datetime(2025, 1, 1, tzinfo=timezone.utc)),
        to_timestamp(datetime(2025, 1, 2, tzinfo=timezone.utc)),
    ]
    assert to_sql_calls(adapter) == [
        """SELECT LAST_ALTERED FROM "raw".INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'orders' AND TABLE_SCHEMA = 'shopify'""",
        """SELECT LAST_ALTERED FROM raw.INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'customers' AND TABLE_SCHEMA = 'shopify'""",
    ]


def test_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description="table description",
        column_descriptions={"a": "a column description"},
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description="table description",
        column_descriptions={"a": "a column description"},
    )

    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM source_table"),
        table_description="table description",
        column_descriptions={"a": "a column description"},
    )

    adapter._create_table_comment(
        "test_table",
        "table description",
    )

    adapter._create_column_comments(
        "test_table",
        {"a": "a column description"},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("a" INT COMMENT 'a column description', "b" INT) COMMENT='table description'""",
        """CREATE TABLE IF NOT EXISTS "test_table" ("a" INT COMMENT 'a column description', "b" INT) COMMENT='table description' AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        """CREATE OR REPLACE VIEW "test_view" COPY GRANTS COMMENT='table description' AS SELECT "a", "b" FROM "source_table\"""",
        """ALTER VIEW "test_view" ALTER COLUMN "a" COMMENT 'a column description'""",
        """COMMENT ON TABLE "test_table" IS 'table description'""",
        """ALTER TABLE "test_table" ALTER COLUMN "a" COMMENT 'a column description'""",
    ]


def test_multiple_column_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        column_descriptions={"a": "a column description", "b": "b column description"},
    )

    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM test_table"),
        column_descriptions={"a": "a column description", "b": "b column description"},
    )

    adapter._create_column_comments(
        "test_table",
        {"a": "a column description changed", "b": "b column description changed"},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("a" INT COMMENT 'a column description', "b" INT COMMENT 'b column description')""",
        """CREATE OR REPLACE VIEW "test_view" COPY GRANTS AS SELECT "a", "b" FROM "test_table\"""",
        """ALTER VIEW "test_view" ALTER COLUMN "a" COMMENT 'a column description', COLUMN "b" COMMENT 'b column description'""",
        """ALTER TABLE "test_table" ALTER COLUMN "a" COMMENT 'a column description changed', COLUMN "b" COMMENT 'b column description changed'""",
    ]


def test_sync_grants_config(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    relation = normalize_identifiers(
        exp.to_table("test_db.test_schema.test_table", dialect="snowflake"), dialect="snowflake"
    )
    new_grants_config = {"SELECT": ["ROLE role1", "ROLE role2"], "INSERT": ["ROLE role3"]}

    current_grants = [
        ("SELECT", "ROLE old_role"),
        ("UPDATE", "ROLE legacy_role"),
    ]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="snowflake")
    expected_sql = (
        "SELECT privilege_type, grantee FROM TEST_DB.INFORMATION_SCHEMA.TABLE_PRIVILEGES "
        "WHERE table_catalog = 'TEST_DB' AND table_schema = 'TEST_SCHEMA' AND table_name = 'TEST_TABLE' "
        "AND grantor = CURRENT_ROLE() AND grantee <> CURRENT_ROLE()"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 5

    assert 'GRANT SELECT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" TO ROLE "ROLE1"' in sql_calls
    assert 'GRANT SELECT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" TO ROLE "ROLE2"' in sql_calls
    assert 'GRANT INSERT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" TO ROLE "ROLE3"' in sql_calls
    assert (
        'REVOKE SELECT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" FROM ROLE "OLD_ROLE"'
        in sql_calls
    )
    assert (
        'REVOKE UPDATE ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" FROM ROLE "LEGACY_ROLE"'
        in sql_calls
    )


def test_sync_grants_config_with_overlaps(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    relation = normalize_identifiers(
        exp.to_table("test_db.test_schema.test_table", dialect="snowflake"), dialect="snowflake"
    )
    new_grants_config = {
        "SELECT": ["ROLE shared", "ROLE new_role"],
        "INSERT": ["ROLE shared", "ROLE writer"],
    }

    current_grants = [
        ("SELECT", "ROLE shared"),
        ("SELECT", "ROLE legacy"),
        ("INSERT", "ROLE shared"),
    ]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="snowflake")
    expected_sql = (
        """SELECT privilege_type, grantee FROM TEST_DB.INFORMATION_SCHEMA.TABLE_PRIVILEGES """
        "WHERE table_catalog = 'TEST_DB' AND table_schema = 'TEST_SCHEMA' AND table_name = 'TEST_TABLE' "
        "AND grantor = CURRENT_ROLE() AND grantee <> CURRENT_ROLE()"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 3

    assert (
        'GRANT SELECT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" TO ROLE "NEW_ROLE"' in sql_calls
    )
    assert (
        'GRANT INSERT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" TO ROLE "WRITER"' in sql_calls
    )
    assert (
        'REVOKE SELECT ON TABLE "TEST_DB"."TEST_SCHEMA"."TEST_TABLE" FROM ROLE "LEGACY"'
        in sql_calls
    )


@pytest.mark.parametrize(
    "table_type, expected_keyword",
    [
        (DataObjectType.TABLE, "TABLE"),
        (DataObjectType.VIEW, "VIEW"),
        (DataObjectType.MATERIALIZED_VIEW, "MATERIALIZED VIEW"),
        (DataObjectType.MANAGED_TABLE, "DYNAMIC TABLE"),
    ],
)
def test_sync_grants_config_object_kind(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    table_type: DataObjectType,
    expected_keyword: str,
) -> None:
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    relation = normalize_identifiers(
        exp.to_table("test_db.test_schema.test_object", dialect="snowflake"), dialect="snowflake"
    )

    mocker.patch.object(adapter, "fetchall", return_value=[])

    adapter.sync_grants_config(relation, {"SELECT": ["ROLE test"]}, table_type)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        f'GRANT SELECT ON {expected_keyword} "TEST_DB"."TEST_SCHEMA"."TEST_OBJECT" TO ROLE "TEST"'
    ]


def test_sync_grants_config_quotes(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    relation = normalize_identifiers(
        exp.to_table('"test_db"."test_schema"."test_table"', dialect="snowflake"),
        dialect="snowflake",
    )
    new_grants_config = {"SELECT": ["ROLE role1", "ROLE role2"], "INSERT": ["ROLE role3"]}

    current_grants = [
        ("SELECT", "ROLE old_role"),
        ("UPDATE", "ROLE legacy_role"),
    ]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="snowflake")
    expected_sql = (
        """SELECT privilege_type, grantee FROM "test_db".INFORMATION_SCHEMA.TABLE_PRIVILEGES """
        "WHERE table_catalog = 'test_db' AND table_schema = 'test_schema' AND table_name = 'test_table' "
        "AND grantor = CURRENT_ROLE() AND grantee <> CURRENT_ROLE()"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 5

    assert 'GRANT SELECT ON TABLE "test_db"."test_schema"."test_table" TO ROLE "ROLE1"' in sql_calls
    assert 'GRANT SELECT ON TABLE "test_db"."test_schema"."test_table" TO ROLE "ROLE2"' in sql_calls
    assert 'GRANT INSERT ON TABLE "test_db"."test_schema"."test_table" TO ROLE "ROLE3"' in sql_calls
    assert (
        'REVOKE SELECT ON TABLE "test_db"."test_schema"."test_table" FROM ROLE "OLD_ROLE"'
        in sql_calls
    )
    assert (
        'REVOKE UPDATE ON TABLE "test_db"."test_schema"."test_table" FROM ROLE "LEGACY_ROLE"'
        in sql_calls
    )


def test_sync_grants_config_no_catalog_or_schema(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    relation = normalize_identifiers(
        exp.to_table('"TesT_Table"', dialect="snowflake"), dialect="snowflake"
    )
    new_grants_config = {"SELECT": ["ROLE role1", "ROLE role2"], "INSERT": ["ROLE role3"]}

    current_grants = [
        ("SELECT", "ROLE old_role"),
        ("UPDATE", "ROLE legacy_role"),
    ]
    fetchall_mock = mocker.patch.object(adapter, "fetchall", return_value=current_grants)
    mocker.patch.object(adapter, "get_current_catalog", return_value="caTalog")
    mocker.patch.object(adapter, "_get_current_schema", return_value="sChema")

    adapter.sync_grants_config(relation, new_grants_config)

    fetchall_mock.assert_called_once()
    executed_query = fetchall_mock.call_args[0][0]
    executed_sql = executed_query.sql(dialect="snowflake")
    expected_sql = (
        """SELECT privilege_type, grantee FROM "caTalog".INFORMATION_SCHEMA.TABLE_PRIVILEGES """
        "WHERE table_catalog = 'caTalog' AND table_schema = 'sChema' AND table_name = 'TesT_Table' "
        "AND grantor = CURRENT_ROLE() AND grantee <> CURRENT_ROLE()"
    )
    assert executed_sql == expected_sql

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 5

    assert 'GRANT SELECT ON TABLE "TesT_Table" TO ROLE "ROLE1"' in sql_calls
    assert 'GRANT SELECT ON TABLE "TesT_Table" TO ROLE "ROLE2"' in sql_calls
    assert 'GRANT INSERT ON TABLE "TesT_Table" TO ROLE "ROLE3"' in sql_calls
    assert 'REVOKE SELECT ON TABLE "TesT_Table" FROM ROLE "OLD_ROLE"' in sql_calls
    assert 'REVOKE UPDATE ON TABLE "TesT_Table" FROM ROLE "LEGACY_ROLE"' in sql_calls


def test_df_to_source_queries_use_schema(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter.table_exists",
        return_value=False,
    )
    mocker.patch("snowflake.connector.pandas_tools.write_pandas", return_value=None)
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    adapter.DEFAULT_BATCH_SIZE = 1

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.replace_query(
        "other_db.test_table", df, {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")}
    )
    assert 'USE SCHEMA "other_db"' in to_sql_calls(adapter)

    adapter.replace_query(
        "other_catalog.other_db.test_table",
        df,
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
    )
    assert 'USE SCHEMA "other_catalog"."other_db"' in to_sql_calls(adapter)


def test_df_to_source_queries_reset_non_default_index(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    mocker.patch(
        "sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter.table_exists",
        return_value=False,
    )
    write_pandas = mocker.patch("snowflake.connector.pandas_tools.write_pandas", return_value=None)
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    df = pd.DataFrame({"a": [2, 3], "b": [5, 6]}, index=[1, 2])
    adapter.replace_query(
        "other_db.test_table", df, {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")}
    )

    uploaded_df = write_pandas.call_args.args[1]
    assert uploaded_df.index.equals(pd.RangeIndex(start=0, stop=2, step=1))
    assert uploaded_df.to_dict("list") == {"a": [2, 3], "b": [5, 6]}


def test_create_managed_table(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    mocker.patch(
        "sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter._current_warehouse",
        return_value=exp.to_identifier("default_warehouse"),
        new_callable=mocker.PropertyMock,
    )

    query = parse_one("SELECT a, b FROM source_table")
    columns_to_types = {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")}

    # no properties, should raise about TARGET_LAG
    with pytest.raises(SQLMeshError, match=r".*`target_lag` must be specified.*"):
        adapter.create_managed_table(
            table_name="test_table",
            query=query,
            target_columns_to_types=columns_to_types,
        )

    # warehouse not specified, should default to current_warehouse()
    adapter.create_managed_table(
        table_name="test_table",
        query=query,
        target_columns_to_types=columns_to_types,
        table_properties={"target_lag": exp.Literal.string("20 minutes")},
    )

    # warehouse specified, should use it
    adapter.create_managed_table(
        table_name="test_table",
        query=query,
        target_columns_to_types=columns_to_types,
        table_properties={
            "target_lag": exp.Literal.string("20 minutes"),
            "warehouse": exp.to_identifier("foo"),
        },
    )

    # clustered by, partitioned by (partitioned by should get ignored)
    adapter.create_managed_table(
        table_name="test_table",
        query=query,
        target_columns_to_types=columns_to_types,
        table_properties={
            "target_lag": exp.Literal.string("20 minutes"),
        },
        clustered_by=[exp.column("a")],
        partitioned_by=["b"],
    )

    # other properties
    adapter.create_managed_table(
        table_name="test_table",
        query=query,
        target_columns_to_types=columns_to_types,
        table_properties={
            "target_lag": exp.Literal.string("20 minutes"),
            "refresh_mode": exp.Literal.string("auto"),
            "initialize": exp.Literal.string("on_create"),
        },
    )

    # table_format=iceberg
    adapter.create_managed_table(
        table_name="test_table",
        query=query,
        target_columns_to_types=columns_to_types,
        table_properties={
            "target_lag": exp.Literal.string("20 minutes"),
            "catalog": exp.Literal.string("snowflake"),
            "external_volume": exp.Literal.string("test"),
        },
        table_format="iceberg",
    )

    assert to_sql_calls(adapter) == [
        """CREATE OR REPLACE DYNAMIC TABLE "test_table" TARGET_LAG='20 minutes' WAREHOUSE="default_warehouse" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        """CREATE OR REPLACE DYNAMIC TABLE "test_table" TARGET_LAG='20 minutes' WAREHOUSE="foo" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        """CREATE OR REPLACE DYNAMIC TABLE "test_table" CLUSTER BY ("a") TARGET_LAG='20 minutes' WAREHOUSE="default_warehouse" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        """CREATE OR REPLACE DYNAMIC TABLE "test_table" TARGET_LAG='20 minutes' REFRESH_MODE='auto' INITIALIZE='on_create' WAREHOUSE="default_warehouse" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        """CREATE OR REPLACE DYNAMIC ICEBERG TABLE "test_table" TARGET_LAG='20 minutes' CATALOG='snowflake' EXTERNAL_VOLUME='test' WAREHOUSE="default_warehouse" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
    ]


def test_drop_managed_table(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.drop_managed_table(table_name="foo.bar", exists=False)
    adapter.drop_managed_table(table_name="foo.bar", exists=True)

    assert to_sql_calls(adapter) == [
        'DROP DYNAMIC TABLE "foo"."bar"',
        'DROP DYNAMIC TABLE IF EXISTS "foo"."bar"',
    ]


def test_ctas_skips_dynamic_table_properties(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    query = parse_one("SELECT a, b FROM source_table")
    columns_to_types = {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")}

    adapter.ctas(
        table_name="test_table",
        query_or_df=query,
        target_columns_to_types=columns_to_types,
        table_properties={
            "warehouse": exp.to_identifier("foo"),
            "target_lag": exp.Literal.string("20 minutes"),
            "refresh_mode": exp.Literal.string("auto"),
            "initialize": exp.Literal.string("on_create"),
        },
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_table" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery"'
    ]


def test_set_current_catalog(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    adapter._default_catalog = "foo"

    adapter.set_current_catalog("foo")
    adapter.set_current_catalog("FOO")
    adapter.set_current_catalog("fOo")
    adapter.set_current_catalog("bar")
    adapter.set_current_catalog("BAR")

    model_a: SqlModel = t.cast(
        SqlModel,
        load_sql_based_model(
            d.parse(
                """
        MODEL (
            name external.test.table,
            kind full,
            dialect bigquery
        );

        SELECT 1;
    """
            )
        ),
    )

    model_b: SqlModel = t.cast(
        SqlModel,
        load_sql_based_model(
            d.parse(
                """
        MODEL (
            name "exTERnal".test.table,
            kind full,
            dialect bigquery
        );

        SELECT 1;
    """
            )
        ),
    )

    assert model_a.catalog == "external"
    assert model_b.catalog == "exTERnal"

    adapter.set_current_catalog(model_a.catalog)
    adapter.set_current_catalog(model_b.catalog)

    assert to_sql_calls(adapter) == [
        'USE "FOO"',
        'USE "FOO"',
        'USE "FOO"',
        'USE "bar"',
        'USE "BAR"',
        'USE "external"',
        'USE "exTERnal"',
    ]


def test_set_current_schema(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    adapter._default_catalog = "foo"

    adapter.set_current_schema('"foo"')
    adapter.set_current_schema('foo."foo"')

    # in this example, the catalog of '"foo"' is normalized in duckdb.
    # even though it's quoted, it should get replaced with "FOO"
    # because it matches the default catalog
    adapter.set_current_schema('"foo"."fOo"')

    assert to_sql_calls(adapter) == [
        'USE SCHEMA "foo"',
        'USE SCHEMA "FOO"."foo"',
        'USE SCHEMA "FOO"."fOo"',
    ]


def test_replace_query_snowpark_dataframe(
    mocker: MockerFixture, make_mocked_engine_adapter: t.Callable
):
    if not optional_import("snowflake.snowpark"):
        pytest.skip("Snowpark not available in this environment")

    from snowflake.snowpark.session import Session
    from snowflake.snowpark.dataframe import DataFrame as SnowparkDataFrame

    session = Session.builder.config("local_testing", True).create()
    # df.createOrReplaceTempView() throws "[Local Testing] Mocking SnowflakePlan Rename is not supported" when used against the Snowflake local_testing session
    # since we cant trace any queries from the Snowpark library anyway, we just suppress this and verify the cleanup queries issued by our EngineAdapter
    session._conn._suppress_not_implemented_error = True

    df: SnowparkDataFrame = session.create_dataframe([(1, "name")], schema=["ID", "NAME"])
    assert isinstance(df, SnowparkDataFrame)

    mocker.patch("sqlmesh.core.engine_adapter.base.random_id", return_value="e6wjkjj6")
    spy = mocker.spy(df, "createOrReplaceTempView")

    adapter: SnowflakeEngineAdapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)
    adapter._default_catalog = "foo"

    adapter.replace_query(
        table_name="foo",
        query_or_df=df,
        target_columns_to_types={
            "ID": exp.DataType.build("INT"),
            "NAME": exp.DataType.build("VARCHAR"),
        },
    )

    # verify that DROP VIEW is called instead of DROP TABLE
    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE TABLE "foo" AS SELECT CAST("ID" AS INT) AS "ID", CAST("NAME" AS VARCHAR) AS "NAME" FROM (SELECT CAST("ID" AS INT) AS "ID", CAST("NAME" AS VARCHAR) AS "NAME" FROM "__temp_foo_e6wjkjj6") AS "_subquery"',
        'DROP VIEW IF EXISTS "__temp_foo_e6wjkjj6"',
    ]


def test_creatable_type_materialized_view_properties(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        materialized=True,
        materialized_properties={
            # Partitioned by is not supported so we are confirming it is ignored
            "partitioned_by": [exp.column("ds")],
            "clustered_by": [exp.column("a")],
            "partition_interval_unit": IntervalUnit.DAY,
        },
    )

    sql_calls = to_sql_calls(adapter)
    # https://docs.snowflake.com/en/sql-reference/sql/create-materialized-view#syntax
    assert sql_calls == [
        'CREATE OR REPLACE MATERIALIZED VIEW "test_table" COPY GRANTS CLUSTER BY ("a") AS SELECT 1',
    ]


def test_creatable_type_secure_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        view_properties={
            "creatable_type": exp.Column(this=exp.Identifier(this="secure")),
        },
    )

    sql_calls = to_sql_calls(adapter)
    # https://docs.snowflake.com/en/sql-reference/sql/create-view.html
    assert sql_calls == [
        'CREATE OR REPLACE SECURE VIEW "test_table" COPY GRANTS AS SELECT 1',
    ]


def test_creatable_type_secure_materialized_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        materialized=True,
        view_properties={
            "creatable_type": exp.Column(this=exp.Identifier(this="secure")),
        },
    )

    sql_calls = to_sql_calls(adapter)
    # https://docs.snowflake.com/en/sql-reference/sql/create-view.html
    assert sql_calls == [
        'CREATE OR REPLACE SECURE MATERIALIZED VIEW "test_table" COPY GRANTS AS SELECT 1',
    ]


def test_creatable_type_temporary_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_view(
        "test_table",
        parse_one("SELECT 1"),
        view_properties={
            "creatable_type": exp.column("temporary"),
        },
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE OR REPLACE TEMPORARY VIEW "test_table" COPY GRANTS AS SELECT 1',
    ]


def test_creatable_type_temporary_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_properties={
            "creatable_type": exp.column("temporary"),
        },
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TEMPORARY TABLE IF NOT EXISTS "test_table" ("a" INT, "b" INT)',
    ]


def test_creatable_type_transient_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_properties={
            "creatable_type": exp.column("transient"),
        },
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TRANSIENT TABLE IF NOT EXISTS "test_table" ("a" INT, "b" INT)',
    ]


def test_creatable_type_materialize_creatable_type_raise_error(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    with pytest.raises(SQLMeshError):
        adapter.create_view(
            "test_view",
            parse_one("SELECT 1"),
            view_properties={
                "creatable_type": exp.column("materialized"),
            },
        )


def test_creatable_type_transient_type_from_model_definition(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    model: SqlModel = t.cast(
        SqlModel,
        load_sql_based_model(
            d.parse(
                """
MODEL (
    name external.test.table,
    kind full,
    physical_properties (
        creatable_type = transient
    )
);
SELECT a::INT;
    """
            )
        ),
    )
    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TRANSIENT TABLE IF NOT EXISTS "external"."test"."table" ("a" INT)',
    ]


def test_creatable_type_transient_type_from_model_definition_with_other_property(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    model: SqlModel = t.cast(
        SqlModel,
        load_sql_based_model(
            d.parse(
                """
MODEL (
    name external.test.table,
    kind full,
    physical_properties (
        creatable_type = transient,
        require_partition_filter = true
    )
);
SELECT a::INT;
    """
            )
        ),
    )
    adapter.create_table(
        model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_properties=model.physical_properties,
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TRANSIENT TABLE IF NOT EXISTS "external"."test"."table" ("a" INT) REQUIRE_PARTITION_FILTER=TRUE'
    ]


def test_create_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter)

    adapter.create_view("test_view", parse_one("SELECT 1"))
    adapter.create_view("test_view", parse_one("SELECT 1"), replace=False)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE OR REPLACE VIEW "test_view" COPY GRANTS AS SELECT 1',
        'CREATE VIEW "test_view" AS SELECT 1',
    ]


def test_clone_table(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    mocker.patch("sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter.set_current_catalog")
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter, default_catalog="test_catalog")
    adapter.clone_table("target_table", "source_table")
    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "target_table" CLONE "source_table"'
    )

    # Validate with transient type we create the clone table accordingly
    rendered_physical_properties = {
        "creatable_type": exp.column("transient"),
    }
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter, default_catalog="test_catalog")
    adapter.clone_table(
        "target_table", "source_table", rendered_physical_properties=rendered_physical_properties
    )
    adapter.cursor.execute.assert_called_once_with(
        'CREATE TRANSIENT TABLE IF NOT EXISTS "target_table" CLONE "source_table"'
    )

    # Validate other engine adapters would work as usual even when we pass the properties
    adapter = make_mocked_engine_adapter(EngineAdapter, default_catalog="test_catalog")
    adapter.SUPPORTS_CLONING = True
    adapter.clone_table(
        "target_table", "source_table", rendered_physical_properties=rendered_physical_properties
    )
    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "target_table" CLONE "source_table"'
    )


def test_table_format_iceberg(snowflake_mocked_engine_adapter: SnowflakeEngineAdapter) -> None:
    adapter = snowflake_mocked_engine_adapter

    model = load_sql_based_model(
        expressions=d.parse("""
        MODEL (
            name test.table,
            kind full,
            table_format iceberg,
            physical_properties (
              catalog = 'snowflake',
              external_volume = 'test'
            )
        );
        SELECT a::INT;
        """)
    )
    assert isinstance(model, SqlModel)
    assert model.table_format == "iceberg"

    adapter.create_table(
        table_name=model.name,
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
        table_properties=model.physical_properties,
    )

    adapter.ctas(
        table_name=model.name,
        query_or_df=model.render_query_or_raise(),
        target_columns_to_types=model.columns_to_types_or_raise,
        table_format=model.table_format,
        table_properties=model.physical_properties,
    )

    assert to_sql_calls(adapter) == [
        'CREATE ICEBERG TABLE IF NOT EXISTS "test"."table" ("a" INT) CATALOG=\'snowflake\' EXTERNAL_VOLUME=\'test\'',
        'CREATE ICEBERG TABLE IF NOT EXISTS "test"."table" CATALOG=\'snowflake\' EXTERNAL_VOLUME=\'test\' AS SELECT CAST("a" AS INT) AS "a" FROM (SELECT CAST("a" AS INT) AS "a") AS "_subquery"',
    ]


def test_clone_table_iceberg(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    mocker.patch("sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter.set_current_catalog")
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter, default_catalog="test_catalog")

    # Snowflake rejects `CREATE TABLE ... CLONE` for Iceberg tables
    adapter.clone_table("target_table", "source_table", table_format="iceberg")
    adapter.cursor.execute.assert_called_once_with(
        'CREATE ICEBERG TABLE IF NOT EXISTS "target_table" CLONE "source_table"'
    )

    # Engines that don't need format-specific DDL are unaffected
    adapter = make_mocked_engine_adapter(EngineAdapter, default_catalog="test_catalog")
    adapter.SUPPORTS_CLONING = True
    adapter.clone_table("target_table", "source_table", table_format="iceberg")
    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "target_table" CLONE "source_table"'
    )


def test_alter_table_iceberg(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    mocker.patch("sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter.set_current_catalog")
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter, default_catalog="test_catalog")

    current_table = {"a": "INT"}
    target_table = {"a": "INT", "b": "INT"}
    adapter.columns = lambda table_name, **kwargs: {
        k: exp.DataType.build(v)
        for k, v in (current_table if table_name == "test_table" else target_table).items()
    }

    alter_operations = adapter.get_alter_operations("test_table", "target_table")

    # Snowflake rejects `ALTER TABLE` for Iceberg tables
    adapter.alter_table(alter_operations, table_format="iceberg")
    assert to_sql_calls(adapter) == ['ALTER ICEBERG TABLE "test_table" ADD "b" INT']

    # Without a table format the regular `ALTER TABLE` is used
    adapter = make_mocked_engine_adapter(SnowflakeEngineAdapter, default_catalog="test_catalog")
    adapter.alter_table(alter_operations)
    assert to_sql_calls(adapter) == ['ALTER TABLE "test_table" ADD "b" INT']


def test_create_view_with_schema_and_grants(
    snowflake_mocked_engine_adapter: SnowflakeEngineAdapter,
):
    adapter = snowflake_mocked_engine_adapter

    model_v = load_sql_based_model(
        d.parse(f"""
                MODEL (
                    name test.v,
                    kind VIEW,
                    description 'normal **view** from integration test',
                    dialect 'snowflake'
                );

                select 1 as "ID", 'foo' as "NAME";
                """)
    )

    model_mv = load_sql_based_model(
        d.parse(f"""
            MODEL (
                name test.mv,
                kind VIEW (
                    materialized true
                ),
                description 'materialized **view** from integration test',
                dialect 'snowflake'
            );

            select 1 as "ID", 'foo' as "NAME";
            """)
    )

    assert isinstance(model_v.kind, ViewKind)
    assert isinstance(model_mv.kind, ViewKind)

    adapter.create_view(
        "target_view",
        model_v.render_query_or_raise(),
        model_v.columns_to_types,
        materialized=model_v.kind.materialized,
        view_properties=model_v.render_physical_properties(),
        table_description=model_v.description,
        column_descriptions=model_v.column_descriptions,
    )

    adapter.create_view(
        "target_materialized_view",
        model_mv.render_query_or_raise(),
        model_mv.columns_to_types,
        materialized=model_mv.kind.materialized,
        view_properties=model_mv.render_physical_properties(),
        table_description=model_mv.description,
        column_descriptions=model_mv.column_descriptions,
    )

    assert to_sql_calls(adapter) == [
        # normal view - COPY GRANTS goes after the column list
        """CREATE OR REPLACE VIEW "target_view" ("ID", "NAME") COPY GRANTS COMMENT='normal **view** from integration test' AS SELECT 1 AS "ID", 'foo' AS "NAME\"""",
        # materialized view - COPY GRANTS goes before the column list
        """CREATE OR REPLACE MATERIALIZED VIEW "target_materialized_view" COPY GRANTS ("ID", "NAME") COMMENT='materialized **view** from integration test' AS SELECT 1 AS "ID", 'foo' AS "NAME\"""",
    ]


def test_create_catalog(snowflake_mocked_engine_adapter: SnowflakeEngineAdapter) -> None:
    adapter = snowflake_mocked_engine_adapter
    adapter.create_catalog(exp.to_identifier("foo"))

    assert to_sql_calls(adapter) == [
        "CREATE DATABASE IF NOT EXISTS \"foo\" COMMENT='sqlmesh_managed'"
    ]


def test_drop_catalog(snowflake_mocked_engine_adapter: SnowflakeEngineAdapter) -> None:
    adapter = snowflake_mocked_engine_adapter
    adapter.drop_catalog(exp.to_identifier("foo"))

    assert to_sql_calls(adapter) == [
        """SELECT 1 FROM "INFORMATION_SCHEMA"."DATABASES" WHERE "DATABASE_NAME" = 'foo' AND "COMMENT" = 'sqlmesh_managed'""",
        'DROP DATABASE IF EXISTS "foo"',
    ]


def _make_snowflake_clustering_adapter(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    current_columns: t.Dict[str, str],
    target_columns: t.Dict[str, str],
    current_clustering_key: t.Optional[str] = None,
    target_clustering_key: t.Optional[str] = None,
) -> SnowflakeEngineAdapter:
    """Create a mocked SnowflakeEngineAdapter wired for clustering-key tests."""
    adapter = make_mocked_engine_adapter(
        SnowflakeEngineAdapter, default_catalog="catalog", patch_get_data_objects=False
    )
    mocker.patch("sqlmesh.core.engine_adapter.snowflake.SnowflakeEngineAdapter.set_current_catalog")

    adapter.columns = lambda table_name, **kwargs: {
        k: exp.DataType.build(v)
        for k, v in (
            current_columns if table_name == "catalog.db.current_table" else target_columns
        ).items()
    }

    current_obj = DataObject(
        catalog="catalog",
        schema="db",
        name="current_table",
        type=DataObjectType.TABLE,
        clustering_key=current_clustering_key,
    )
    target_obj = DataObject(
        catalog="catalog",
        schema="db",
        name="target_table",
        type=DataObjectType.TABLE,
        clustering_key=target_clustering_key,
    )

    def _mock_get_data_objects(
        schema_name: t.Any, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        results = []
        for obj in [current_obj, target_obj]:
            if object_names is None or obj.name in object_names:
                results.append(obj)
        return results

    mocker.patch.object(adapter, "_get_data_objects", side_effect=_mock_get_data_objects)
    return adapter


def _cluster_alter(action: str) -> str:
    return f'ALTER TABLE "CATALOG"."db"."current_table" {action}'


def test_clustering_key_drop_before_column_drop(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 1: Both clustered columns dropped; clustering key removed.

    Current: TENANT_ID, PARTITION_MONTH — clustered on LINEAR("TENANT_ID", "PARTITION_MONTH").
    Target: TENANT_ID only, no clustering.
    Expected order: DROP CLUSTERING KEY, then DROP COLUMN PARTITION_MONTH.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TENANT_ID": "INT", "PARTITION_MONTH": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("TENANT_ID", "PARTITION_MONTH")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 2
    assert isinstance(operations[0], TableAlterDropClusterKeyOperation)
    assert isinstance(operations[1], TableAlterDropColumnOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "PARTITION_MONTH"'),
    ]


def test_clustering_key_cluster_by_before_column_drop(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 2: Shrink clustering key from (ID, PARTITION_MONTH) to (ID); drop PARTITION_MONTH.

    Current: ID, PARTITION_MONTH — clustered on LINEAR("ID", "PARTITION_MONTH").
    Target: ID only, clustered on LINEAR("ID").
    Expected order: DROP CLUSTERING KEY, DROP COLUMN PARTITION_MONTH, CLUSTER BY ("ID").
    Setting the new key before dropping the column that was in the old key fails on
    Snowflake, so the old key must come off first.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT", "PARTITION_MONTH": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key='LINEAR("ID", "PARTITION_MONTH")',
        target_clustering_key='LINEAR("ID")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 3
    assert isinstance(operations[0], TableAlterDropClusterKeyOperation)
    assert isinstance(operations[1], TableAlterDropColumnOperation)
    assert isinstance(operations[2], TableAlterChangeClusterKeyOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "PARTITION_MONTH"'),
        _cluster_alter('CLUSTER BY ("ID")'),
    ]


def test_clustering_key_unchanged_when_dropped_column_not_in_key(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 3: Dropped column is not referenced by the clustering key.

    Current: TENANT_ID, EXTRA — clustered on LINEAR("TENANT_ID").
    Target: TENANT_ID, clustered on LINEAR("TENANT_ID").
    Expected: only DROP COLUMN EXTRA; no clustering statements emitted.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TENANT_ID": "INT", "EXTRA": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("TENANT_ID")',
        target_clustering_key='LINEAR("TENANT_ID")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")

    assert len(operations) == 1
    assert isinstance(operations[0], TableAlterDropColumnOperation)
    assert not any(
        isinstance(op, (TableAlterChangeClusterKeyOperation, TableAlterDropClusterKeyOperation))
        for op in operations
    )


def test_clustering_key_add_column_before_cluster_by(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 4: New clustering key references a column being added.

    Current: ID, not clustered.
    Target: ID, C — clustered on LINEAR("C").
    Expected order: ADD COLUMN C, then CLUSTER BY ("C").
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT"},
        target_columns={"ID": "INT", "C": "INT"},
        current_clustering_key=None,
        target_clustering_key='LINEAR("C")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 2
    assert isinstance(operations[0], TableAlterAddColumnOperation)
    assert isinstance(operations[1], TableAlterChangeClusterKeyOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter('ADD "C" INT'),
        _cluster_alter('CLUSTER BY ("C")'),
    ]


def test_clustering_key_drop_column_add_column_change_key(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 4b: Drop b, add c, and change the clustering key from b to c.

    Current: A, B — clustered on LINEAR("B").
    Target: A, C — clustered on LINEAR("C").
    Expected order: DROP CLUSTERING KEY, DROP COLUMN B, ADD COLUMN C, CLUSTER BY ("C").
    Snowflake rejects DROP COLUMN while the column is still in the key, and also
    rejects CLUSTER BY on a column that has not yet been added.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"A": "INT", "B": "INT"},
        target_columns={"A": "INT", "C": "INT"},
        current_clustering_key='LINEAR("B")',
        target_clustering_key='LINEAR("C")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 4
    assert isinstance(operations[0], TableAlterDropClusterKeyOperation)
    assert isinstance(operations[1], TableAlterDropColumnOperation)
    assert isinstance(operations[2], TableAlterAddColumnOperation)
    assert isinstance(operations[3], TableAlterChangeClusterKeyOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "B"'),
        _cluster_alter('ADD "C" INT'),
        _cluster_alter('CLUSTER BY ("C")'),
    ]


def test_clustering_key_drop_only_when_same_columns(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 5: No column changes, clustering key removed.

    Current: ID, C — clustered on LINEAR("ID").
    Target: ID, C — not clustered.
    Expected: exactly one DROP CLUSTERING KEY, nothing else.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT", "C": "INT"},
        target_columns={"ID": "INT", "C": "INT"},
        current_clustering_key='LINEAR("ID")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 1
    assert isinstance(operations[0], TableAlterDropClusterKeyOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [_cluster_alter("DROP CLUSTERING KEY")]


def test_clustering_key_inserted_between_multiple_drops(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Case 6: Multiple columns dropped; only one belongs to the clustering key.

    Current: EXTRA, PARTITION_MONTH, TENANT_ID — clustered on LINEAR("PARTITION_MONTH").
    Target: TENANT_ID, not clustered.
    Schema diff proposes: DROP EXTRA, DROP PARTITION_MONTH.
    Expected order: DROP COLUMN EXTRA, DROP CLUSTERING KEY, DROP COLUMN PARTITION_MONTH.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"EXTRA": "INT", "PARTITION_MONTH": "INT", "TENANT_ID": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("PARTITION_MONTH")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 3
    assert isinstance(operations[0], TableAlterDropColumnOperation)
    assert isinstance(operations[1], TableAlterDropClusterKeyOperation)
    assert isinstance(operations[2], TableAlterDropColumnOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter('DROP COLUMN "EXTRA"'),
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "PARTITION_MONTH"'),
    ]


def test_clustering_key_cluster_by_change_inserted_between_drops(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Clustering key changes (not just dropped) with multiple column drops.

    Current: EXTRA, PARTITION_MONTH, ID — clustered on LINEAR("PARTITION_MONTH").
    Target: ID, clustered on LINEAR("ID").
    Expected: DROP COLUMN EXTRA, DROP CLUSTERING KEY, DROP COLUMN PARTITION_MONTH, CLUSTER BY ("ID").
    The old key comes off before the drop of the column it names; the new key is set last.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"EXTRA": "INT", "PARTITION_MONTH": "INT", "ID": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key='LINEAR("PARTITION_MONTH")',
        target_clustering_key='LINEAR("ID")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 4
    assert isinstance(operations[0], TableAlterDropColumnOperation)
    assert operations[0].column_parts[0].name == "EXTRA"
    assert isinstance(operations[1], TableAlterDropClusterKeyOperation)
    assert isinstance(operations[2], TableAlterDropColumnOperation)
    assert isinstance(operations[3], TableAlterChangeClusterKeyOperation)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter('DROP COLUMN "EXTRA"'),
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "PARTITION_MONTH"'),
        _cluster_alter('CLUSTER BY ("ID")'),
    ]


def test_clustering_key_reorder_targets_current_table(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """All operations must target the current (source) table, not the target table."""
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TENANT_ID": "INT", "PARTITION_MONTH": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("TENANT_ID", "PARTITION_MONTH")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")

    for op in operations:
        assert "current_table" in op.target_table.sql(dialect="snowflake").lower()


def test_clustering_key_normalization_quoted_upper_matches_upper_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """LINEAR("PARTITION_MONTH") references the column string PARTITION_MONTH.

    Both the quoted key identifier and the column name normalize to PARTITION_MONTH
    in Snowflake, so the reorder must fire.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TENANT_ID": "INT", "PARTITION_MONTH": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("PARTITION_MONTH")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")

    assert isinstance(operations[0], TableAlterDropClusterKeyOperation)
    assert isinstance(operations[1], TableAlterDropColumnOperation)


def test_clustering_key_quoted_upper_does_not_match_stored_lower_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """LINEAR("PARTITION_MONTH") does not reference a column stored as partition_month.

    DESCRIBE returns the stored name. A lowercase name was created with quotes, so it
    is a different column from PARTITION_MONTH and the key stays in place.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TENANT_ID": "INT", "partition_month": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("PARTITION_MONTH")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert len(operations) == 2
    assert isinstance(operations[0], TableAlterDropColumnOperation)
    assert isinstance(operations[1], TableAlterDropClusterKeyOperation)
    assert to_sql_calls(adapter) == [
        _cluster_alter('DROP COLUMN "partition_month"'),
        _cluster_alter("DROP CLUSTERING KEY"),
    ]


def test_clustering_key_normalization_quoted_lower_no_match_upper_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """LINEAR("partition_month") does NOT reference the column PARTITION_MONTH.

    The quoted-lowercase identifier stays lowercase, while the column string
    PARTITION_MONTH normalizes to PARTITION_MONTH.  No match → no reorder.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TENANT_ID": "INT", "PARTITION_MONTH": "INT"},
        target_columns={"TENANT_ID": "INT"},
        current_clustering_key='LINEAR("partition_month")',
        target_clustering_key='LINEAR("partition_month")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")

    assert len(operations) == 1
    assert isinstance(operations[0], TableAlterDropColumnOperation)
    assert not any(
        isinstance(op, (TableAlterChangeClusterKeyOperation, TableAlterDropClusterKeyOperation))
        for op in operations
    )


def test_clustering_key_quoted_lower_matches_stored_lower_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """LINEAR("b") references the column stored as b.

    The stored name is already case-resolved, so it matches the quoted key and the
    key comes off before the drop.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"id": "INT", "b": "INT"},
        target_columns={"id": "INT"},
        current_clustering_key='LINEAR("id", "b")',
        target_clustering_key='LINEAR("id")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert [type(op) for op in operations] == [
        TableAlterDropClusterKeyOperation,
        TableAlterDropColumnOperation,
        TableAlterChangeClusterKeyOperation,
    ]
    assert to_sql_calls(adapter) == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "b"'),
        _cluster_alter('CLUSTER BY ("id")'),
    ]


def test_clustering_key_expression_column_dropped(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """A column inside a key expression is still part of the key.

    Current: TS, ID clustered on LINEAR(TO_DATE("TS"), "ID").
    Target: ID clustered on LINEAR("ID").
    Expected: DROP CLUSTERING KEY, DROP COLUMN TS, CLUSTER BY ("ID").
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TS": "DATE", "ID": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key='LINEAR(TO_DATE("TS"), "ID")',
        target_clustering_key='LINEAR("ID")',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert to_sql_calls(adapter) == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "TS"'),
        _cluster_alter('CLUSTER BY ("ID")'),
    ]


def test_clustering_key_expression_keeps_expression_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Inverse of dropping the column inside the key expression.

    Current: TS DATE, ID INT clustered on LINEAR(TO_DATE("TS"), "ID").
    Target: TS DATE only, clustered on LINEAR(TO_DATE("TS")).
    The plain column ID is dropped. TS stays, because the remaining key still uses it.
    Expected: DROP CLUSTERING KEY, DROP COLUMN ID, CLUSTER BY (TO_DATE("TS")).
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"TS": "DATE", "ID": "INT"},
        target_columns={"TS": "DATE"},
        current_clustering_key='LINEAR(TO_DATE("TS"), "ID")',
        target_clustering_key='LINEAR(TO_DATE("TS"))',
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "ID"'),
        _cluster_alter('CLUSTER BY (TO_DATE("TS"))'),
    ]
    assert not any('DROP COLUMN "TS"' in sql for sql in sql_calls)


def test_clustering_key_substring_expression_column_dropped(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """SUBSTRING(NAME, 1, 3) still names NAME, so NAME cannot be dropped first."""
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"NAME": "VARCHAR", "ID": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key="LINEAR(SUBSTRING(NAME, 1, 3), ID)",
        target_clustering_key="LINEAR(ID)",
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert to_sql_calls(adapter) == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "NAME"'),
        _cluster_alter('CLUSTER BY ("ID")'),
    ]


def test_clustering_key_type_change_on_key_column(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """VARCHAR to INT on a key column is a drop and an add, and the key stays the same.

    Snowflake still rejects the drop while the column is in the key, so the key comes
    off first and is set again after the new column exists.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"A": "INT", "B": "VARCHAR"},
        target_columns={"A": "INT", "B": "INT"},
        current_clustering_key="LINEAR(B)",
        target_clustering_key="LINEAR(B)",
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    assert to_sql_calls(adapter) == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "B"'),
        _cluster_alter('ADD "B" INT'),
        _cluster_alter('CLUSTER BY ("B")'),
    ]


def test_clustering_key_issue_5813_clone_inherited_column_drop(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Issue 5813: a cloned FULL snapshot still has the source clustering key.

    The issue's model is ANALYTICS.SESSION_FACTS. The deployed query projects
    tenant_id, user_id, session_start_ts, and date_trunc('month', session_start_ts)
    AS partition_month, with clustered_by (TENANT_ID, PARTITION_MONTH). A later edit
    removes clustered_by and stops projecting partition_month.

    SQLMesh creates the new physical table with CREATE TABLE ... CLONE. Snowflake
    copies the clustering key onto that clone. clone_table only emits the CLONE
    statement, so this test is the alter that follows: the current table is the
    clone and still has the inherited key. The issue does not state column types.
    TENANT_ID and USER_ID are INT here; SESSION_START_TS is TIMESTAMP and
    PARTITION_MONTH is DATE because the issue derives the latter with date_trunc.

    Snowflake rejects DROP COLUMN PARTITION_MONTH while it is still in the key
    (error 003203). Expected: DROP CLUSTERING KEY, then DROP COLUMN PARTITION_MONTH.
    The columns the model still projects are not dropped, and no CLUSTER BY is
    emitted because the target has no clustering key.
    """
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={
            "TENANT_ID": "INT",
            "USER_ID": "INT",
            "SESSION_START_TS": "TIMESTAMP",
            "PARTITION_MONTH": "DATE",
        },
        target_columns={
            "TENANT_ID": "INT",
            "USER_ID": "INT",
            "SESSION_START_TS": "TIMESTAMP",
        },
        current_clustering_key='LINEAR("TENANT_ID", "PARTITION_MONTH")',
        target_clustering_key=None,
    )

    operations = adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
    adapter.alter_table(operations)

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        _cluster_alter("DROP CLUSTERING KEY"),
        _cluster_alter('DROP COLUMN "PARTITION_MONTH"'),
    ]
    for kept in ('"TENANT_ID"', '"USER_ID"', '"SESSION_START_TS"'):
        assert not any(f"DROP COLUMN {kept}" in sql for sql in sql_calls)


def test_clustering_key_unparseable_without_column_drop_skips_parse(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """A key string that cannot be parsed is ignored unless a column is dropped.

    LINEAR( is not valid Snowflake SQL. With no column drop, the current key is not
    parsed: an unchanged key emits nothing, and a removed or replaced key is appended.
    """
    unchanged = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key="LINEAR(",
        target_clustering_key="LINEAR(",
    )
    assert (
        unchanged.get_alter_operations("catalog.db.current_table", "catalog.db.target_table") == []
    )

    removed = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key="LINEAR(",
        target_clustering_key=None,
    )
    removed_operations = removed.get_alter_operations(
        "catalog.db.current_table", "catalog.db.target_table"
    )
    removed.alter_table(removed_operations)
    assert [type(op) for op in removed_operations] == [TableAlterDropClusterKeyOperation]
    assert to_sql_calls(removed) == [_cluster_alter("DROP CLUSTERING KEY")]

    replaced = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key="LINEAR(",
        target_clustering_key='LINEAR("ID")',
    )
    replaced_operations = replaced.get_alter_operations(
        "catalog.db.current_table", "catalog.db.target_table"
    )
    replaced.alter_table(replaced_operations)
    assert [type(op) for op in replaced_operations] == [TableAlterChangeClusterKeyOperation]
    assert to_sql_calls(replaced) == [_cluster_alter('CLUSTER BY ("ID")')]


def test_clustering_key_unparseable_column_drop_still_raises(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
) -> None:
    """Dropping a column still parses the current key, so a bad key still fails."""
    adapter = _make_snowflake_clustering_adapter(
        make_mocked_engine_adapter,
        mocker,
        current_columns={"ID": "INT", "EXTRA": "INT"},
        target_columns={"ID": "INT"},
        current_clustering_key="LINEAR(",
        target_clustering_key=None,
    )

    with pytest.raises(ParseError):
        adapter.get_alter_operations("catalog.db.current_table", "catalog.db.target_table")
