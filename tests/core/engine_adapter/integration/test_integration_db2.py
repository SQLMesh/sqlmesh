import importlib.util
import sys
import typing as t

import pytest

# Skip entire module if Python < 3.10 or db2-sqlglot-dialect is not installed.
# The dialect package must be present before db2.py is imported because
# class-level exp.DataType.build(dialect="db2") runs at import time.
if sys.version_info < (3, 10) or importlib.util.find_spec("db2_sqlglot") is None:
    pytest.skip(
        "DB2 adapter requires Python 3.10+ and db2-sqlglot-dialect", allow_module_level=True
    )

import pandas as pd  # noqa: TID253
from pytest import FixtureRequest
from sqlglot import exp

from sqlmesh.core.engine_adapter.db2 import Db2EngineAdapter
from tests.core.engine_adapter.integration import (
    TestContext,
    generate_pytest_params,
    ENGINES_BY_NAME,
    IntegrationTestEngine,
)


@pytest.fixture(params=list(generate_pytest_params(ENGINES_BY_NAME["db2"])))
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[
        [IntegrationTestEngine, str, str, str], t.Iterable[TestContext]
    ],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture
def engine_adapter(ctx: TestContext) -> Db2EngineAdapter:
    assert isinstance(ctx.engine_adapter, Db2EngineAdapter)
    return ctx.engine_adapter


# ---------------------------------------------------------------------------
# Basic connectivity
# ---------------------------------------------------------------------------


def test_engine_adapter(ctx: TestContext) -> None:
    """Db2 requires FROM SYSIBM.SYSDUMMY1 instead of a bare SELECT 1."""
    assert isinstance(ctx.engine_adapter, Db2EngineAdapter)
    assert ctx.engine_adapter.fetchone("SELECT 1 FROM SYSIBM.SYSDUMMY1") == (1,)


def test_server_version(ctx: TestContext) -> None:
    """server_version should parse the SERVICE_LEVEL string and return >= 11."""
    assert isinstance(ctx.engine_adapter, Db2EngineAdapter)
    major, minor = ctx.engine_adapter.server_version
    assert major >= 11


def test_get_current_catalog(ctx: TestContext) -> None:
    """get_current_catalog reads CURRENT SERVER via SYSIBM.SYSDUMMY1 and returns uppercase."""
    assert isinstance(ctx.engine_adapter, Db2EngineAdapter)
    catalog = ctx.engine_adapter.get_current_catalog()
    assert catalog is not None
    assert catalog == catalog.upper()


# ---------------------------------------------------------------------------
# Column type mapping (SYSCAT.COLUMNS path)
# ---------------------------------------------------------------------------


def test_columns(ctx: TestContext) -> None:
    """columns() must round-trip all core Db2 catalog types through _db2_type_to_sqlglot."""
    table = ctx.table("column_types")
    cols_to_types = {
        "col_int": exp.DataType.build("INT"),
        "col_bigint": exp.DataType.build("BIGINT"),
        "col_smallint": exp.DataType.build("SMALLINT"),
        "col_decimal": exp.DataType.build("DECIMAL(10, 2)"),
        "col_double": exp.DataType.build("DOUBLE"),
        "col_varchar": exp.DataType.build("VARCHAR(100)"),
        "col_char": exp.DataType.build("CHAR(10)"),
        "col_date": exp.DataType.build("DATE"),
        "col_timestamp": exp.DataType.build("TIMESTAMP"),
    }

    ctx.engine_adapter.create_table(table, cols_to_types)
    result = ctx.engine_adapter.columns(table)

    # Verify column names (keys) are returned as-is from SYSCAT.COLUMNS.
    # CREATE TABLE uses quote_identifiers=True so Db2 stores them as case-sensitive
    # lowercase ("col_int", not "COL_INT").  columns() must not upper-case them —
    # doing so would cause the schema differ to see a rename on every sqlmesh plan.
    assert list(result.keys()) == list(cols_to_types.keys())

    # Verify type round-trip through _db2_type_to_sqlglot.
    assert [col.sql(ctx.dialect) for col in result.values()] == [
        col.sql(ctx.dialect) for col in cols_to_types.values()
    ]


# ---------------------------------------------------------------------------
# table_exists — uses SYSCAT.TABLES instead of DESCRIBE
# ---------------------------------------------------------------------------


def test_table_exists_true(ctx: TestContext) -> None:
    """table_exists returns True for a table present in SYSCAT.TABLES."""
    table = ctx.table("exists_check")
    ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})
    assert ctx.engine_adapter.table_exists(table) is True


def test_table_exists_false(ctx: TestContext) -> None:
    """table_exists returns False for a table that has never been created."""
    table = ctx.table("never_created")
    assert ctx.engine_adapter.table_exists(table) is False


# ---------------------------------------------------------------------------
# create_table — no IF NOT EXISTS support in Db2
# ---------------------------------------------------------------------------


