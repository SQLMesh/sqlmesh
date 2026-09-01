# type: ignore
import sys
import typing as t

import pytest

# Skip entire module if Python < 3.10 BEFORE any DB2 imports
# DB2 adapter requires Python 3.10+ for db2-sqlglot-dialect dependency
if sys.version_info < (3, 10):
    pytest.skip(
        "DB2 adapter requires Python 3.10+ for db2-sqlglot-dialect", allow_module_level=True
    )

from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one

from sqlmesh.core.engine_adapter.db2 import Db2EngineAdapter
from sqlmesh.core.engine_adapter.shared import CatalogSupport
from tests.core.engine_adapter import to_sql_calls

# Mark all tests in this file
pytestmark = [
    pytest.mark.engine,
    pytest.mark.db2,
]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable) -> Db2EngineAdapter:
    return make_mocked_engine_adapter(Db2EngineAdapter)


# ---------------------------------------------------------------------------
# columns() — reads SYSCAT.COLUMNS, maps Db2 catalog types to sqlglot types
# ---------------------------------------------------------------------------


def test_columns(adapter: Db2EngineAdapter):
    """columns() must map every Db2 catalog type correctly and return names as-is."""
    adapter.cursor.fetchall.return_value = [
        ("id", "INTEGER", 4, 0),
        ("name", "VARCHAR", 100, 0),
        ("amount", "DECIMAL", 10, 2),
        ("created_at", "TIMESTAMP", 10, 6),
        ("data", "CLOB", 1048576, 0),
        ("binary_data", "BLOB", 1048576, 0),
        ("flag", "SMALLINT", 2, 0),
        ("big_num", "BIGINT", 8, 0),
        ("price", "DOUBLE", 8, 0),
        ("code", "CHAR", 10, 0),
    ]

    result = adapter.columns("test_schema.test_table")

    # Keys must be returned exactly as stored in SYSCAT.COLUMNS — no uppercasing.
    # CREATE TABLE stores them as case-sensitive lowercase when quote_identifiers=True.
    # Uppercasing would cause the schema differ to fire spurious ALTER TABLE every plan.
    assert list(result.keys()) == [
        "id",
        "name",
        "amount",
        "created_at",
        "data",
        "binary_data",
        "flag",
        "big_num",
        "price",
        "code",
    ]
    assert result == {
        "id": exp.DataType.build("INT", dialect=adapter.dialect),
        "name": exp.DataType.build("VARCHAR(100)", dialect=adapter.dialect),
        "amount": exp.DataType.build("DECIMAL(10,2)", dialect=adapter.dialect),
        "created_at": exp.DataType.build("TIMESTAMP", dialect=adapter.dialect),
        "data": exp.DataType.build("CLOB", dialect=adapter.dialect),
        "binary_data": exp.DataType.build("BLOB", dialect=adapter.dialect),
        "flag": exp.DataType.build("SMALLINT", dialect=adapter.dialect),
        "big_num": exp.DataType.build("BIGINT", dialect=adapter.dialect),
        "price": exp.DataType.build("DOUBLE", dialect=adapter.dialect),
        "code": exp.DataType.build("CHAR(10)", dialect=adapter.dialect),
    }


# ---------------------------------------------------------------------------
# _db2_type_to_sqlglot — Db2-specific type mappings
# ---------------------------------------------------------------------------


def test_type_mapping_comprehensive(adapter: Db2EngineAdapter):
    """Db2-specific catalog types must map to the correct sqlglot/Db2 SQL types."""
    cases = [
        # (db2_catalog_type, length, scale, expected_db2_sql)
        ("DECFLOAT", 16, 0, "DOUBLE"),
        ("GRAPHIC", 50, 0, "CHAR(50)"),
        ("VARGRAPHIC", 100, 0, "VARCHAR(100)"),
        ("DBCLOB", 1048576, 0, "CLOB"),
        # XML maps to sqlglot TEXT internally; the Db2 dialect renders TEXT as CLOB
        # (Db2 has no TEXT type — CLOB is the correct unlimited-text equivalent).
        ("XML", 0, 0, "CLOB"),
        ("ROWID", 40, 0, "VARCHAR(40)"),
        ("BOOLEAN", 1, 0, "BOOLEAN"),
    ]
    for db2_type, length, scale, expected in cases:
        result = adapter._db2_type_to_sqlglot(db2_type, length, scale)
        assert result.sql(dialect="db2") == expected, (
            f"{db2_type}: expected {expected!r}, got {result.sql(dialect='db2')!r}"
        )


# ---------------------------------------------------------------------------
# table_exists — queries SYSCAT.TABLES with UPPER() for case-insensitive match
# ---------------------------------------------------------------------------


def test_table_exists_found(adapter: Db2EngineAdapter):
    """table_exists returns True and queries SYSCAT.TABLES with UPPER() wrapping.
    TYPE column is now selected so the cache stores the correct DataObjectType.
    """
    adapter.cursor.fetchone.return_value = ("TEST_SCHEMA", "TEST_TABLE", "T")

    assert adapter.table_exists("test_schema.test_table") is True

    # Exact SQL: identifiers are quoted by quote_identifiers=True in execute().
    # SYSCAT.TABLES is a catalog reference so it renders as "SYSCAT"."TABLES".
    assert to_sql_calls(adapter) == [
        'SELECT "TABSCHEMA", "TABNAME", "TYPE" FROM "SYSCAT"."TABLES" '
        "WHERE UPPER(\"TABSCHEMA\") = 'TEST_SCHEMA' AND UPPER(\"TABNAME\") = 'TEST_TABLE'"
    ]


def test_table_exists_not_found(adapter: Db2EngineAdapter):
    """table_exists returns False when SYSCAT.TABLES has no matching row."""
    adapter.cursor.fetchone.return_value = None

    assert adapter.table_exists("test_schema.nonexistent_table") is False


# ---------------------------------------------------------------------------
# create_index — guards via SYSCAT.INDEXES (no IF NOT EXISTS in Db2)
# ---------------------------------------------------------------------------


def test_create_index(adapter: Db2EngineAdapter):
    """create_index checks SYSCAT.INDEXES then issues CREATE INDEX without IF NOT EXISTS."""
    # None = index does not exist → adapter proceeds to CREATE INDEX.
    # A tuple (0,) would be truthy and incorrectly cause the adapter to skip creation.
    adapter.cursor.fetchone.return_value = None

    adapter.create_index("test_schema.test_table", "idx_test", ("col1", "col2"))

    assert to_sql_calls(adapter) == [
        'SELECT "INDNAME" FROM "SYSCAT"."INDEXES" '
        "WHERE UPPER(\"TABSCHEMA\") = 'TEST_SCHEMA' AND UPPER(\"TABNAME\") = 'TEST_TABLE' "
        "AND UPPER(\"INDNAME\") = 'IDX_TEST'",
        'CREATE INDEX "idx_test" ON "test_schema"."test_table"("col1", "col2")',
    ]


def test_create_index_already_exists(adapter: Db2EngineAdapter):
    """create_index skips CREATE INDEX when SYSCAT.INDEXES finds an existing entry."""
    adapter.cursor.fetchone.return_value = ("IDX_TEST",)  # index found

    adapter.create_index("test_schema.test_table", "idx_test", ("col1",))

    sql_calls = to_sql_calls(adapter)
    # Only the existence check — no CREATE INDEX
    assert len(sql_calls) == 1
    assert '"SYSCAT"."INDEXES"' in sql_calls[0]
    assert "CREATE INDEX" not in sql_calls[0]


# ---------------------------------------------------------------------------
# create_table — PK columns need NOT NULL (Db2 requires it, SQL0542N otherwise)
# ---------------------------------------------------------------------------