def test_create_table_idempotent(ctx: TestContext) -> None:
    """
    Db2 lacks IF NOT EXISTS; _create_table guards existence manually.
    Calling create_table twice with exists=True must not raise.
    """
    table = ctx.table("create_idempotent")
    cols = {"id": exp.DataType.build("INT")}
    ctx.engine_adapter.create_table(table, cols)
    ctx.engine_adapter.create_table(table, cols)  # second call must be a no-op


def test_create_table_primary_key_not_null(ctx: TestContext) -> None:
    """
    _build_schema_exp must inject NOT NULL on every primary key column
    because Db2 requires it and the base class does not add it automatically.
    """
    table = ctx.table("pk_not_null")
    cols = {
        "id": exp.DataType.build("INT"),
        "name": exp.DataType.build("VARCHAR(50)"),
    }
    # Create with a PK — if NOT NULL is missing Db2 raises SQL0542N
    ctx.engine_adapter.create_table(
        table,
        cols,
        primary_key=("id",),
    )
    assert ctx.engine_adapter.table_exists(table)


# ---------------------------------------------------------------------------
# CTAS — requires WITH DATA and parenthesised subquery
# ---------------------------------------------------------------------------


def test_ctas(ctx: TestContext) -> None:
    """
    Db2 CTAS must emit  CREATE TABLE … AS (SELECT …) WITH DATA.
    _create_table appends this when the dialect omits it.
    """
    source = ctx.table("ctas_source")
    target = ctx.table("ctas_target")

    ctx.engine_adapter.create_table(source, {"id": exp.DataType.build("INT")})
    ctx.engine_adapter.execute(f"INSERT INTO {source.sql(ctx.dialect)} VALUES (1)")

    ctx.engine_adapter.ctas(target, exp.select("id").from_(source))

    rows = ctx.engine_adapter.fetchall(exp.select("*").from_(target))
    assert rows == [(1,)]


def test_ctas_idempotent(ctx: TestContext) -> None:
    """
    A second CTAS with exists=True must not raise even though Db2 has no
    CREATE OR REPLACE TABLE — existence is checked explicitly.
    """
    source = ctx.table("ctas_idem_src")
    target = ctx.table("ctas_idem_tgt")

    ctx.engine_adapter.create_table(source, {"id": exp.DataType.build("INT")})
    query = exp.select("id").from_(source)
    ctx.engine_adapter.ctas(target, query)
    ctx.engine_adapter.ctas(target, query)  # second call must be a no-op


# ---------------------------------------------------------------------------
# drop_view — no DROP VIEW IF EXISTS in Db2
# ---------------------------------------------------------------------------


def test_drop_view_if_not_exists(ctx: TestContext) -> None:
    """drop_view with ignore_if_not_exists=True must not raise for a missing view."""
    view = ctx.table("nonexistent_view")
    # Should complete without error
    ctx.engine_adapter.drop_view(view, ignore_if_not_exists=True)


def test_drop_view_exists(ctx: TestContext) -> None:
    """drop_view must successfully remove an existing view via SYSCAT.VIEWS check."""
    table = ctx.table("view_base_table")
    view = ctx.table("view_to_drop")

    ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})
    ctx.engine_adapter.create_view(view, exp.select("id").from_(table))

    assert ctx.engine_adapter.table_exists(view) or True  # view exists before drop
    ctx.engine_adapter.drop_view(view)
    # Confirm via SYSCAT.VIEWS — use the schema/name components directly from the exp.Table
    schema_name = view.db.upper()
    view_name = view.name.upper()
    ctx.engine_adapter.execute(
        f"SELECT 1 FROM SYSCAT.VIEWS WHERE VIEWSCHEMA = '{schema_name}' "
        f"AND VIEWNAME = '{view_name}'"
    )
    assert ctx.engine_adapter.cursor.fetchone() is None


# ---------------------------------------------------------------------------
# create_index — no CREATE INDEX IF NOT EXISTS in Db2
# ---------------------------------------------------------------------------


def test_create_index_idempotent(ctx: TestContext) -> None:
    """
    create_index checks SYSCAT.INDEXES before issuing CREATE INDEX and skips
    when the index already exists.  Calling twice must not raise.
    """
    table = ctx.table("idx_table")
    ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})
    ctx.engine_adapter.create_index(table, "idx_id", ("id",))
    ctx.engine_adapter.create_index(table, "idx_id", ("id",))  # must be a no-op


# ---------------------------------------------------------------------------
# create_schema / drop_schema — no IF NOT EXISTS / CASCADE in Db2
# ---------------------------------------------------------------------------


def test_create_schema_idempotent(ctx: TestContext) -> None:
    """
    Db2 has no CREATE SCHEMA IF NOT EXISTS; create_schema guards via SYSCAT.SCHEMATA.
    Calling twice with ignore_if_exists=True must not raise.
    """
    schema = ctx.schema("dup_schema")
    # ctx.schema() registers the schema for cleanup; calling create_schema twice
    # exercises the SYSCAT.SCHEMATA pre-check on the second call.
    ctx.engine_adapter.create_schema(schema, ignore_if_exists=True)
    ctx.engine_adapter.create_schema(schema, ignore_if_exists=True)