def test_create_table_primary_key_not_null(adapter: Db2EngineAdapter):
    """_build_schema_exp injects NOT NULL on every primary key column."""
    # fetchone=None → table_exists returns False → proceeds to CREATE TABLE.
    # Fully-qualified name avoids _get_current_schema() being called on mock cursor.
    adapter.cursor.fetchone.return_value = None

    adapter.create_table(
        "test_schema.test_table",
        {"id": exp.DataType.build("INT"), "name": exp.DataType.build("VARCHAR(100)")},
        primary_key=("id",),
    )

    # The Db2 dialect renders INT as INTEGER.  NOT NULL is required on PK columns —
    # omitting it would cause Db2 to raise SQL0542N at CREATE TABLE time.
    assert to_sql_calls(adapter) == [
        # table_exists check
        'SELECT "TABSCHEMA", "TABNAME", "TYPE" FROM "SYSCAT"."TABLES" '
        "WHERE UPPER(\"TABSCHEMA\") = 'TEST_SCHEMA' AND UPPER(\"TABNAME\") = 'TEST_TABLE'",
        # CREATE TABLE
        'CREATE TABLE "test_schema"."test_table" '
        '("id" INTEGER NOT NULL, "name" VARCHAR(100), PRIMARY KEY ("id"))',
    ]


# ---------------------------------------------------------------------------
# CTAS — Db2 requires AS (SELECT ...) WITH DATA; base class omits both
# ---------------------------------------------------------------------------


def test_ctas_with_data(adapter: Db2EngineAdapter, mocker: MockerFixture):
    """_create_table appends (…) WITH DATA to CTAS SQL for Db2."""
    mocker.patch.object(adapter, "table_exists", return_value=False)
    mocker.patch.object(adapter, "drop_view")

    adapter.ctas(
        table_name="test_table",
        query_or_df=parse_one("SELECT id, name FROM source_table"),
        exists=False,
    )

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 1
    assert sql_calls[0].startswith("CREATE TABLE")
    assert "WITH DATA" in sql_calls[0]
    # _subquery alias injected by base class must be stripped (Db2 rejects it)
    assert "_subquery" not in sql_calls[0]


# ---------------------------------------------------------------------------
# drop_view — guards via SYSCAT.VIEWS (no DROP VIEW IF EXISTS in Db2)
# ---------------------------------------------------------------------------


def test_drop_view_not_found(adapter: Db2EngineAdapter):
    """drop_view returns early without DROP VIEW when SYSCAT.VIEWS has no match."""
    adapter.cursor.fetchone.return_value = None

    adapter.drop_view("test_schema.myview", ignore_if_not_exists=True)

    assert to_sql_calls(adapter) == [
        'SELECT 1 FROM "SYSCAT"."VIEWS" '
        "WHERE UPPER(\"VIEWSCHEMA\") = 'TEST_SCHEMA' AND UPPER(\"VIEWNAME\") = 'MYVIEW'"
    ]


def test_drop_view_exists(adapter: Db2EngineAdapter):
    """drop_view issues DROP VIEW when SYSCAT.VIEWS confirms existence."""
    adapter.cursor.fetchone.return_value = (1,)  # view found

    adapter.drop_view("test_schema.myview")

    assert to_sql_calls(adapter) == [
        'SELECT 1 FROM "SYSCAT"."VIEWS" '
        "WHERE UPPER(\"VIEWSCHEMA\") = 'TEST_SCHEMA' AND UPPER(\"VIEWNAME\") = 'MYVIEW'",
        'DROP VIEW "test_schema"."myview"',
    ]


# ---------------------------------------------------------------------------
# create_schema — guards via SYSCAT.SCHEMATA (no IF NOT EXISTS in Db2)
# ---------------------------------------------------------------------------


def test_create_schema(adapter: Db2EngineAdapter):
    """create_schema checks SYSCAT.SCHEMATA then issues CREATE SCHEMA."""
    adapter.cursor.fetchone.return_value = None  # schema does not exist

    adapter.create_schema("test_schema", ignore_if_exists=True)

    assert to_sql_calls(adapter) == [
        'SELECT 1 FROM "SYSCAT"."SCHEMATA" WHERE UPPER("SCHEMANAME") = \'TEST_SCHEMA\'',
        'CREATE SCHEMA "test_schema"',
    ]


def test_create_schema_already_exists(adapter: Db2EngineAdapter):
    """create_schema returns early without CREATE SCHEMA when schema already exists."""
    adapter.cursor.fetchone.return_value = (1,)  # schema found

    adapter.create_schema("test_schema", ignore_if_exists=True)

    sql_calls = to_sql_calls(adapter)
    # Only the existence check — no CREATE SCHEMA
    assert len(sql_calls) == 1
    assert '"SYSCAT"."SCHEMATA"' in sql_calls[0]
    assert "CREATE SCHEMA" not in sql_calls[0]


# ---------------------------------------------------------------------------
# drop_schema — Db2 only supports RESTRICT; cascade drops objects manually
# ---------------------------------------------------------------------------


def test_drop_schema_cascade(adapter: Db2EngineAdapter):
    """drop_schema with cascade=True drops views then tables then issues DROP SCHEMA RESTRICT."""
    adapter.cursor.fetchone.return_value = (1,)  # schema exists
    adapter.cursor.fetchall.return_value = [("TBL1",)]  # one object in schema

    adapter.drop_schema("TEST_SCHEMA", cascade=True)

    assert to_sql_calls(adapter) == [
        # existence check
        'SELECT 1 FROM "SYSCAT"."SCHEMATA" WHERE "SCHEMANAME" = \'TEST_SCHEMA\'',
        # list views
        'SELECT "TABNAME" FROM "SYSCAT"."TABLES" '
        "WHERE \"TABSCHEMA\" = 'TEST_SCHEMA' AND \"TYPE\" = 'V'",
        # drop the view
        'DROP VIEW "TEST_SCHEMA"."TBL1"',
        # list tables
        'SELECT "TABNAME" FROM "SYSCAT"."TABLES" '
        "WHERE \"TABSCHEMA\" = 'TEST_SCHEMA' AND \"TYPE\" = 'T'",
        # drop the table
        'DROP TABLE "TEST_SCHEMA"."TBL1"',
        # RESTRICT is raw SQL because sqlglot does not emit it for schemas
        "DROP SCHEMA TEST_SCHEMA RESTRICT",
    ]


def test_drop_schema_not_found(adapter: Db2EngineAdapter):
    """drop_schema returns early without DROP when schema does not exist."""
    adapter.cursor.fetchone.return_value = None

    adapter.drop_schema("nonexistent_schema", ignore_if_not_exists=True)

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 1
    assert '"SYSCAT"."SCHEMATA"' in sql_calls[0]
    assert "DROP" not in sql_calls[0]


# ---------------------------------------------------------------------------
# create_view — replace=True emits CREATE OR REPLACE VIEW
# ---------------------------------------------------------------------------


def test_create_view_replace(adapter: Db2EngineAdapter, mocker: MockerFixture):
    """create_view with replace=True emits CREATE OR REPLACE VIEW."""
    # get_data_object returns None → no type-mismatch drop needed
    mocker.patch.object(adapter, "get_data_object", return_value=None)

    adapter.create_view("test_view", parse_one("SELECT * FROM test_table"), replace=True)

    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE VIEW "test_view" AS SELECT * FROM "test_table"'
    ]


# ---------------------------------------------------------------------------
# _merge — replaces __MERGE_TARGET__ / __MERGE_SOURCE__ with TARGET / SOURCE
# ---------------------------------------------------------------------------


def test_merge_alias_replacement(adapter: Db2EngineAdapter):
    """_merge replaces double-underscore aliases rejected by Db2 with TARGET/SOURCE."""
    adapter.merge(
        target_table="target_table",
        source_table=parse_one("SELECT id, value FROM source_table"),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "value": exp.DataType.build("VARCHAR(100)"),
        },
        unique_key=[exp.to_identifier("id", quoted=True)],
    )

    assert to_sql_calls(adapter) == [
        'MERGE INTO "target_table" AS "TARGET" '
        'USING (SELECT "id", "value" FROM "source_table") AS "SOURCE" '
        'ON "TARGET"."id" = "SOURCE"."id" '
        'WHEN MATCHED THEN UPDATE SET "TARGET"."id" = "SOURCE"."id", "TARGET"."value" = "SOURCE"."value" '
        'WHEN NOT MATCHED THEN INSERT ("id", "value") VALUES ("SOURCE"."id", "SOURCE"."value")'
    ]


# ---------------------------------------------------------------------------
# get_current_catalog — reads CURRENT SERVER via SYSIBM.SYSDUMMY1
# ---------------------------------------------------------------------------