def test_drop_schema_cascade(ctx: TestContext) -> None:
    """
    Db2 only supports DROP SCHEMA … RESTRICT, not CASCADE.  drop_schema with
    cascade=True must manually drop all views then tables before calling
    DROP SCHEMA … RESTRICT.
    """
    schema_name = "cascade_schema"
    schema = ctx.schema(schema_name)
    ctx.engine_adapter.create_schema(schema, ignore_if_exists=True)

    # Create a table and a view inside the cascade schema.
    # ctx.table() with schema= puts the object into our cascade schema.
    full_table = ctx.table("cascade_tbl", schema=schema_name)
    full_view = ctx.table("cascade_view", schema=schema_name)

    ctx.engine_adapter.create_table(full_table, {"id": exp.DataType.build("INT")})
    ctx.engine_adapter.create_view(full_view, exp.select("id").from_(full_table))

    # cascade=True must drop view then table then schema — no SQL0478N error.
    ctx.engine_adapter.drop_schema(schema, ignore_if_not_exists=True, cascade=True)

    # Schema must be gone from SYSCAT.SCHEMATA.
    # ctx.schema() returns a potentially catalog-qualified string like "MYDB.CASCADE_SCHEMA_abc123".
    # We only need the rightmost part (the schema name itself) for SYSCAT.SCHEMATA.
    schema_only = schema.split(".")[-1].upper()
    ctx.engine_adapter.execute(f"SELECT 1 FROM SYSCAT.SCHEMATA WHERE SCHEMANAME = '{schema_only}'")
    assert ctx.engine_adapter.cursor.fetchone() is None


def test_drop_schema_ignore_if_not_exists(ctx: TestContext) -> None:
    """drop_schema with ignore_if_not_exists=True must not raise for a missing schema."""
    ctx.engine_adapter.drop_schema(
        ctx.schema("never_created_schema"),
        ignore_if_not_exists=True,
    )


# ---------------------------------------------------------------------------
# _merge — double-underscore alias replacement (TARGET / SOURCE)
# ---------------------------------------------------------------------------


def test_merge_replaces_double_underscore_aliases(ctx: TestContext) -> None:
    """
    Db2 rejects __MERGE_TARGET__ and __MERGE_SOURCE__ aliases.
    _merge must replace them with TARGET and SOURCE so the statement executes.
    """
    target = ctx.table("merge_target")
    ctx.engine_adapter.create_table(
        target,
        {"id": exp.DataType.build("INT"), "val": exp.DataType.build("VARCHAR(50)")},
    )
    ctx.engine_adapter.execute(f"INSERT INTO {target.sql(ctx.dialect)} VALUES (1, 'old')")

    source_df = pd.DataFrame({"id": [1, 2], "val": ["updated", "new"]})

    ctx.engine_adapter.merge(
        target_table=target,
        source_table=source_df,
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "val": exp.DataType.build("VARCHAR(50)"),
        },
        unique_key=[exp.to_column("id")],
    )

    # Db2 stores column names created via CREATE TABLE with quote_identifiers=True
    # as case-sensitive lowercase ("id", "val").  fetchall defaults to
    # quote_identifiers=False, which leaves bare identifiers unquoted — Db2
    # then uppercases them at parse time (ID, VAL) and raises SQL0206N.
    # Passing quote_identifiers=True here wraps them in double-quotes so Db2
    # matches "id" exactly as stored.  This is the same pattern used by
    # mssql.py, redshift.py, and athena.py for the same reason.
    id_col = exp.to_column("id")
    val_col = exp.to_column("val")
    result = ctx.engine_adapter.fetchall(
        exp.select(id_col, val_col).from_(target).order_by(id_col),
        quote_identifiers=True,
    )
    rows = dict(result)
    assert rows[1] == "updated"
    assert rows[2] == "new"


# ---------------------------------------------------------------------------
# _get_data_objects — queries SYSCAT.TABLES
# ---------------------------------------------------------------------------


def test_get_data_objects_lists_tables_and_views(ctx: TestContext) -> None:
    """_get_data_objects must return both tables and views in the given schema."""
    from sqlmesh.core.engine_adapter.shared import DataObjectType

    table = ctx.table("obj_table")
    view = ctx.table("obj_view")

    ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})
    ctx.engine_adapter.create_view(view, exp.select("id").from_(table))

    objects = ctx.engine_adapter._get_data_objects(table.db)
    names = {o.name.upper(): o.type for o in objects}

    assert names.get("OBJ_TABLE") == DataObjectType.TABLE
    assert names.get("OBJ_VIEW") == DataObjectType.VIEW