def test_get_current_catalog(adapter: Db2EngineAdapter):
    """get_current_catalog reads CURRENT SERVER from SYSIBM.SYSDUMMY1 and returns uppercase."""
    adapter.cursor.fetchone.return_value = ("TESTDB",)

    result = adapter.get_current_catalog()

    assert result == "TESTDB"
    # Raw string because fetchone is called with a plain string, not an exp.Expr
    assert to_sql_calls(adapter) == ["SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1"]


# ---------------------------------------------------------------------------
# _get_current_schema — reads CURRENT SCHEMA, falls back to CURRENT USER
# ---------------------------------------------------------------------------


def test_get_current_schema(adapter: Db2EngineAdapter):
    """_get_current_schema reads CURRENT SCHEMA and returns it lowercased."""
    adapter.cursor.fetchone.return_value = ("TESTSCHEMA",)

    result = adapter._get_current_schema()

    assert result == "testschema"
    assert to_sql_calls(adapter) == ["SELECT CURRENT SCHEMA FROM SYSIBM.SYSDUMMY1"]


# ---------------------------------------------------------------------------
# server_version — parses SERVICE_LEVEL from SYSIBMADM.ENV_INST_INFO
# ---------------------------------------------------------------------------


def test_server_version(adapter: Db2EngineAdapter, mocker: MockerFixture):
    """server_version parses the Db2 version string into a (major, minor) tuple."""
    fetchone_mock = mocker.patch.object(adapter, "fetchone")

    fetchone_mock.return_value = ("Db2 v11.5.0.0",)
    assert adapter.server_version == (11, 5)

    del adapter.server_version
    fetchone_mock.return_value = ("Db2 v12.1.0.0",)
    assert adapter.server_version == (12, 1)


# ---------------------------------------------------------------------------
# catalog_support — Db2 is a single-catalog engine
# ---------------------------------------------------------------------------


def test_catalog_support(adapter: Db2EngineAdapter):
    """Db2 exposes only one catalog (the database itself)."""
    assert adapter.catalog_support == CatalogSupport.SINGLE_CATALOG_ONLY


# ---------------------------------------------------------------------------
# comments — COMMENT_CREATION_TABLE = COMMENT_COMMAND_ONLY (no inline comments)
# ---------------------------------------------------------------------------


def test_comments_on_table(adapter: Db2EngineAdapter):
    """Db2 issues separate COMMENT ON TABLE/COLUMN statements, not inline DDL comments."""
    adapter.cursor.fetchone.return_value = None  # table does not exist

    adapter.create_table(
        "test_schema.test_table",
        {"id": exp.DataType.build("INT"), "name": exp.DataType.build("VARCHAR(100)")},
        table_description="Test table",
        column_descriptions={"id": "Primary key", "name": "User name"},
    )

    assert to_sql_calls(adapter) == [
        'SELECT "TABSCHEMA", "TABNAME", "TYPE" FROM "SYSCAT"."TABLES" '
        "WHERE UPPER(\"TABSCHEMA\") = 'TEST_SCHEMA' AND UPPER(\"TABNAME\") = 'TEST_TABLE'",
        'CREATE TABLE "test_schema"."test_table" ("id" INTEGER, "name" VARCHAR(100))',
        'COMMENT ON TABLE "test_schema"."test_table" IS \'Test table\'',
        'COMMENT ON COLUMN "test_schema"."test_table"."id" IS \'Primary key\'',
        'COMMENT ON COLUMN "test_schema"."test_table"."name" IS \'User name\'',
    ]


# ---------------------------------------------------------------------------
# _create_table_like — always passes exists=False (no IF NOT EXISTS pre-11.5.8)
# ---------------------------------------------------------------------------


def test_create_table_like(adapter: Db2EngineAdapter):
    """_create_table_like emits CREATE TABLE … (LIKE …) without IF NOT EXISTS."""
    adapter._create_table_like(
        target_table_name="target_table",
        source_table_name="source_table",
        exists=True,  # adapter must ignore this and always pass exists=False
    )

    assert to_sql_calls(adapter) == ['CREATE TABLE "target_table" (LIKE "source_table")']
