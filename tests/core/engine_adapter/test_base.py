# type: ignore
import typing as t
from datetime import datetime
from unittest.mock import call

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one
from sqlglot.helper import ensure_list

from sqlmesh.core import dialect as d
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.engine_adapter import EngineAdapter, EngineAdapterWithIndexSupport
from sqlmesh.core.engine_adapter.shared import InsertOverwriteStrategy, DataObject
from sqlmesh.core.schema_diff import SchemaDiffer, TableAlterOperation, NestedSupport
from sqlmesh.utils import columns_to_types_to_struct
from sqlmesh.utils.date import to_ds
from sqlmesh.utils.errors import SQLMeshError, UnsupportedCatalogOperationError
from tests.core.engine_adapter import to_sql_calls


pytestmark = pytest.mark.engine


def test_create_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"))
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"), replace=False)
    # Test that `physical_properties` are ignored for base engine adapter
    adapter.create_view(
        "test_view",
        parse_one("SELECT a FROM tbl"),
        replace=True,
        view_properties={"a": exp.convert(1)},
    )

    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE VIEW "test_view" AS SELECT "a" FROM "tbl"',
        'CREATE VIEW "test_view" AS SELECT "a" FROM "tbl"',
        'CREATE OR REPLACE VIEW "test_view" AS SELECT "a" FROM "tbl"',
    ]


def test_create_view_existing_data_object_type_mismatch(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test_view", type="table")],
    )
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"))

    assert to_sql_calls(adapter) == [
        'DROP TABLE IF EXISTS "test_view"',
        'CREATE OR REPLACE VIEW "test_view" AS SELECT "a" FROM "tbl"',
    ]


def test_create_view_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.create_view("test_view", pd.DataFrame({"a": [1, 2, 3]}), replace=False)
    adapter.create_view(
        "test_view",
        pd.DataFrame({"a": [1, 2, 3]}),
        replace=True,
        view_properties={"a": exp.convert(1)},
    )

    assert to_sql_calls(adapter) == [
        'CREATE VIEW "test_view" ("a") AS SELECT CAST("a" AS BIGINT) AS "a" FROM (VALUES (1), (2), (3)) AS "t"("a")',
        'CREATE OR REPLACE VIEW "test_view" ("a") AS SELECT CAST("a" AS BIGINT) AS "a" FROM (VALUES (1), (2), (3)) AS "t"("a")',
    ]


def test_create_view_pandas_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    bigint_dtype = exp.DataType.build("BIGINT")
    adapter.create_view(
        "test_view",
        pd.DataFrame({"a": [1, 2, 3], "ignored_source": [4, 5, 6]}),
        target_columns_to_types={"a": bigint_dtype, "b": bigint_dtype},
        replace=False,
        source_columns=["a", "ignored_source"],
    )

    assert to_sql_calls(adapter) == [
        'CREATE VIEW "test_view" ("a", "b") AS SELECT "a", CAST(NULL AS BIGINT) AS "b" FROM (SELECT CAST("a" AS BIGINT) AS "a" FROM (VALUES (1), (2), (3)) AS "t"("a")) AS "select_source_columns"',
    ]


def test_create_view_query_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.create_view(
        "test_view",
        parse_one("SELECT a, ignored_source FROM tbl"),
        target_columns_to_types={
            "a": exp.DataType.build("BIGINT"),
            "b": exp.DataType.build("BIGINT"),
        },
        replace=False,
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'CREATE VIEW "test_view" ("a", "b") AS SELECT "a", CAST(NULL AS BIGINT) AS "b" FROM (SELECT "a", "ignored_source" FROM "tbl") AS "select_source_columns"',
    ]


def test_create_materialized_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.SUPPORTS_MATERIALIZED_VIEWS = True
    adapter.create_view(
        "test_view",
        parse_one("SELECT a FROM tbl"),
        materialized=True,
        target_columns_to_types={"a": exp.DataType.build("INT")},
    )
    adapter.create_view(
        "test_view",
        parse_one("SELECT a FROM tbl"),
        replace=False,
        materialized=True,
        target_columns_to_types={"a": exp.DataType.build("INT")},
    )

    adapter.cursor.execute.assert_has_calls(
        [
            call('CREATE OR REPLACE MATERIALIZED VIEW "test_view" AS SELECT "a" FROM "tbl"'),
            call('CREATE MATERIALIZED VIEW "test_view" AS SELECT "a" FROM "tbl"'),
        ]
    )

    adapter.SUPPORTS_MATERIALIZED_VIEW_SCHEMA = True
    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM tbl"),
        replace=False,
        materialized=True,
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
    )
    adapter.create_view(
        "test_view", parse_one("SELECT a, b FROM tbl"), replace=False, materialized=True
    )

    adapter.cursor.execute.assert_has_calls(
        [
            call('CREATE MATERIALIZED VIEW "test_view" ("a", "b") AS SELECT "a", "b" FROM "tbl"'),
            call('CREATE MATERIALIZED VIEW "test_view" AS SELECT "a", "b" FROM "tbl"'),
        ]
    )


def test_create_schema(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.create_schema("test_schema")
    adapter.create_schema("test_schema", ignore_if_exists=False)

    adapter.cursor.execute.assert_has_calls(
        [
            call('CREATE SCHEMA IF NOT EXISTS "test_schema"'),
            call('CREATE SCHEMA "test_schema"'),
        ]
    )

    with pytest.raises(UnsupportedCatalogOperationError):
        adapter.create_schema("test_catalog.test_schema")


def test_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.cursor.fetchall.return_value = [
        ("id", "int"),
        ("name", "string"),
        ("price", "double"),
        ("ds", "string"),
        ("# Partition Information", ""),
        ("# col_name", "data_type"),
        ("ds", "string"),
    ]
    assert adapter.columns("test_table") == {
        "id": exp.DataType.build("int"),
        "name": exp.DataType.build("string"),
        "price": exp.DataType.build("double"),
        "ds": exp.DataType.build("string"),
    }

    adapter.cursor.execute.assert_called_once_with('DESCRIBE "test_table"')


def test_iceberg_corrupt(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.cursor.fetchall.return_value = [
        ("id", "int"),
        (" ", ""),
        ("", b"\n"),
    ]
    assert adapter.columns("test_table") == {
        "id": exp.DataType.build("int"),
    }


def test_table_exists(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    assert adapter.table_exists("test_table")
    adapter.cursor.execute.assert_called_once_with('DESCRIBE "test_table"')

    adapter.cursor.reset_mock()
    adapter.cursor.execute.side_effect = RuntimeError("error")
    assert not adapter.table_exists("test_table")
    adapter.cursor.execute.assert_called_once_with('DESCRIBE "test_table"')


def test_insert_overwrite_by_time_partition(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, b FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
    )

    adapter.cursor.begin.assert_called_once()
    adapter.cursor.commit.assert_called_once()

    assert to_sql_calls(adapter) == [
        """DELETE FROM "test_table" WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02'""",
        """INSERT INTO "test_table" ("a", "b") SELECT "a", "b" FROM (SELECT "a", "b" FROM "tbl") AS "_subquery" WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02'""",
    ]


def test_insert_overwrite_by_time_partition_missing_time_column_type(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    columns_mock = mocker.patch.object(adapter, "columns")
    columns_mock.return_value = {"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")}

    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, b FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("UNKNOWN"),
        },
    )

    columns_mock.assert_called_once_with("test_table")
    adapter.cursor.begin.assert_called_once()
    adapter.cursor.commit.assert_called_once()

    assert to_sql_calls(adapter) == [
        """DELETE FROM "test_table" WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02'""",
        """INSERT INTO "test_table" ("a", "b") SELECT "a", "b" FROM (SELECT "a", "b" FROM "tbl") AS "_subquery" WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02'""",
    ]


def test_insert_overwrite_by_time_partition_supports_insert_overwrite(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE

    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, b FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
    )

    adapter.cursor.execute.assert_called_once_with(
        """INSERT OVERWRITE TABLE "test_table" ("a", "b") SELECT "a", "b" FROM (SELECT "a", "b" FROM "tbl") AS "_subquery" WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02'"""
    )


def test_insert_overwrite_by_time_partition_supports_insert_overwrite_pandas(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE
    df = pd.DataFrame({"a": [1, 2], "ds": ["2022-01-01", "2022-01-02"]})

    adapter.insert_overwrite_by_time_partition(
        "test_table",
        df,
        start="2022-01-01",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
    )

    assert to_sql_calls(adapter) == [
        """INSERT OVERWRITE TABLE "test_table" ("a", "ds") SELECT "a", "ds" FROM (SELECT CAST("a" AS INT) AS "a", CAST("ds" AS TEXT) AS "ds" FROM (VALUES (1, '2022-01-01'), (2, '2022-01-02')) AS "t"("a", "ds")) AS "_subquery" WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_by_time_partition_supports_insert_overwrite_pandas_source_columns(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE
    df = pd.DataFrame({"a": [1, 2], "ignored_source": [3, 4]})
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        df,
        start="2022-01-01",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        """INSERT OVERWRITE TABLE "test_table" ("a", "ds") SELECT "a", "ds" FROM (SELECT CAST("a" AS INT) AS "a", CAST(NULL AS TEXT) AS "ds" FROM (VALUES (1), (2)) AS "t"("a")) AS "_subquery" WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_by_time_partition_supports_insert_overwrite_query_source_columns(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, ignored_source FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        """INSERT OVERWRITE TABLE "test_table" ("a", "ds") SELECT "a", "ds" FROM (SELECT "a", CAST(NULL AS TEXT) AS "ds" FROM (SELECT "a", "ignored_source" FROM "tbl") AS "select_source_columns") AS "_subquery" WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_by_time_partition_replace_where(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.REPLACE_WHERE

    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, b FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="b",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
    )

    assert to_sql_calls(adapter) == [
        """INSERT INTO "test_table" REPLACE WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02' SELECT "a", "b" FROM (SELECT "a", "b" FROM "tbl") AS "_subquery" WHERE "b" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_by_time_partition_replace_where_pandas(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.REPLACE_WHERE
    df = pd.DataFrame({"a": [1, 2], "ds": ["2022-01-01", "2022-01-02"]})

    adapter.insert_overwrite_by_time_partition(
        "test_table",
        df,
        start="2022-01-01",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
    )

    assert to_sql_calls(adapter) == [
        """INSERT INTO "test_table" REPLACE WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02' SELECT "a", "ds" FROM (SELECT CAST("a" AS INT) AS "a", CAST("ds" AS TEXT) AS "ds" FROM (VALUES (1, '2022-01-01'), (2, '2022-01-02')) AS "t"("a", "ds")) AS "_subquery" WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_by_time_partition_replace_where_pandas_source_columns(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.REPLACE_WHERE
    df = pd.DataFrame({"a": [1, 2], "ignored_source": [3, 4]})
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        df,
        start="2022-01-01",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        """INSERT INTO "test_table" REPLACE WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02' SELECT "a", "ds" FROM (SELECT CAST("a" AS INT) AS "a", CAST(NULL AS TEXT) AS "ds" FROM (VALUES (1), (2)) AS "t"("a")) AS "_subquery" WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_by_time_partition_replace_where_query_source_columns(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.REPLACE_WHERE
    adapter.insert_overwrite_by_time_partition(
        "test_table",
        parse_one("SELECT a, ignored_source FROM tbl"),
        start="2022-01-01",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        """INSERT INTO "test_table" REPLACE WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02' SELECT "a", "ds" FROM (SELECT "a", CAST(NULL AS TEXT) AS "ds" FROM (SELECT "a", "ignored_source" FROM "tbl") AS "select_source_columns") AS "_subquery" WHERE "ds" BETWEEN '2022-01-01' AND '2022-01-02'"""
    ]


def test_insert_overwrite_no_where(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one("SELECT b, a FROM tbl"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("STRING")},
        target_table="test_table",
    )

    adapter._insert_overwrite_by_condition(
        "test_table",
        source_queries,
        target_columns_to_types=columns_to_types,
    )

    adapter.cursor.begin.assert_called_once()
    adapter.cursor.commit.assert_called_once()

    assert to_sql_calls(adapter) == [
        """DELETE FROM "test_table" WHERE TRUE""",
        """INSERT INTO "test_table" ("a", "b") SELECT "a", "b" FROM (SELECT "b", "a" FROM "tbl") AS "_subquery" """.strip(),
    ]


def test_insert_overwrite_by_condition_column_contains_unsafe_characters(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.MERGE

    source_queries, columns_to_types = adapter._get_source_queries_and_columns_to_types(
        parse_one("SELECT 1 AS c"), None, target_table="test_table"
    )

    columns_mock = mocker.patch.object(adapter, "columns")
    columns_mock.return_value = {"foo.bar.baz": exp.DataType.build("INT")}

    adapter._insert_overwrite_by_condition(
        "test_table",
        source_queries,
        target_columns_to_types=None,
    )

    # The goal here is to assert that we don't parse `foo.bar.baz` into a qualified column
    assert to_sql_calls(adapter) == [
        'MERGE INTO "test_table" AS "__MERGE_TARGET__" USING (SELECT "foo.bar.baz" FROM (SELECT 1 AS "c") AS "_subquery") AS "__MERGE_SOURCE__" ON FALSE WHEN NOT MATCHED BY SOURCE THEN DELETE WHEN NOT MATCHED THEN INSERT ("foo.bar.baz") VALUES ("foo.bar.baz")'
    ]


def test_insert_append_query(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.insert_append(
        "test_table",
        parse_one("SELECT a FROM tbl"),
        target_columns_to_types={"a": exp.DataType.build("INT")},
    )

    assert to_sql_calls(adapter) == [
        'INSERT INTO "test_table" ("a") SELECT "a" FROM "tbl"',
    ]


def test_insert_append_query_select_star(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.insert_append(
        "test_table",
        parse_one("SELECT 1 AS a, * FROM tbl"),
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
    )

    assert to_sql_calls(adapter) == [
        'INSERT INTO "test_table" ("a", "b") SELECT "a", "b" FROM (SELECT 1 AS "a", * FROM "tbl") AS "_subquery"',
    ]


def test_insert_append_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.insert_append(
        "test_table",
        df,
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
    )

    assert to_sql_calls(adapter) == [
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (1, 4), (2, 5), (3, 6)) AS "t"("a", "b")',
    ]


def test_insert_append_pandas_batches(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.DEFAULT_BATCH_SIZE = 1

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.insert_append(
        "test_table",
        df,
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
    )

    adapter.cursor.begin.assert_called_once()
    adapter.cursor.commit.assert_called_once()

    assert to_sql_calls(adapter) == [
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (1, 4)) AS "t"("a", "b")',
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (2, 5)) AS "t"("a", "b")',
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (3, 6)) AS "t"("a", "b")',
    ]


def test_insert_append_pandas_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    df = pd.DataFrame({"a": [1, 2, 3], "ignored_source": [4, 5, 6]})
    adapter.insert_append(
        "test_table",
        df,
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST(NULL AS INT) AS "b" FROM (VALUES (1), (2), (3)) AS "t"("a")',
    ]


def test_insert_append_query_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.insert_append(
        "test_table",
        parse_one("SELECT a, ignored_source FROM tbl"),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'INSERT INTO "test_table" ("a", "b") SELECT "a", CAST(NULL AS INT) AS "b" FROM (SELECT "a", "ignored_source" FROM "tbl") AS "select_source_columns"',
    ]


def test_create_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }
    adapter.create_table("test_table", columns_to_types)

    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "test_table" ("cola" INT, "colb" TEXT)'
    )


def test_create_table_properties(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }
    adapter.create_table(
        "test_table",
        columns_to_types,
        partitioned_by=[exp.to_column("colb")],
        storage_format="ICEBERG",
    )

    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "test_table" ("cola" INT, "colb" TEXT)'
    )


def test_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description="test description",
        column_descriptions={"a": "a description"},
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description="test description",
        column_descriptions={"a": "a description"},
    )

    # CTAS call should not include schema definition if UNKNOWN data type present
    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("UNKNOWN"), "b": exp.DataType.build("INT")},
        table_description="test description",
        column_descriptions={"a": "a description"},
    )

    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM source_table"),
        table_description="test description",
    )

    adapter._create_table_comment(
        "test_table",
        "test description",
    )

    adapter._create_column_comments(
        "test_table",
        {"a": "a description"},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("a" INT COMMENT 'a description', "b" INT) COMMENT='test description'""",
        """CREATE TABLE IF NOT EXISTS "test_table" ("a" INT COMMENT 'a description', "b" INT) COMMENT='test description' AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", "b" FROM "source_table") AS "_subquery\"""",
        """CREATE TABLE IF NOT EXISTS "test_table" COMMENT='test description' AS SELECT "a", "b" FROM "source_table\"""",
        """COMMENT ON COLUMN "test_table"."a" IS 'a description'""",
        """CREATE OR REPLACE VIEW "test_view" COMMENT='test description' AS SELECT "a", "b" FROM "source_table\"""",
        """COMMENT ON TABLE "test_table" IS 'test description'""",
        """COMMENT ON COLUMN "test_table"."a" IS 'a description'""",
    ]

    # verify comments aren't registered if the config flag is False
    adapter_no_comments = make_mocked_engine_adapter(EngineAdapter, register_comments=False)

    adapter_no_comments.create_table(
        "test_table",
        {"a": exp.DataType.build("int"), "b": exp.DataType.build("int")},
        table_description="test description",
        column_descriptions={"a": "a description"},
    )

    sql_calls = to_sql_calls(adapter_no_comments)
    assert sql_calls == [
        """CREATE TABLE IF NOT EXISTS "test_table" ("a" INT, "b" INT)""",
    ]


@pytest.mark.parametrize(
    "schema_differ_config, current_table, target_table, expected_final_structure, expected",
    [
        (
            {
                "support_positional_add": True,
                "nested_support": NestedSupport.ALL,
                "array_element_selector": "element",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "c": "INT",
                "d": "INT",
                "nested": "STRUCT<nested_a INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT>>",
            },
            {
                "f": "VARCHAR(100)",
                "a": "INT",
                "e": "TEXT",
                "b": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            {
                "f": "VARCHAR(100)",
                "a": "INT",
                "e": "TEXT",
                "b": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "c"',
                'ALTER TABLE "test_table" DROP COLUMN "d"',
                'ALTER TABLE "test_table" ADD COLUMN "f" VARCHAR(100) FIRST',
                'ALTER TABLE "test_table" ADD COLUMN "e" TEXT AFTER "a"',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_b" INT AFTER "nested_a"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_b" INT AFTER "array_a"',
            ],
        ),
        (
            {
                "coerceable_types": {
                    exp.DataType.build("FLOAT"): {exp.DataType.build("INT")},
                },
            },
            {
                "a": "FLOAT",
                "b": "TEXT",
            },
            {
                "a": "INT",
                "b": "TEXT",
            },
            {
                "a": "FLOAT",
                "b": "TEXT",
            },
            [],
        ),
        (
            {
                "nested_support": NestedSupport.ALL_BUT_DROP,
                "array_element_selector": "element",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "c": "INT",
                "d": "INT",
                "nested": "STRUCT<nested_a INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT>>",
            },
            {
                "f": "VARCHAR(100)",
                "a": "INT",
                "e": "TEXT",
                "b": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_c INT, nested_b INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT, array_b INT>>",
                "f": "VARCHAR(100)",
                "e": "TEXT",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "c"',
                'ALTER TABLE "test_table" DROP COLUMN "d"',
                'ALTER TABLE "test_table" ADD COLUMN "f" VARCHAR(100)',
                'ALTER TABLE "test_table" ADD COLUMN "e" TEXT',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_b" INT',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_b" INT',
            ],
        ),
        (
            {
                "array_element_selector": "element",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "c": "INT",
                "d": "INT",
                "nested": "STRUCT<nested_a INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT>>",
            },
            {
                "f": "VARCHAR(100)",
                "a": "INT",
                "e": "TEXT",
                "b": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "f": "VARCHAR(100)",
                "e": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "c"',
                'ALTER TABLE "test_table" DROP COLUMN "d"',
                'ALTER TABLE "test_table" ADD COLUMN "f" VARCHAR(100)',
                'ALTER TABLE "test_table" ADD COLUMN "e" TEXT',
                'ALTER TABLE "test_table" DROP COLUMN "nested"',
                'ALTER TABLE "test_table" ADD COLUMN "nested" STRUCT<"nested_a" INT, "nested_b" INT, "nested_c" INT>',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col" ARRAY<STRUCT<"array_a" INT, "array_b" INT, "array_c" INT>>',
            ],
        ),
        (
            {
                "array_element_selector": "element",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "c": "INT",
                "d": "INT",
                "nested": "STRUCT<nested_a INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT>>",
            },
            {
                "f": "VARCHAR(100)",
                "a": "INT",
                "e": "TEXT",
                "b": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            {
                "a": "INT",
                "b": "TEXT",
                "f": "VARCHAR(100)",
                "e": "TEXT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_c INT>>",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "c"',
                'ALTER TABLE "test_table" DROP COLUMN "d"',
                'ALTER TABLE "test_table" ADD COLUMN "f" VARCHAR(100)',
                'ALTER TABLE "test_table" ADD COLUMN "e" TEXT',
                'ALTER TABLE "test_table" DROP COLUMN "nested"',
                'ALTER TABLE "test_table" ADD COLUMN "nested" STRUCT<"nested_a" INT, "nested_b" INT, "nested_c" INT>',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col" ARRAY<STRUCT<"array_a" INT, "array_b" INT, "array_c" INT>>',
            ],
        ),
        # Test multiple operations on a column with positional and nested features enabled
        (
            {
                "support_positional_add": True,
                "nested_support": NestedSupport.ALL,
                "array_element_selector": "element",
            },
            {
                "nested": """STRUCT<nested_a INT, "nested_c" INT, nested_e INT>""",
                "array_col": """ARRAY<STRUCT<array_a INT, "array_c" INT, array_e INT>>""",
            },
            {
                "nested": """STRUCT<nested_a INT, "nested_b" INT, nested_d INT, nested_e INT>""",
                "array_col": """ARRAY<STRUCT<array_a INT, "array_b" INT, array_d INT, array_e INT>>""",
            },
            {
                "nested": """STRUCT<nested_a INT, "nested_b" INT, nested_d INT, nested_e INT>""",
                "array_col": """ARRAY<STRUCT<array_a INT, "array_b" INT, array_d INT, array_e INT>>""",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "nested"."nested_c"',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_b" INT AFTER "nested_a"',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_d" INT AFTER "nested_b"',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"."element"."array_c"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_b" INT AFTER "array_a"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_d" INT AFTER "array_b"',
            ],
        ),
        # Test multiple operations on a column with positional and nested features enabled and that when adding
        # last we don't include position since it is not needed
        (
            {
                "support_positional_add": True,
                "nested_support": NestedSupport.ALL,
                "array_element_selector": "element",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_c INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT>>",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT>>",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT>>",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "nested"."nested_c"',
                # Position is not included since we are adding to last so we don't need to specify position
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_b" INT',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_d" INT',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"."element"."array_c"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_b" INT',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_d" INT',
            ],
        ),
        # Test multiple operations on a column with positional and no nested features enabled
        (
            {
                "support_positional_add": True,
                "array_element_selector": "element",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_c INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT, array_e INT>>",
                "col_c": "INT",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT, array_e INT>>",
                "col_c": "INT",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT, array_e INT>>",
                "col_c": "INT",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "nested"',
                'ALTER TABLE "test_table" ADD COLUMN "nested" STRUCT<"nested_a" INT, "nested_b" INT, "nested_d" INT, "nested_e" INT> FIRST',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col" ARRAY<STRUCT<"array_a" INT, "array_b" INT, "array_d" INT, "array_e" INT>> AFTER "nested"',
            ],
        ),
        # Test multiple operations on a column with no positional and nested features enabled
        (
            {
                "nested_support": NestedSupport.ALL,
                "array_element_selector": "element",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_c INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT, nested_e INT>>",
                "col_c": "INT",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT, nested_e INT>>",
                "col_c": "INT",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_e INT, nested_b INT, nested_d INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, nested_e INT, array_b INT, array_d INT>>",
                "col_c": "INT",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "nested"."nested_c"',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_b" INT',
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_d" INT',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"."element"."array_c"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_b" INT',
                'ALTER TABLE "test_table" ADD COLUMN "array_col"."element"."array_d" INT',
            ],
        ),
        # Test multiple operations on a column with no positional or nested features enabled
        (
            {
                "array_element_selector": "element",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_c INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_c INT, array_e INT>>",
                "col_c": "INT",
            },
            {
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT, array_e INT>>",
                "col_c": "INT",
            },
            {
                "col_c": "INT",
                "nested": "STRUCT<nested_a INT, nested_b INT, nested_d INT, nested_e INT>",
                "array_col": "ARRAY<STRUCT<array_a INT, array_b INT, array_d INT, array_e INT>>",
            },
            [
                'ALTER TABLE "test_table" DROP COLUMN "nested"',
                'ALTER TABLE "test_table" ADD COLUMN "nested" STRUCT<"nested_a" INT, "nested_b" INT, "nested_d" INT, "nested_e" INT>',
                'ALTER TABLE "test_table" DROP COLUMN "array_col"',
                'ALTER TABLE "test_table" ADD COLUMN "array_col" ARRAY<STRUCT<"array_a" INT, "array_b" INT, "array_d" INT, "array_e" INT>>',
            ],
        ),
        # Test deeply nested structures
        (
            {
                "nested_support": NestedSupport.ALL,
                "array_element_selector": "element",
            },
            {
                "nested": """STRUCT<nested_1_a STRUCT<"nested_2_a" ARRAY<STRUCT<nested_3_a STRUCT<nested_4_a ARRAY<STRUCT<"nested_5_a" ARRAY<STRUCT<nested_6_a INT>>>>>>>>>""",
            },
            {
                "nested": """STRUCT<nested_1_a STRUCT<"nested_2_a" ARRAY<STRUCT<nested_3_a STRUCT<nested_4_a ARRAY<STRUCT<"nested_5_a" ARRAY<STRUCT<nested_6_a INT, nested_6_b INT>>>>>>>>>""",
            },
            {
                "nested": """STRUCT<nested_1_a STRUCT<"nested_2_a" ARRAY<STRUCT<nested_3_a STRUCT<nested_4_a ARRAY<STRUCT<"nested_5_a" ARRAY<STRUCT<nested_6_a INT, nested_6_b INT>>>>>>>>>""",
            },
            [
                'ALTER TABLE "test_table" ADD COLUMN "nested"."nested_1_a"."nested_2_a"."element"."nested_3_a"."nested_4_a"."element"."nested_5_a"."element"."nested_6_b" INT',
            ],
        ),
    ],
)
def test_alter_table(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    schema_differ_config: t.Dict[str, t.Any],
    current_table: t.Dict[str, str],
    target_table: t.Dict[str, str],
    expected_final_structure: t.Dict[str, str],
    expected: t.List[str],
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.SCHEMA_DIFFER_KWARGS = schema_differ_config
    original_from_structs = adapter.schema_differ._from_structs

    def _from_structs(*args, **kwargs) -> t.List[TableAlterOperation]:
        operations = original_from_structs(*args, **kwargs)
        if not operations:
            return operations
        assert (
            operations[-1].expected_table_struct.sql()
            == columns_to_types_to_struct(expected_final_structure).sql()
        )
        return operations

    mocker.patch.object(SchemaDiffer, "_from_structs", side_effect=_from_structs)

    current_table_name = "test_table"
    target_table_name = "target_table"

    def table_columns(table_name: str) -> t.Dict[str, exp.DataType]:
        if table_name == current_table_name:
            return {k: exp.DataType.build(v) for k, v in current_table.items()}
        return {k: exp.DataType.build(v) for k, v in target_table.items()}

    adapter.columns = table_columns

    adapter.alter_table(adapter.get_alter_operations(current_table_name, target_table_name))

    adapter.cursor.begin.assert_called_once()
    adapter.cursor.commit.assert_called_once()
    adapter.cursor.execute.assert_has_calls([call(x) for x in expected])


def test_merge_upsert(make_mocked_engine_adapter: t.Callable, assert_exp_eq):
    adapter = make_mocked_engine_adapter(EngineAdapter)

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

    assert_exp_eq(
        adapter.cursor.execute.call_args[0][0],
        """
MERGE INTO "target" AS "__MERGE_TARGET__" USING (
  SELECT
    "ID",
    "ts",
    "val"
  FROM "source"
) AS "__MERGE_SOURCE__"
  ON "__MERGE_TARGET__"."ID" = "__MERGE_SOURCE__"."ID"
  WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."ID" = "__MERGE_SOURCE__"."ID",
    "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts",
    "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val"
  WHEN NOT MATCHED THEN INSERT ("ID", "ts", "val")
    VALUES ("__MERGE_SOURCE__"."ID", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")
""",
    )

    adapter.cursor.reset_mock()
    adapter.merge(
        target_table="target",
        source_table=parse_one("SELECT id, ts, val FROM source"),
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.column("id"), exp.column("ts")],
    )
    adapter.cursor.execute.assert_called_once_with(
        'MERGE INTO "target" AS "__MERGE_TARGET__" USING (SELECT "id", "ts", "val" FROM "source") AS "__MERGE_SOURCE__" ON "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id" AND "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts" '
        'WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id", "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val" '
        'WHEN NOT MATCHED THEN INSERT ("id", "ts", "val") VALUES ("__MERGE_SOURCE__"."id", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")'
    )


def test_merge_upsert_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    df = pd.DataFrame({"id": [1, 2, 3], "ts": [4, 5, 6], "val": [1, 2, 3]})
    adapter.merge(
        target_table="target",
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id")],
    )
    adapter.cursor.execute.assert_called_once_with(
        'MERGE INTO "target" AS "__MERGE_TARGET__" USING (SELECT CAST("id" AS INT) AS "id", CAST("ts" AS TIMESTAMP) AS "ts", CAST("val" AS INT) AS "val" FROM (VALUES (1, 4, 1), (2, 5, 2), (3, 6, 3)) AS "t"("id", "ts", "val")) AS "__MERGE_SOURCE__" ON "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id" '
        'WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id", "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val" '
        'WHEN NOT MATCHED THEN INSERT ("id", "ts", "val") VALUES ("__MERGE_SOURCE__"."id", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")'
    )

    adapter.cursor.reset_mock()
    adapter.merge(
        target_table="target",
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id"), exp.to_identifier("ts")],
    )
    adapter.cursor.execute.assert_called_once_with(
        'MERGE INTO "target" AS "__MERGE_TARGET__" USING (SELECT CAST("id" AS INT) AS "id", CAST("ts" AS TIMESTAMP) AS "ts", CAST("val" AS INT) AS "val" FROM (VALUES (1, 4, 1), (2, 5, 2), (3, 6, 3)) AS "t"("id", "ts", "val")) AS "__MERGE_SOURCE__" ON "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id" AND "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts" '
        'WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id", "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val" '
        'WHEN NOT MATCHED THEN INSERT ("id", "ts", "val") VALUES ("__MERGE_SOURCE__"."id", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")'
    )


def test_merge_upsert_pandas_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    df = pd.DataFrame({"id": [1, 2, 3], "ts": [4, 5, 6], "ignored_source": [7, 8, 9]})
    adapter.merge(
        target_table="target",
        source_table=df,
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id")],
        source_columns=["id", "ignored_source", "ts"],
    )
    adapter.cursor.execute.assert_called_once_with(
        'MERGE INTO "target" AS "__MERGE_TARGET__" USING (SELECT CAST("id" AS INT) AS "id", CAST("ts" AS TIMESTAMP) AS "ts", CAST(NULL AS INT) AS "val" FROM (VALUES (1, 4), (2, 5), (3, 6)) AS "t"("id", "ts")) AS "__MERGE_SOURCE__" ON "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id" '
        'WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id", "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val" '
        'WHEN NOT MATCHED THEN INSERT ("id", "ts", "val") VALUES ("__MERGE_SOURCE__"."id", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")'
    )


def test_merge_upsert_query_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.merge(
        target_table="target",
        source_table=parse_one("SELECT id, ts, ignored_source FROM source"),
        target_columns_to_types={
            "id": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("id")],
        source_columns=["id", "ts", "ignored_source"],
    )
    adapter.cursor.execute.assert_called_once_with(
        'MERGE INTO "target" AS "__MERGE_TARGET__" USING (SELECT "id", "ts", CAST(NULL AS INT) AS "val" FROM (SELECT "id", "ts", "ignored_source" FROM "source") AS "select_source_columns") AS "__MERGE_SOURCE__" ON "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id" '
        'WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."id" = "__MERGE_SOURCE__"."id", "__MERGE_TARGET__"."ts" = "__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val" '
        'WHEN NOT MATCHED THEN INSERT ("id", "ts", "val") VALUES ("__MERGE_SOURCE__"."id", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")'
    )


def test_merge_when_matched(make_mocked_engine_adapter: t.Callable, assert_exp_eq):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
        when_matched=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    source=False,
                    then=exp.Update(
                        expressions=[
                            exp.column("val", "__MERGE_TARGET__").eq(
                                exp.column("val", "__MERGE_SOURCE__")
                            ),
                            exp.column("ts", "__MERGE_TARGET__").eq(
                                exp.Coalesce(
                                    this=exp.column("ts", "__MERGE_SOURCE__"),
                                    expressions=[exp.column("ts", "__MERGE_TARGET__")],
                                )
                            ),
                        ],
                    ),
                )
            ]
        ),
    )

    assert_exp_eq(
        adapter.cursor.execute.call_args[0][0],
        """
MERGE INTO "target" AS "__MERGE_TARGET__" USING (
  SELECT
    "ID",
    "ts",
    "val"
  FROM "source"
) AS "__MERGE_SOURCE__"
  ON "__MERGE_TARGET__"."ID" = "__MERGE_SOURCE__"."ID"
  WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val", "__MERGE_TARGET__"."ts" = COALESCE("__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."ts")
  WHEN NOT MATCHED THEN INSERT ("ID", "ts", "val")
    VALUES ("__MERGE_SOURCE__"."ID", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")
""",
    )


def test_merge_when_matched_multiple(make_mocked_engine_adapter: t.Callable, assert_exp_eq):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
        when_matched=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    condition=exp.column("ID", "__MERGE_SOURCE__").eq(exp.Literal.number(1)),
                    then=exp.Update(
                        expressions=[
                            exp.column("val", "__MERGE_TARGET__").eq(
                                exp.column("val", "__MERGE_SOURCE__")
                            ),
                            exp.column("ts", "__MERGE_TARGET__").eq(
                                exp.Coalesce(
                                    this=exp.column("ts", "__MERGE_SOURCE__"),
                                    expressions=[exp.column("ts", "__MERGE_TARGET__")],
                                )
                            ),
                        ],
                    ),
                ),
                exp.When(
                    matched=True,
                    source=False,
                    then=exp.Update(
                        expressions=[
                            exp.column("val", "__MERGE_TARGET__").eq(
                                exp.column("val", "__MERGE_SOURCE__")
                            ),
                            exp.column("ts", "__MERGE_TARGET__").eq(
                                exp.Coalesce(
                                    this=exp.column("ts", "__MERGE_SOURCE__"),
                                    expressions=[exp.column("ts", "__MERGE_TARGET__")],
                                )
                            ),
                        ],
                    ),
                ),
            ]
        ),
    )

    assert_exp_eq(
        adapter.cursor.execute.call_args[0][0],
        """
MERGE INTO "target" AS "__MERGE_TARGET__" USING (
  SELECT
    "ID",
    "ts",
    "val"
  FROM "source"
) AS "__MERGE_SOURCE__"
  ON "__MERGE_TARGET__"."ID" = "__MERGE_SOURCE__"."ID"
  WHEN MATCHED AND "__MERGE_SOURCE__"."ID" = 1 THEN UPDATE SET "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val", "__MERGE_TARGET__"."ts" = COALESCE("__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."ts"),
  WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val", "__MERGE_TARGET__"."ts" = COALESCE("__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."ts")
  WHEN NOT MATCHED THEN INSERT ("ID", "ts", "val")
    VALUES ("__MERGE_SOURCE__"."ID", "__MERGE_SOURCE__"."ts", "__MERGE_SOURCE__"."val")
""",
    )


def test_merge_filter(make_mocked_engine_adapter: t.Callable, assert_exp_eq):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one('SELECT "ID", ts, val FROM source')),
        target_columns_to_types={
            "ID": exp.DataType.build("int"),
            "ts": exp.DataType.build("timestamp"),
            "val": exp.DataType.build("int"),
        },
        unique_key=[exp.to_identifier("ID", quoted=True)],
        when_matched=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    source=False,
                    then=exp.Update(
                        expressions=[
                            exp.column("val", "__MERGE_TARGET__").eq(
                                exp.column("val", "__MERGE_SOURCE__")
                            ),
                            exp.column("ts", "__MERGE_TARGET__").eq(
                                exp.Coalesce(
                                    this=exp.column("ts", "__MERGE_SOURCE__"),
                                    expressions=[exp.column("ts", "__MERGE_TARGET__")],
                                )
                            ),
                        ],
                    ),
                )
            ]
        ),
        merge_filter=exp.And(
            this=exp.GT(
                this=exp.column("ID", "__MERGE_SOURCE__"),
                expression=exp.Literal(this="0", is_string=False),
            ),
            expression=exp.LT(
                this=exp.column("ts", "__MERGE_TARGET__"),
                expression=exp.Timestamp(this=exp.column("2020-02-05", quoted=True)),
            ),
        ),
    )

    assert_exp_eq(
        adapter.cursor.execute.call_args[0][0],
        """
MERGE INTO "target" AS "__MERGE_TARGET__"
USING (
    SELECT "ID", "ts", "val"
    FROM "source"
) AS "__MERGE_SOURCE__"
ON (
    "__MERGE_SOURCE__"."ID" > 0
    AND "__MERGE_TARGET__"."ts" < TIMESTAMP("2020-02-05")
)
AND "__MERGE_TARGET__"."ID" = "__MERGE_SOURCE__"."ID"
WHEN MATCHED THEN
    UPDATE SET
        "__MERGE_TARGET__"."val" = "__MERGE_SOURCE__"."val",
        "__MERGE_TARGET__"."ts" = COALESCE("__MERGE_SOURCE__"."ts", "__MERGE_TARGET__"."ts")
WHEN NOT MATCHED THEN
    INSERT ("ID", "ts", "val")
    VALUES (
        "__MERGE_SOURCE__"."ID",
        "__MERGE_SOURCE__"."ts",
        "__MERGE_SOURCE__"."val"
    );
""",
    )


def test_scd_type_2_by_time(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_time(
        target_table="target",
        source_table=t.cast(
            exp.Select, parse_one("SELECT id, name, price, test_UPDATED_at FROM source")
        ),
        unique_key=[
            parse_one("""COALESCE("id", '') || '|' || COALESCE("name", '')"""),
            parse_one("""COALESCE("name", '')"""),
        ],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        updated_at_col=exp.column("test_UPDATED_at", quoted=True),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_UPDATED_at": exp.DataType.build("TIMESTAMP"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON (COALESCE("id", '') || '|' || COALESCE("name", ''), COALESCE("name", ''))
    TRUE AS "_exists",
    "id",
    "name",
    "price",
    CAST("test_UPDATED_at" AS TIMESTAMP) AS "test_UPDATED_at"
  FROM (
    SELECT
      "id",
      "name",
      "price",
      "test_UPDATED_at"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_UPDATED_at",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON (
      COALESCE("static"."id", '') || '|' || COALESCE("static"."name", '')
    ) = (
      COALESCE("latest"."id", '') || '|' || COALESCE("latest"."name", '')
    )
    AND COALESCE("static"."name", '') = COALESCE("latest"."name", '')
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    COALESCE("id", '') || '|' || COALESCE("name", '') AS "_key0",
    COALESCE("name", '') AS "_key1",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    COALESCE("id", '') || '|' || COALESCE("name", ''),
    COALESCE("name", '')
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_UPDATED_at" AS "t_test_UPDATED_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_UPDATED_at" AS "test_UPDATED_at"
  FROM "latest"
  LEFT JOIN "source"
    ON (
      COALESCE("latest"."id", '') || '|' || COALESCE("latest"."name", '')
    ) = (
      COALESCE("source"."id", '') || '|' || COALESCE("source"."name", '')
    )
    AND COALESCE("latest"."name", '') = COALESCE("source"."name", '')
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_UPDATED_at" AS "t_test_UPDATED_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_UPDATED_at" AS "test_UPDATED_at"
  FROM "latest"
  RIGHT JOIN "source"
    ON (
      COALESCE("latest"."id", '') || '|' || COALESCE("latest"."name", '')
    ) = (
      COALESCE("source"."id", '') || '|' || COALESCE("source"."name", '')
    )
    AND COALESCE("latest"."name", '') = COALESCE("source"."name", '')
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("joined"."t_test_UPDATED_at", "joined"."test_UPDATED_at") AS "test_UPDATED_at",
    CASE
      WHEN "t_test_valid_from" IS NULL AND NOT "latest_deleted"."_exists" IS NULL
      THEN CASE
        WHEN "latest_deleted"."test_valid_to" > "test_UPDATED_at"
        THEN "latest_deleted"."test_valid_to"
        ELSE "test_UPDATED_at"
      END
      WHEN "t_test_valid_from" IS NULL
      THEN CAST('1970-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_from"
    END AS "test_valid_from",
    CASE
      WHEN "joined"."test_UPDATED_at" > "joined"."t_test_UPDATED_at"
      THEN "joined"."test_UPDATED_at"
      WHEN "joined"."_exists" IS NULL
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON (
      COALESCE("joined"."id", '') || '|' || COALESCE("joined"."name", '')
    ) = "latest_deleted"."_key0"
    AND COALESCE("joined"."name", '') = "latest_deleted"."_key1"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_UPDATED_at" AS "test_valid_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    "joined"."test_UPDATED_at" > "joined"."t_test_UPDATED_at"
)
SELECT
  CAST("id" AS INT) AS "id",
  CAST("name" AS VARCHAR) AS "name",
  CAST("price" AS DOUBLE) AS "price",
  CAST("test_UPDATED_at" AS TIMESTAMP) AS "test_UPDATED_at",
  CAST("test_valid_from" AS TIMESTAMP) AS "test_valid_from",
  CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to"
FROM (
  SELECT "id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to" FROM "static"
  UNION ALL SELECT "id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to" FROM "updated_rows"
  UNION ALL SELECT "id", "name", "price", "test_UPDATED_at", "test_valid_from", "test_valid_to" FROM "inserted_rows"
) AS "_subquery"
    """
        ).sql()
    )


def test_scd_type_2_by_time_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "name": ["a", "b", "c"],
            "test_UPDATED_at": [
                "2020-01-01 10:00:00",
                "2020-01-02 15:00:00",
                "2020-01-03 12:00:00",
            ],
            "ignored_source": [4, 5, 6],
        }
    )
    adapter.scd_type_2_by_time(
        target_table="target",
        source_table=df,
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        updated_at_col=exp.column("test_UPDATED_at", quoted=True),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_UPDATED_at": exp.DataType.build("TIMESTAMP"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        source_columns=["id", "name", "test_UPDATED_at", "ignored_source"],
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
        start=datetime(2020, 1, 1, 0, 0, 0),
        is_restatement=True,
    )
    sql_calls = to_sql_calls(adapter)
    assert (
        parse_one(sql_calls[1]).sql()
        == parse_one("""
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON ("id")
    TRUE AS "_exists",
    "id",
    "name",
    "price",
    CAST("test_UPDATED_at" AS TIMESTAMP) AS "test_UPDATED_at"
  FROM (
    SELECT
      CAST("id" AS INT) AS "id",
      CAST("name" AS VARCHAR) AS "name",
      CAST(NULL AS DOUBLE) AS "price",
      CAST("test_UPDATED_at" AS TIMESTAMP) AS "test_UPDATED_at"
    FROM (VALUES
      (1, 'a', '2020-01-01 10:00:00'),
      (2, 'b', '2020-01-02 15:00:00'),
      (3, 'c', '2020-01-03 12:00:00')) AS "t"("id", "name", "test_UPDATED_at")
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_UPDATED_at",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id" = "latest"."id"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id" AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_UPDATED_at" AS "t_test_UPDATED_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_UPDATED_at" AS "test_UPDATED_at"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id" = "source"."id"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_UPDATED_at" AS "t_test_UPDATED_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_UPDATED_at" AS "test_UPDATED_at"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id" = "source"."id"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("joined"."t_test_UPDATED_at", "joined"."test_UPDATED_at") AS "test_UPDATED_at",
    CASE
      WHEN "t_test_valid_from" IS NULL AND NOT "latest_deleted"."_exists" IS NULL
      THEN CASE
        WHEN "latest_deleted"."test_valid_to" > "test_UPDATED_at"
        THEN "latest_deleted"."test_valid_to"
        ELSE "test_UPDATED_at"
      END
      WHEN "t_test_valid_from" IS NULL
      THEN CAST('1970-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_from"
    END AS "test_valid_from",
    CASE
      WHEN "joined"."test_UPDATED_at" > "joined"."t_test_UPDATED_at"
      THEN "joined"."test_UPDATED_at"
      WHEN "joined"."_exists" IS NULL
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id" = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_UPDATED_at" AS "test_valid_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    "joined"."test_UPDATED_at" > "joined"."t_test_UPDATED_at"
)
SELECT
  CAST("id" AS INT) AS "id",
  CAST("name" AS VARCHAR) AS "name",
  CAST("price" AS DOUBLE) AS "price",
  CAST("test_UPDATED_at" AS TIMESTAMP) AS "test_UPDATED_at",
  CAST("test_valid_from" AS TIMESTAMP) AS "test_valid_from",
  CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to"
FROM (
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to"
  FROM "static"
  UNION ALL
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to"
  FROM "updated_rows"
  UNION ALL
  SELECT
    "id",
    "name",
    "price",
    "test_UPDATED_at",
    "test_valid_from",
    "test_valid_to"
  FROM "inserted_rows"
) AS "_subquery"
    """).sql()
    )


def test_scd_type_2_by_time_no_invalidate_hard_deletes(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_time(
        target_table="target",
        source_table=t.cast(
            exp.Select, parse_one("SELECT id, name, price, test_updated_at FROM source")
        ),
        unique_key=[exp.func("COALESCE", "id", "''")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        updated_at_col=exp.column("test_updated_at", quoted=True),
        invalidate_hard_deletes=False,
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_updated_at": exp.DataType.build("TIMESTAMP"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON (COALESCE("id", ''))
    TRUE AS "_exists",
    "id",
    "name",
    "price",
    CAST("test_updated_at" AS TIMESTAMP) AS "test_updated_at"
  FROM (
    SELECT
      "id",
      "name",
      "price",
      "test_updated_at"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_updated_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_updated_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_updated_at",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON COALESCE("static"."id", '') = COALESCE("latest"."id", '')
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    COALESCE("id", '') AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    COALESCE("id", '')
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_updated_at" AS "t_test_updated_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_updated_at" AS "test_updated_at"
  FROM "latest"
  LEFT JOIN "source"
    ON COALESCE("latest"."id", '') = COALESCE("source"."id", '')
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_updated_at" AS "t_test_updated_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_updated_at" AS "test_updated_at"
  FROM "latest"
  RIGHT JOIN "source"
    ON COALESCE("latest"."id", '') = COALESCE("source"."id", '')
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("joined"."t_test_updated_at", "joined"."test_updated_at") AS "test_updated_at",
    CASE
      WHEN "t_test_valid_from" IS NULL AND NOT "latest_deleted"."_exists" IS NULL
      THEN CASE
        WHEN "latest_deleted"."test_valid_to" > "test_updated_at"
        THEN "latest_deleted"."test_valid_to"
        ELSE "test_updated_at"
      END
      WHEN "t_test_valid_from" IS NULL
      THEN CAST('1970-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_from"
    END AS "test_valid_from",
    CASE
      WHEN "joined"."test_updated_at" > "joined"."t_test_updated_at"
      THEN "joined"."test_updated_at"
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON COALESCE("joined"."id", '') = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_updated_at",
    "test_updated_at" AS "test_valid_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    "joined"."test_updated_at" > "joined"."t_test_updated_at"
)
SELECT
  CAST("id" AS INT) AS "id",
  CAST("name" AS VARCHAR) AS "name",
  CAST("price" AS DOUBLE) AS "price",
  CAST("test_updated_at" AS TIMESTAMP) AS "test_updated_at",
  CAST("test_valid_from" AS TIMESTAMP) AS "test_valid_from",
  CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to"
FROM (
  SELECT "id", "name", "price", "test_updated_at", "test_valid_from", "test_valid_to" FROM "static"
  UNION ALL SELECT "id", "name", "price", "test_updated_at", "test_valid_from", "test_valid_to" FROM "updated_rows"
  UNION ALL SELECT "id", "name", "price", "test_updated_at", "test_valid_from", "test_valid_to" FROM "inserted_rows"
) AS "_subquery"
    """
        ).sql()
    )


def test_merge_scd_type_2_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    df = pd.DataFrame(
        {
            "id1": [1, 2, 3],
            "id2": [4, 5, 6],
            "name": ["muffins", "chips", "soda"],
            "price": [4.0, 5.0, 6.0],
            "test_updated_at": [
                "2020-01-01 10:00:00",
                "2020-01-02 15:00:00",
                "2020-01-03 12:00:00",
            ],
        }
    )
    adapter.scd_type_2_by_time(
        target_table="target",
        source_table=df,
        unique_key=[exp.column("id1"), exp.column("id2")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        updated_at_col=exp.column("test_updated_at", quoted=True),
        target_columns_to_types={
            "id1": exp.DataType.build("INT"),
            "id2": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_updated_at": exp.DataType.build("TIMESTAMPTZ"),
            "test_valid_from": exp.DataType.build("TIMESTAMPTZ"),
            "test_valid_to": exp.DataType.build("TIMESTAMPTZ"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON ("id1", "id2")
    TRUE AS "_exists",
    "id1",
    "id2",
    "name",
    "price",
    CAST("test_updated_at" AS TIMESTAMPTZ) AS "test_updated_at"
  FROM (
    SELECT
      CAST("id1" AS INT) AS "id1",
      CAST("id2" AS INT) AS "id2",
      CAST("name" AS VARCHAR) AS "name",
      CAST("price" AS DOUBLE) AS "price",
      CAST("test_updated_at" AS TIMESTAMPTZ) AS "test_updated_at",
    FROM (VALUES
      (1, 4, 'muffins', 4.0, '2020-01-01 10:00:00'),
      (2, 5, 'chips', 5.0, '2020-01-02 15:00:00'),
      (3, 6, 'soda', 6.0, '2020-01-03 12:00:00')) AS "t"("id1", "id2", "name", "price", "test_updated_at")
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id1",
    "id2",
    "name",
    "price",
    "test_updated_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id1",
    "id2",
    "name",
    "price",
    "test_updated_at",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id1",
    "static"."id2",
    "static"."name",
    "static"."price",
    "static"."test_updated_at",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id1" = "latest"."id1" AND "static"."id2" = "latest"."id2"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id1" AS "_key0",
    "id2" AS "_key1",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id1",
    "id2"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id1" AS "t_id1",
    "latest"."id2" AS "t_id2",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_updated_at" AS "t_test_updated_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id1" AS "id1",
    "source"."id2" AS "id2",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_updated_at" AS "test_updated_at"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id1" = "source"."id1" AND "latest"."id2" = "source"."id2"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id1" AS "t_id1",
    "latest"."id2" AS "t_id2",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_updated_at" AS "t_test_updated_at",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id1" AS "id1",
    "source"."id2" AS "id2",
    "source"."name" AS "name",
    "source"."price" AS "price",
    "source"."test_updated_at" AS "test_updated_at"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id1" = "source"."id1" AND "latest"."id2" = "source"."id2"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id1", "joined"."id1") AS "id1",
    COALESCE("joined"."t_id2", "joined"."id2") AS "id2",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("joined"."t_test_updated_at", "joined"."test_updated_at") AS "test_updated_at",
    CASE
      WHEN "t_test_valid_from" IS NULL AND NOT "latest_deleted"."_exists" IS NULL
      THEN CASE
        WHEN "latest_deleted"."test_valid_to" > "test_updated_at"
        THEN "latest_deleted"."test_valid_to"
        ELSE "test_updated_at"
      END
      WHEN "t_test_valid_from" IS NULL
      THEN CAST('1970-01-01 00:00:00+00:00' AS TIMESTAMPTZ)
      ELSE "t_test_valid_from"
    END AS "test_valid_from",
    CASE
      WHEN "joined"."test_updated_at" > "joined"."t_test_updated_at"
      THEN "joined"."test_updated_at"
      WHEN "joined"."_exists" IS NULL
      THEN CAST('2020-01-01 00:00:00+00:00' AS TIMESTAMPTZ)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id1" = "latest_deleted"."_key0"
    AND "joined"."id2" = "latest_deleted"."_key1"
), "inserted_rows" AS (
  SELECT
    "id1",
    "id2",
    "name",
    "price",
    "test_updated_at",
    "test_updated_at" AS "test_valid_from",
    CAST(NULL AS TIMESTAMPTZ) AS "test_valid_to"
  FROM "joined"
  WHERE
    "joined"."test_updated_at" > "joined"."t_test_updated_at"
)
SELECT CAST("id1" AS INT) AS "id1", CAST("id2" AS INT) AS "id2", CAST("name" AS VARCHAR) AS "name", CAST("price" AS DOUBLE) AS "price", CAST("test_updated_at" AS TIMESTAMPTZ) AS "test_updated_at", CAST("test_valid_from" AS TIMESTAMPTZ) AS "test_valid_from", CAST("test_valid_to" AS TIMESTAMPTZ) AS "test_valid_to" FROM (SELECT "id1", "id2", "name", "price", "test_updated_at", "test_valid_from", "test_valid_to" FROM "static" UNION ALL SELECT "id1", "id2", "name", "price", "test_updated_at", "test_valid_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id1", "id2", "name", "price", "test_updated_at", "test_valid_from", "test_valid_to" FROM "inserted_rows") AS "_subquery"
"""
        ).sql()
    )


def test_scd_type_2_by_column(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_column(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, name, price FROM source")),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("test_VALID_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        check_columns=[exp.column("name"), exp.column("price")],
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_VALID_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
        extra_col_ignore="testing",
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON ("id")
    TRUE AS "_exists",
    "id",
    "name",
    "price"
  FROM (
    SELECT
      "id",
      "name",
      "price"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_VALID_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_VALID_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_VALID_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id" = "latest"."id"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id" AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_VALID_from" AS "t_test_VALID_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id" = "source"."id"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_VALID_from" AS "t_test_VALID_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id" = "source"."id"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("t_test_VALID_from", CAST('2020-01-01 00:00:00' AS TIMESTAMP)) AS "test_VALID_from",
    CASE
      WHEN "joined"."_exists" IS NULL
      OR (
        (
          NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
        )
        AND (
          "joined"."name" <> "joined"."t_name"
          OR (
            "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
          )
          OR (
            NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
          )
          OR "joined"."price" <> "joined"."t_price"
          OR (
            "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
          )
          OR (
            NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
          )
        )
      )
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id" = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    CAST('2020-01-01 00:00:00' AS TIMESTAMP) AS "test_VALID_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    (
      NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
    )
    AND (
      "joined"."name" <> "joined"."t_name"
      OR (
        "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
      )
      OR (
        NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
      )
      OR "joined"."price" <> "joined"."t_price"
      OR (
        "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
      )
      OR (
        NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
      )
    )
)
SELECT CAST("id" AS INT) AS "id", CAST("name" AS VARCHAR) AS "name", CAST("price" AS DOUBLE) AS "price", CAST("test_VALID_from" AS TIMESTAMP) AS "test_VALID_from", CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to" FROM (SELECT "id", "name", "price", "test_VALID_from", "test_valid_to" FROM "static" UNION ALL SELECT "id", "name", "price", "test_VALID_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id", "name", "price", "test_VALID_from", "test_valid_to" FROM "inserted_rows") AS "_subquery"
    """
        ).sql()
    )


def test_scd_type_2_by_column_composite_key(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_column(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id_a, id_b, name, price FROM source")),
        unique_key=[exp.func("CONCAT", exp.column("id_a"), exp.column("id_b"))],
        valid_from_col=exp.column("test_VALID_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        check_columns=[exp.column("name"), exp.column("price")],
        target_columns_to_types={
            "id_a": exp.DataType.build("VARCHAR"),
            "id_b": exp.DataType.build("VARCHAR"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_VALID_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON (CONCAT("id_a", "id_b"))
    TRUE AS "_exists",
    "id_a",
    "id_b",
    "name",
    "price",
  FROM (
    SELECT
      "id_a",
      "id_b",
      "name",
      "price"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id_a",
    "id_b",
    "name",
    "price",
    "test_VALID_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id_a",
    "id_b",
    "name",
    "price",
    "test_VALID_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id_a",
    "static"."id_b",
    "static"."name",
    "static"."price",
    "static"."test_VALID_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON CONCAT("static"."id_a", "static"."id_b") = CONCAT("latest"."id_a", "latest"."id_b")
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    CONCAT("id_a", "id_b") AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    CONCAT("id_a", "id_b")
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id_a" AS "t_id_a",
    "latest"."id_b" AS "t_id_b",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_VALID_from" AS "t_test_VALID_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id_a" AS "id_a",
    "source"."id_b" AS "id_b",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  LEFT JOIN "source"
    ON CONCAT("latest"."id_a", "latest"."id_b") = CONCAT("source"."id_a", "source"."id_b")
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id_a" AS "t_id_a",
    "latest"."id_b" AS "t_id_b",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_VALID_from" AS "t_test_VALID_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id_a" AS "id_a",
    "source"."id_b" AS "id_b",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  RIGHT JOIN "source"
    ON CONCAT("latest"."id_a", "latest"."id_b") = CONCAT("source"."id_a", "source"."id_b")
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id_a", "joined"."id_a") AS "id_a",
    COALESCE("joined"."t_id_b", "joined"."id_b") AS "id_b",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("t_test_VALID_from", CAST('2020-01-01 00:00:00' AS TIMESTAMP)) AS "test_VALID_from",
    CASE
      WHEN "joined"."_exists" IS NULL
      OR (
        (
          NOT CONCAT("t_id_a", "t_id_b") IS NULL AND NOT CONCAT("id_a", "id_b") IS NULL
        )
        AND (
          "joined"."name" <> "joined"."t_name"
          OR (
            "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
          )
          OR (
            NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
          )
          OR "joined"."price" <> "joined"."t_price"
          OR (
            "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
          )
          OR (
            NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
          )
        )
      )
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON CONCAT("joined"."id_a", "joined"."id_b") = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id_a",
    "id_b",
    "name",
    "price",
    CAST('2020-01-01 00:00:00' AS TIMESTAMP) AS "test_VALID_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    (
      NOT CONCAT("t_id_a", "t_id_b") IS NULL AND NOT CONCAT("id_a", "id_b") IS NULL
    )
    AND (
      "joined"."name" <> "joined"."t_name"
      OR (
        "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
      )
      OR (
        NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
      )
      OR "joined"."price" <> "joined"."t_price"
      OR (
        "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
      )
      OR (
        NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
      )
    )
)
SELECT CAST("id_a" AS VARCHAR) AS "id_a", CAST("id_b" AS VARCHAR) AS "id_b", CAST("name" AS VARCHAR) AS "name", CAST("price" AS DOUBLE) AS "price", CAST("test_VALID_from" AS TIMESTAMP) AS "test_VALID_from", CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to" FROM (SELECT "id_a", "id_b", "name", "price", "test_VALID_from", "test_valid_to" FROM "static" UNION ALL SELECT "id_a", "id_b", "name", "price", "test_VALID_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id_a", "id_b", "name", "price", "test_VALID_from", "test_valid_to" FROM "inserted_rows") AS "_subquery"
    """
        ).sql()
    )


def test_scd_type_2_truncate(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_column(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, name, price FROM source")),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        check_columns=[exp.column("name"), exp.column("price")],
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
        truncate=True,
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON ("id")
    TRUE AS "_exists",
    "id",
    "name",
    "price"
  FROM (
    SELECT
      "id",
      "name",
      "price"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
  LIMIT 0
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
  LIMIT 0
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id" = "latest"."id"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id" AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id" = "source"."id"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id" = "source"."id"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("t_test_valid_from", CAST('1970-01-01 00:00:00' AS TIMESTAMP)) AS "test_valid_from",
    CASE
      WHEN "joined"."_exists" IS NULL
      OR (
        (
          NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
        )
        AND (
          "joined"."name" <> "joined"."t_name"
          OR (
            "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
          )
          OR (
            NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
          )
          OR "joined"."price" <> "joined"."t_price"
          OR (
            "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
          )
          OR (
            NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
          )
        )
      )
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id" = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    CAST('2020-01-01 00:00:00' AS TIMESTAMP) AS "test_valid_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    (
      NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
    )
    AND (
      "joined"."name" <> "joined"."t_name"
      OR (
        "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
      )
      OR (
        NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
      )
      OR "joined"."price" <> "joined"."t_price"
      OR (
        "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
      )
      OR (
        NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
      )
    )
)
SELECT CAST("id" AS INT) AS "id", CAST("name" AS VARCHAR) AS "name", CAST("price" AS DOUBLE) AS "price", CAST("test_valid_from" AS TIMESTAMP) AS "test_valid_from", CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to" FROM (SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "static" UNION ALL SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "inserted_rows") AS "_subquery"
    """
        ).sql()
    )


def test_scd_type_2_by_column_star_check(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_column(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, name, price FROM source")),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        check_columns=exp.Star(),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON ("id")
    TRUE AS "_exists",
    "id",
    "name",
    "price"
  FROM (
    SELECT
      "id",
      "name",
      "price"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id" = "latest"."id"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id" AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id" = "source"."id"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id" = "source"."id"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("t_test_valid_from", CAST('2020-01-01 00:00:00' AS TIMESTAMP)) AS "test_valid_from",
    CASE
      WHEN "joined"."_exists" IS NULL
      OR (
        (
          NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
        )
        AND (
          "joined"."id" <> "joined"."t_id"
          OR (
            "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
          )
          OR (
            NOT "joined"."t_id" IS NULL AND "joined"."id" IS NULL
          )
          OR "joined"."name" <> "joined"."t_name"
          OR (
            "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
          )
          OR (
            NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
          )
          OR "joined"."price" <> "joined"."t_price"
          OR (
            "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
          )
          OR (
            NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
          )
        )
      )
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id" = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    CAST('2020-01-01 00:00:00' AS TIMESTAMP) AS "test_valid_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    (
      NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
    )
    AND (
      "joined"."id" <> "joined"."t_id"
      OR (
        "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
      )
      OR (
        NOT "joined"."t_id" IS NULL AND "joined"."id" IS NULL
      )
      OR "joined"."name" <> "joined"."t_name"
      OR (
        "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
      )
      OR (
        NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
      )
      OR "joined"."price" <> "joined"."t_price"
      OR (
        "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
      )
      OR (
        NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
      )
    )
)
SELECT CAST("id" AS INT) AS "id", CAST("name" AS VARCHAR) AS "name", CAST("price" AS DOUBLE) AS "price", CAST("test_valid_from" AS TIMESTAMP) AS "test_valid_from", CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to" FROM (SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "static" UNION ALL SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "inserted_rows") AS "_subquery"
    """
        ).sql()
    )


def test_scd_type_2_by_column_no_invalidate_hard_deletes(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.scd_type_2_by_column(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, name, price FROM source")),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("test_valid_from", quoted=True),
        valid_to_col=exp.column("test_valid_to", quoted=True),
        invalidate_hard_deletes=False,
        check_columns=[exp.column("name"), exp.column("price")],
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
            "price": exp.DataType.build("DOUBLE"),
            "test_valid_from": exp.DataType.build("TIMESTAMP"),
            "test_valid_to": exp.DataType.build("TIMESTAMP"),
        },
        execution_time=datetime(2020, 1, 1, 0, 0, 0),
    )

    assert (
        adapter.cursor.execute.call_args[0][0]
        == parse_one(
            """
CREATE OR REPLACE TABLE "target" AS
WITH "source" AS (
  SELECT DISTINCT ON ("id")
    TRUE AS "_exists",
    "id",
    "name",
    "price"
  FROM (
    SELECT
      "id",
      "name",
      "price"
    FROM "source"
  ) AS "raw_source"
), "static" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    NOT "test_valid_to" IS NULL
), "latest" AS (
  SELECT
    "id",
    "name",
    "price",
    "test_valid_from",
    "test_valid_to",
    TRUE AS "_exists"
  FROM "target"
  WHERE
    "test_valid_to" IS NULL
), "deleted" AS (
  SELECT
    "static"."id",
    "static"."name",
    "static"."price",
    "static"."test_valid_from",
    "static"."test_valid_to"
  FROM "static"
  LEFT JOIN "latest"
    ON "static"."id" = "latest"."id"
  WHERE
    "latest"."test_valid_to" IS NULL
), "latest_deleted" AS (
  SELECT
    TRUE AS "_exists",
    "id" AS "_key0",
    MAX("test_valid_to") AS "test_valid_to"
  FROM "deleted"
  GROUP BY
    "id"
), "joined" AS (
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  LEFT JOIN "source"
    ON "latest"."id" = "source"."id"
  UNION ALL
  SELECT
    "source"."_exists" AS "_exists",
    "latest"."id" AS "t_id",
    "latest"."name" AS "t_name",
    "latest"."price" AS "t_price",
    "latest"."test_valid_from" AS "t_test_valid_from",
    "latest"."test_valid_to" AS "t_test_valid_to",
    "source"."id" AS "id",
    "source"."name" AS "name",
    "source"."price" AS "price"
  FROM "latest"
  RIGHT JOIN "source"
    ON "latest"."id" = "source"."id"
  WHERE
    "latest"."_exists" IS NULL
), "updated_rows" AS (
  SELECT
    COALESCE("joined"."t_id", "joined"."id") AS "id",
    COALESCE("joined"."t_name", "joined"."name") AS "name",
    COALESCE("joined"."t_price", "joined"."price") AS "price",
    COALESCE("t_test_valid_from", CAST('2020-01-01 00:00:00' AS TIMESTAMP)) AS "test_valid_from",
    CASE
      WHEN (
        NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
      )
      AND (
        "joined"."name" <> "joined"."t_name"
        OR (
          "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
        )
        OR (
          NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
        )
        OR "joined"."price" <> "joined"."t_price"
        OR (
          "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
        )
        OR (
          NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
        )
      )
      THEN CAST('2020-01-01 00:00:00' AS TIMESTAMP)
      ELSE "t_test_valid_to"
    END AS "test_valid_to"
  FROM "joined"
  LEFT JOIN "latest_deleted"
    ON "joined"."id" = "latest_deleted"."_key0"
), "inserted_rows" AS (
  SELECT
    "id",
    "name",
    "price",
    CAST('2020-01-01 00:00:00' AS TIMESTAMP) AS "test_valid_from",
    CAST(NULL AS TIMESTAMP) AS "test_valid_to"
  FROM "joined"
  WHERE
    (
      NOT "joined"."t_id" IS NULL AND NOT "joined"."id" IS NULL
    )
    AND (
      "joined"."name" <> "joined"."t_name"
      OR (
        "joined"."t_name" IS NULL AND NOT "joined"."name" IS NULL
      )
      OR (
        NOT "joined"."t_name" IS NULL AND "joined"."name" IS NULL
      )
      OR "joined"."price" <> "joined"."t_price"
      OR (
        "joined"."t_price" IS NULL AND NOT "joined"."price" IS NULL
      )
      OR (
        NOT "joined"."t_price" IS NULL AND "joined"."price" IS NULL
      )
    )
)
SELECT CAST("id" AS INT) AS "id", CAST("name" AS VARCHAR) AS "name", CAST("price" AS DOUBLE) AS "price", CAST("test_valid_from" AS TIMESTAMP) AS "test_valid_from", CAST("test_valid_to" AS TIMESTAMP) AS "test_valid_to" FROM (SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "static" UNION ALL SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "updated_rows" UNION ALL SELECT "id", "name", "price", "test_valid_from", "test_valid_to" FROM "inserted_rows") AS "_subquery"
    """
        ).sql()
    )


def test_replace_query(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    columns_mock = mocker.patch.object(adapter, "columns")
    columns_mock.return_value = None

    adapter.replace_query(
        "test_table", parse_one("SELECT a FROM tbl"), {"a": exp.DataType.build("INT")}
    )
    adapter.replace_query("test_table", parse_one("SELECT a FROM tbl"))
    adapter.replace_query(
        "test_table", parse_one("SELECT a FROM tbl"), {"a": exp.DataType.build("UNKNOWN")}
    )

    # TODO: Shouldn't we enforce that `a` is casted to an int?
    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE TABLE "test_table" AS SELECT CAST("a" AS INT) AS "a" FROM (SELECT "a" FROM "tbl") AS "_subquery"',
        'CREATE OR REPLACE TABLE "test_table" AS SELECT "a" FROM "tbl"',
        'CREATE OR REPLACE TABLE "test_table" AS SELECT "a" FROM "tbl"',
    ]


def test_replace_query_data_object_type_mismatch(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[DataObject(schema="", name="test_table", type="view")],
    )

    adapter.replace_query(
        "test_table", parse_one("SELECT a FROM tbl"), {"a": exp.DataType.build("INT")}
    )

    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "test_table"',
        'CREATE OR REPLACE TABLE "test_table" AS SELECT CAST("a" AS INT) AS "a" FROM (SELECT "a" FROM "tbl") AS "_subquery"',
    ]


def test_replace_query_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.DEFAULT_BATCH_SIZE = 1

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.replace_query(
        "test_table", df, {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")}
    )

    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE TABLE "test_table" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (1, 4)) AS "t"("a", "b")) AS "_subquery"',
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (2, 5)) AS "t"("a", "b")) AS "_subquery"',
        'INSERT INTO "test_table" ("a", "b") SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (VALUES (3, 6)) AS "t"("a", "b")) AS "_subquery"',
    ]


def test_replace_query_pandas_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    df = pd.DataFrame({"a": [1, 2, 3], "ignored_source": [4, 5, 6]})
    adapter.replace_query(
        "test_table",
        df,
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE TABLE "test_table" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT CAST("a" AS INT) AS "a", CAST(NULL AS INT) AS "b" FROM (VALUES (1), (2), (3)) AS "t"("a")) AS "_subquery"',
    ]


def test_replace_query_query_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.replace_query(
        "test_table",
        parse_one("SELECT a, ignored_source FROM tbl"),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE TABLE "test_table" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", CAST(NULL AS INT) AS "b" FROM (SELECT "a", "ignored_source" FROM "tbl") AS "select_source_columns") AS "_subquery"',
    ]


def test_replace_query_self_referencing_not_exists_unknown(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.table_exists",
        return_value=False,
    )

    with pytest.raises(
        SQLMeshError,
        match="Cannot create a table without knowing the column types.*",
    ):
        adapter.replace_query(
            "test",
            parse_one("SELECT a FROM test"),
            target_columns_to_types={"a": exp.DataType.build("UNKNOWN")},
        )


def test_replace_query_self_referencing_exists(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.table_exists",
        return_value=True,
    )

    adapter.replace_query(
        "test",
        parse_one("SELECT a FROM test"),
        target_columns_to_types={"a": exp.DataType.build("UNKNOWN")},
    )

    assert to_sql_calls(adapter) == [
        'CREATE OR REPLACE TABLE "test" AS SELECT "a" FROM "test"',
    ]


def test_replace_query_self_referencing_not_exists_known(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.table_exists",
        return_value=False,
    )

    adapter.replace_query(
        "test",
        parse_one("SELECT a FROM test"),
        target_columns_to_types={"a": exp.DataType.build("INT")},
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test" ("a" INT)',
        'CREATE OR REPLACE TABLE "test" AS SELECT CAST("a" AS INT) AS "a" FROM (SELECT "a" FROM "test") AS "_subquery"',
    ]


def test_create_table_like(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }
    columns_mock = mocker.patch.object(adapter, "columns")
    columns_mock.return_value = columns_to_types

    adapter.create_table_like("target_table", "source_table")
    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "target_table" ("cola" INT, "colb" TEXT)'
    )


def test_create_table_primary_key(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapterWithIndexSupport)

    columns_to_types = {
        "cola": exp.DataType.build("INT"),
        "colb": exp.DataType.build("TEXT"),
    }
    adapter.create_table("test_table", columns_to_types, primary_key=("cola", "colb"))

    adapter.cursor.execute.assert_called_once_with(
        'CREATE TABLE IF NOT EXISTS "test_table" ("cola" INT, "colb" TEXT, PRIMARY KEY ("cola", "colb"))'
    )


def test_create_index(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapterWithIndexSupport)
    adapter.SUPPORTS_INDEXES = True

    adapter.create_index("test_table", "test_index", ("cola", "colb"))
    adapter.cursor.execute.assert_called_once_with(
        'CREATE INDEX IF NOT EXISTS "test_index" ON "test_table"("cola", "colb")'
    )


def test_rename_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.rename_table("old_table", "new_table")
    adapter.cursor.execute.assert_called_once_with('ALTER TABLE "old_table" RENAME TO "new_table"')


def test_clone_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter, dialect="bigquery")

    with pytest.raises(NotImplementedError):
        adapter.clone_table("target_table", "source_table")

    adapter.SUPPORTS_CLONING = True

    adapter.clone_table("target_table", "source_table")

    adapter.cursor.execute.assert_called_once_with(
        "CREATE TABLE IF NOT EXISTS `target_table` CLONE `source_table`"
    )


def test_ctas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.ctas("new_table", parse_one("SELECT * FROM old_table"))

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "new_table" AS SELECT * FROM "old_table"'
    ]


def test_ctas_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    # Inferring the types from the dataframe
    adapter.ctas("new_table", df)

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "new_table" AS SELECT CAST("a" AS BIGINT) AS "a", CAST("b" AS BIGINT) AS "b" FROM (SELECT CAST("a" AS BIGINT) AS "a", CAST("b" AS BIGINT) AS "b" FROM (VALUES (1, 4), (2, 5), (3, 6)) AS "t"("a", "b")) AS "_subquery"'
    ]


def test_ctas_pandas_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    df = pd.DataFrame({"a": [1, 2, 3], "ignored_source": [4, 5, 6]})
    adapter.ctas(
        "test_table",
        df,
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_table" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT CAST("a" AS INT) AS "a", CAST(NULL AS INT) AS "b" FROM (VALUES (1), (2), (3)) AS "t"("a")) AS "_subquery"',
    ]


def test_ctas_query_source_columns(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.ctas(
        "test_table",
        parse_one("SELECT a, ignored_source FROM tbl"),
        target_columns_to_types={
            "a": exp.DataType.build("INT"),
            "b": exp.DataType.build("INT"),
        },
        source_columns=["a", "ignored_source"],
    )
    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "test_table" AS SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" FROM (SELECT "a", CAST(NULL AS INT) AS "b" FROM (SELECT "a", "ignored_source" FROM "tbl") AS "select_source_columns") AS "_subquery"',
    ]


def test_drop_view(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    adapter.drop_view("test_view")
    adapter.drop_view("test_view", materialized=True)
    adapter.SUPPORTS_MATERIALIZED_VIEWS = True
    adapter.drop_view("test_view", materialized=True)

    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "test_view"',
        'DROP VIEW IF EXISTS "test_view"',
        'DROP MATERIALIZED VIEW IF EXISTS "test_view"',
    ]


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
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["SCHEMA"]

    adapter.drop_schema(**kwargs)

    assert to_sql_calls(adapter) == ensure_list(expected)


def test_drop_schema_catalog(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    with pytest.raises(UnsupportedCatalogOperationError):
        adapter.drop_schema("test_catalog.test_schema")


def test_get_current_catalog(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    with pytest.raises(NotImplementedError):
        adapter.get_current_catalog()


def test_get_temp_table(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    mocker.patch("sqlmesh.core.engine_adapter.base.random_id", return_value="abcdefgh")

    value = adapter._get_temp_table(
        normalize_model_name("catalog.db.test_table", default_catalog=None, dialect=None)
    )

    assert value.sql() == '"catalog"."db"."__temp_test_table_abcdefgh"'


def test_get_data_objects_batching(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter._get_data_objects = mocker.Mock(return_value=[])

    names = {f"name_{i}" for i in range(adapter.DATA_OBJECT_FILTER_BATCH_SIZE + 1)}
    adapter.get_data_objects("test_schema", names)

    calls = adapter._get_data_objects.call_args_list
    assert len(calls) == 2

    assert calls[0][0][0] == "test_schema"
    assert calls[1][0][0] == "test_schema"

    assert len(calls[0][0][1]) == adapter.DATA_OBJECT_FILTER_BATCH_SIZE
    assert len(calls[1][0][1]) == 1


def test_pre_ping(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter._pre_ping = True
    adapter.cursor.execute.side_effect = RuntimeError("error")

    with pytest.raises(RuntimeError):
        adapter.execute("SELECT 'test'")

    assert to_sql_calls(adapter) == [
        "SELECT 1",  # ping
        "SELECT 'test'",
    ]

    adapter._connection_pool.get().close.assert_called_once()


def test_with_settings_preserves_pre_ping(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter, pre_ping=True)
    assert adapter.with_settings()._pre_ping is True
    assert adapter.with_settings(pre_ping=False)._pre_ping is False

    adapter = make_mocked_engine_adapter(EngineAdapter)
    assert adapter.with_settings()._pre_ping is False


@pytest.mark.parametrize(
    "partitioned_by",
    [
        ["DATETIME_TRUNC(ds, MONTH)", "b"],
        ["b"],
    ],
)
def test_insert_overwrite_by_partition_query(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
    make_temp_table_name: t.Callable,
    partitioned_by: t.List[str],
):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "test_schema.test_table"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    adapter.insert_overwrite_by_partition(
        table_name,
        parse_one("SELECT a, ds, b FROM tbl"),
        partitioned_by=[d.parse_one(k) for k in partitioned_by],
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("DATETIME"),
            "b": exp.DataType.build("boolean"),
        },
    )

    expected_delete_stmt = (
        'DELETE FROM "test_schema"."test_table" WHERE CONCAT_WS(\'__SQLMESH_DELIM__\', DATETIME_TRUNC("ds", MONTH), "b") IN (SELECT DISTINCT CONCAT_WS(\'__SQLMESH_DELIM__\', DATETIME_TRUNC("ds", MONTH), "b") FROM "test_schema"."__temp_test_table_abcdefgh")'
        if len(partitioned_by) > 1
        else 'DELETE FROM "test_schema"."test_table" WHERE "b" IN (SELECT DISTINCT "b" FROM "test_schema"."__temp_test_table_abcdefgh")'
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'CREATE TABLE "test_schema"."__temp_test_table_abcdefgh" AS SELECT CAST("a" AS INT) AS "a", CAST("ds" AS DATETIME) AS "ds", CAST("b" AS BOOLEAN) AS "b" FROM (SELECT "a", "ds", "b" FROM "tbl") AS "_subquery"',
        expected_delete_stmt,
        'INSERT INTO "test_schema"."test_table" ("a", "ds", "b") SELECT "a", "ds", "b" FROM "test_schema"."__temp_test_table_abcdefgh"',
        'DROP TABLE IF EXISTS "test_schema"."__temp_test_table_abcdefgh"',
    ]


def test_insert_overwrite_by_partition_query_insert_overwrite_strategy(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture, make_temp_table_name: t.Callable
):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE

    temp_table_mock = mocker.patch("sqlmesh.core.engine_adapter.EngineAdapter._get_temp_table")
    table_name = "test_schema.test_table"
    temp_table_id = "abcdefgh"
    temp_table_mock.return_value = make_temp_table_name(table_name, temp_table_id)

    adapter.insert_overwrite_by_partition(
        table_name,
        parse_one("SELECT a, ds, b FROM tbl"),
        partitioned_by=[
            d.parse_one("DATETIME_TRUNC(ds, MONTH)"),
            d.parse_one("b"),
        ],
        target_columns_to_types={
            "a": exp.DataType.build("int"),
            "ds": exp.DataType.build("DATETIME"),
            "b": exp.DataType.build("boolean"),
        },
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        'INSERT OVERWRITE TABLE "test_schema"."test_table" ("a", "ds", "b") SELECT "a", "ds", "b" FROM "tbl"'
    ]


def test_log_sql(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(EngineAdapter)

    mock_logger = mocker.patch("sqlmesh.core.engine_adapter.base.logger")

    df = pd.DataFrame({"id": [1, 2, 3], "value": ["test1", "test2", "test3"]})

    adapter.execute(parse_one("SELECT 1"))
    adapter.execute(parse_one("INSERT INTO test SELECT * FROM source"))
    adapter.execute(parse_one("INSERT INTO test (id, value) VALUES (1, 'test')"))
    adapter.insert_append("test", df)
    adapter.replace_query("test", df)

    assert mock_logger.log.call_count == 5
    assert mock_logger.log.call_args_list[0][0][2] == "SELECT 1"
    assert mock_logger.log.call_args_list[1][0][2] == 'INSERT INTO "test" SELECT * FROM "source"'
    assert (
        mock_logger.log.call_args_list[2][0][2]
        == 'INSERT INTO "test" ("id", "value") VALUES "<REDACTED VALUES>"'
    )
    assert (
        mock_logger.log.call_args_list[3][0][2]
        == 'INSERT INTO "test" ("id", "value") SELECT CAST("id" AS BIGINT) AS "id", CAST("value" AS TEXT) AS "value" FROM (VALUES "<REDACTED VALUES>") AS "t"("id", "value")'
    )
    assert (
        mock_logger.log.call_args_list[4][0][2]
        == 'CREATE OR REPLACE TABLE "test" AS SELECT CAST("id" AS BIGINT) AS "id", CAST("value" AS TEXT) AS "value" FROM (SELECT CAST("id" AS BIGINT) AS "id", CAST("value" AS TEXT) AS "value" FROM (VALUES "<REDACTED VALUES>") AS "t"("id", "value")) AS "_subquery"'
    )


@pytest.mark.parametrize(
    "columns, source_columns, expected",
    [
        (["a", "b"], None, 'SELECT "a", "b"'),
        (["a", "b"], ["a"], 'SELECT "a", NULL AS "b"'),
        (["a", "b"], ["a", "b"], 'SELECT "a", "b"'),
        (["a", "b"], ["c", "d"], 'SELECT NULL AS "a", NULL AS "b"'),
        (["a", "b"], [], 'SELECT "a", "b"'),
    ],
)
def test_select_columns(
    columns: t.List[str], source_columns: t.Optional[t.List[str]], expected: str
) -> None:
    assert (
        EngineAdapter._select_columns(
            columns,
            source_columns,
        ).sql()
        == expected
    )


@pytest.mark.parametrize(
    "columns_to_types, source_columns, expected",
    [
        (
            {
                "a": exp.DataType.build("INT"),
                "b": exp.DataType.build("TEXT"),
            },
            None,
            [
                'CAST("a" AS INT) AS "a"',
                'CAST("b" AS TEXT) AS "b"',
            ],
        ),
        (
            {
                "a": exp.DataType.build("INT"),
                "b": exp.DataType.build("TEXT"),
            },
            ["a"],
            [
                'CAST("a" AS INT) AS "a"',
                'CAST(NULL AS TEXT) AS "b"',
            ],
        ),
        (
            {
                "a": exp.DataType.build("INT"),
                "b": exp.DataType.build("TEXT"),
            },
            ["b", "c"],
            [
                'CAST(NULL AS INT) AS "a"',
                'CAST("b" AS TEXT) AS "b"',
            ],
        ),
    ],
)
def test_casted_columns(
    columns_to_types: t.Dict[str, exp.DataType], source_columns: t.List[str], expected: t.List[str]
) -> None:
    assert [
        x.sql() for x in EngineAdapter._casted_columns(columns_to_types, source_columns)
    ] == expected


def test_data_object_cache_get_data_objects(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table1 = DataObject(catalog=None, schema="test_schema", name="table1", type="table")
    table2 = DataObject(catalog=None, schema="test_schema", name="table2", type="table")

    mock_get_data_objects = mocker.patch.object(
        adapter, "_get_data_objects", return_value=[table1, table2]
    )

    result1 = adapter.get_data_objects("test_schema", {"table1", "table2"}, safe_to_cache=True)
    assert len(result1) == 2
    assert mock_get_data_objects.call_count == 1

    result2 = adapter.get_data_objects("test_schema", {"table1", "table2"}, safe_to_cache=True)
    assert len(result2) == 2
    assert mock_get_data_objects.call_count == 1  # Should not increase

    result3 = adapter.get_data_objects("test_schema", {"table1"})
    assert len(result3) == 1
    assert result3[0].name == "table1"
    assert mock_get_data_objects.call_count == 1  # Should not increase


def test_data_object_cache_get_data_objects_bypasses_cache(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table1 = DataObject(catalog=None, schema="test_schema", name="table1", type="table")
    table2 = DataObject(catalog=None, schema="test_schema", name="table2", type="table")

    mock_get_data_objects = mocker.patch.object(
        adapter, "_get_data_objects", return_value=[table1, table2]
    )

    assert adapter.get_data_objects("test_schema")
    assert adapter.get_data_objects("test_schema", {"table1", "table2"})
    assert adapter.get_data_objects("test_schema", {"table1", "table2"})
    assert adapter.get_data_objects("test_schema", {"table1"})
    assert adapter.get_data_object("test_schema.table1") is not None

    mock_get_data_objects.return_value = []
    assert not adapter.get_data_objects("test_schema")
    assert not adapter.get_data_objects("test_schema", {"missing"})
    assert not adapter.get_data_objects("test_schema", {"missing"})
    assert adapter.get_data_object("test_schema.missing") is None

    # None of the calls should've been cached
    assert mock_get_data_objects.call_count == 9
    assert not adapter._data_object_cache


def test_data_object_cache_get_data_objects_no_object_names(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table1 = DataObject(catalog=None, schema="test_schema", name="table1", type="table")
    table2 = DataObject(catalog=None, schema="test_schema", name="table2", type="table")

    mock_get_data_objects = mocker.patch.object(
        adapter, "_get_data_objects", return_value=[table1, table2]
    )

    result1 = adapter.get_data_objects("test_schema", safe_to_cache=True)
    assert len(result1) == 2
    assert mock_get_data_objects.call_count == 1

    result2 = adapter.get_data_objects("test_schema", {"table1", "table2"}, safe_to_cache=True)
    assert len(result2) == 2
    assert mock_get_data_objects.call_count == 1  # Should not increase


def test_data_object_cache_get_data_object(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table = DataObject(catalog=None, schema="test_schema", name="test_table", type="table")

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[table])

    result1 = adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert result1 is not None
    assert result1.name == "test_table"
    assert mock_get_data_objects.call_count == 1

    result2 = adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert result2 is not None
    assert result2.name == "test_table"
    assert mock_get_data_objects.call_count == 1  # Should not increase


def test_data_object_cache_cleared_on_drop_table(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table = DataObject(catalog=None, schema="test_schema", name="test_table", type="table")

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[table])

    adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert mock_get_data_objects.call_count == 1

    adapter.drop_table("test_schema.test_table")

    mock_get_data_objects.return_value = []
    result = adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 2


def test_data_object_cache_cleared_on_drop_view(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    view = DataObject(catalog=None, schema="test_schema", name="test_view", type="view")

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[view])

    adapter.get_data_object("test_schema.test_view", safe_to_cache=True)
    assert mock_get_data_objects.call_count == 1

    adapter.drop_view("test_schema.test_view")

    mock_get_data_objects.return_value = []
    result = adapter.get_data_object("test_schema.test_view", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 2


def test_data_object_cache_cleared_on_drop_data_object(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table = DataObject(catalog=None, schema="test_schema", name="test_table", type="table")

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[table])

    adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert mock_get_data_objects.call_count == 1

    adapter.drop_data_object(table)

    mock_get_data_objects.return_value = []
    result = adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 2


def test_data_object_cache_cleared_on_create_table(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    from sqlglot import exp

    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    # Initially cache that table doesn't exist
    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[])
    result = adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 1

    # Create the table
    table = DataObject(catalog=None, schema="test_schema", name="test_table", type="table")
    mock_get_data_objects.return_value = [table]
    adapter.create_table(
        "test_schema.test_table",
        {"col1": exp.DataType.build("INT")},
    )

    # Cache should be cleared, so next get_data_object should call _get_data_objects again
    result = adapter.get_data_object("test_schema.test_table", safe_to_cache=True)
    assert result is not None
    assert mock_get_data_objects.call_count == 2


def test_data_object_cache_cleared_on_create_view(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    from sqlglot import parse_one

    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    # Initially cache that view doesn't exist
    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[])
    result = adapter.get_data_object("test_schema.test_view", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 1

    # Create the view
    view = DataObject(catalog=None, schema="test_schema", name="test_view", type="view")
    mock_get_data_objects.return_value = [view]
    adapter.create_view(
        "test_schema.test_view",
        parse_one("SELECT 1 AS col1"),
    )

    # Cache should be cleared, so next get_data_object should call _get_data_objects again
    result = adapter.get_data_object("test_schema.test_view", safe_to_cache=True)
    assert result is not None
    assert mock_get_data_objects.call_count == 2


def test_data_object_cache_cleared_on_clone_table(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    from sqlmesh.core.engine_adapter.snowflake import SnowflakeEngineAdapter

    adapter = make_mocked_engine_adapter(
        SnowflakeEngineAdapter, patch_get_data_objects=False, default_catalog="test_catalog"
    )

    # Initially cache that target table doesn't exist
    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[])
    result = adapter.get_data_object("test_schema.test_target", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 1

    # Clone the table
    target_table = DataObject(
        catalog="test_catalog", schema="test_schema", name="test_target", type="table"
    )
    mock_get_data_objects.return_value = [target_table]
    adapter.clone_table("test_schema.test_target", "test_schema.test_source")

    # Cache should be cleared, so next get_data_object should call _get_data_objects again
    result = adapter.get_data_object("test_schema.test_target", safe_to_cache=True)
    assert result is not None
    assert mock_get_data_objects.call_count == 2


def test_data_object_cache_with_catalog(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    from sqlmesh.core.engine_adapter.snowflake import SnowflakeEngineAdapter

    adapter = make_mocked_engine_adapter(
        SnowflakeEngineAdapter, patch_get_data_objects=False, default_catalog="test_catalog"
    )

    table = DataObject(
        catalog="test_catalog", schema="test_schema", name="test_table", type="table"
    )

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[table])

    result1 = adapter.get_data_object("test_catalog.test_schema.test_table", safe_to_cache=True)
    assert result1 is not None
    assert result1.catalog == "test_catalog"
    assert mock_get_data_objects.call_count == 1

    result2 = adapter.get_data_object("test_catalog.test_schema.test_table", safe_to_cache=True)
    assert result2 is not None
    assert result2.catalog == "test_catalog"
    assert mock_get_data_objects.call_count == 1  # Should not increase


def test_data_object_cache_partial_cache_hit(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table1 = DataObject(catalog=None, schema="test_schema", name="table1", type="table")
    table2 = DataObject(catalog=None, schema="test_schema", name="table2", type="table")
    table3 = DataObject(catalog=None, schema="test_schema", name="table3", type="table")

    mock_get_data_objects = mocker.patch.object(
        adapter, "_get_data_objects", return_value=[table1, table2]
    )

    adapter.get_data_objects("test_schema", {"table1", "table2"}, safe_to_cache=True)
    assert mock_get_data_objects.call_count == 1

    mock_get_data_objects.return_value = [table3]
    result = adapter.get_data_objects("test_schema", {"table1", "table3"}, safe_to_cache=True)

    assert len(result) == 2
    assert {obj.name for obj in result} == {"table1", "table3"}
    assert mock_get_data_objects.call_count == 2  # Called again for table3


def test_data_object_cache_get_data_objects_missing_objects(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    table1 = DataObject(catalog=None, schema="test_schema", name="table1", type="table")
    table2 = DataObject(catalog=None, schema="test_schema", name="table2", type="table")

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[])

    result1 = adapter.get_data_objects("test_schema", {"table1", "table2"}, safe_to_cache=True)
    assert not result1
    assert mock_get_data_objects.call_count == 1

    result2 = adapter.get_data_objects("test_schema", {"table1", "table2"}, safe_to_cache=True)
    assert not result2
    assert mock_get_data_objects.call_count == 1  # Should not increase

    result3 = adapter.get_data_objects("test_schema", {"table1"}, safe_to_cache=True)
    assert not result3
    assert mock_get_data_objects.call_count == 1  # Should not increase


def test_data_object_cache_cleared_on_rename_table(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    old_table = DataObject(catalog=None, schema="test_schema", name="old_table", type="table")
    mock_get_data_objects = mocker.patch.object(
        adapter, "_get_data_objects", return_value=[old_table]
    )

    result = adapter.get_data_object("test_schema.old_table", safe_to_cache=True)
    assert result is not None
    assert result.name == "old_table"
    assert mock_get_data_objects.call_count == 1

    new_table = DataObject(catalog=None, schema="test_schema", name="new_table", type="table")
    mock_get_data_objects.return_value = [new_table]
    adapter.rename_table("test_schema.old_table", "test_schema.new_table")

    mock_get_data_objects.return_value = []
    result = adapter.get_data_object("test_schema.old_table", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 2

    mock_get_data_objects.return_value = [new_table]
    result = adapter.get_data_object("test_schema.new_table", safe_to_cache=True)
    assert result is not None
    assert result.name == "new_table"
    assert mock_get_data_objects.call_count == 3


def test_data_object_cache_cleared_on_create_table_like(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    from sqlglot import exp

    adapter = make_mocked_engine_adapter(EngineAdapter, patch_get_data_objects=False)

    columns_to_types = {
        "col1": exp.DataType.build("INT"),
        "col2": exp.DataType.build("TEXT"),
    }
    mocker.patch.object(adapter, "columns", return_value=columns_to_types)

    mock_get_data_objects = mocker.patch.object(adapter, "_get_data_objects", return_value=[])
    result = adapter.get_data_object("test_schema.target_table", safe_to_cache=True)
    assert result is None
    assert mock_get_data_objects.call_count == 1

    target_table = DataObject(catalog=None, schema="test_schema", name="target_table", type="table")
    mock_get_data_objects.return_value = [target_table]
    adapter.create_table_like("test_schema.target_table", "test_schema.source_table")

    result = adapter.get_data_object("test_schema.target_table", safe_to_cache=True)
    assert result is not None
    assert result.name == "target_table"
    assert mock_get_data_objects.call_count == 2


def test_diff_grants_configs():
    new = {"SELECT": ["u1", "u2"], "INSERT": ["u1"]}
    old = {"SELECT": ["u1", "u3"], "update": ["u1"]}

    additions, removals = EngineAdapter._diff_grants_configs(new, old)

    assert additions.get("SELECT") and set(additions["SELECT"]) == {"u2"}
    assert removals.get("SELECT") and set(removals["SELECT"]) == {"u3"}

    assert additions.get("INSERT") and set(additions["INSERT"]) == {"u1"}
    assert removals.get("update") and set(removals["update"]) == {"u1"}

    for perm, grantees in additions.items():
        assert set(grantees).isdisjoint(set(old.get(perm, [])))
    for perm, grantees in removals.items():
        assert set(grantees).isdisjoint(set(new.get(perm, [])))


def test_diff_grants_configs_empty_new():
    new = {}
    old = {"SELECT": ["u1", "u2"], "INSERT": ["u3"]}

    additions, removals = EngineAdapter._diff_grants_configs(new, old)

    assert additions == {}
    assert removals == old


def test_diff_grants_configs_empty_old():
    new = {"SELECT": ["u1", "u2"], "INSERT": ["u3"]}
    old = {}

    additions, removals = EngineAdapter._diff_grants_configs(new, old)

    assert additions == new
    assert removals == {}


def test_diff_grants_configs_identical():
    grants = {"SELECT": ["u1", "u2"], "INSERT": ["u3"]}

    additions, removals = EngineAdapter._diff_grants_configs(grants, grants)

    assert additions == {}
    assert removals == {}


def test_diff_grants_configs_none_configs():
    grants = {"SELECT": ["u1"]}

    additions, removals = EngineAdapter._diff_grants_configs(grants, {})
    assert additions == grants
    assert removals == {}

    additions, removals = EngineAdapter._diff_grants_configs({}, grants)
    assert additions == {}
    assert removals == grants

    additions, removals = EngineAdapter._diff_grants_configs({}, {})
    assert additions == {}
    assert removals == {}


def test_diff_grants_configs_duplicate_grantees():
    new = {"SELECT": ["u1", "u2", "u1"]}
    old = {"SELECT": ["u2", "u3", "u2"]}

    additions, removals = EngineAdapter._diff_grants_configs(new, old)

    assert additions["SELECT"] == ["u1", "u1"]
    assert removals["SELECT"] == ["u3"]


def test_diff_grants_configs_case_sensitive():
    new = {"select": ["u1"], "SELECT": ["u2"]}
    old = {"Select": ["u3"]}

    additions, removals = EngineAdapter._diff_grants_configs(new, old)

    assert set(additions.keys()) == {"select", "SELECT"}
    assert set(removals.keys()) == {"Select"}
    assert additions["select"] == ["u1"]
    assert additions["SELECT"] == ["u2"]
    assert removals["Select"] == ["u3"]


def test_sync_grants_config_unsupported_engine(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    adapter.SUPPORTS_GRANTS = False

    relation = exp.to_table("test_table")
    grants_config = {"SELECT": ["user1"]}

    with pytest.raises(NotImplementedError, match="Engine does not support grants"):
        adapter.sync_grants_config(relation, grants_config)


def test_get_current_grants_config_not_implemented(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(EngineAdapter)
    relation = exp.to_table("test_table")

    with pytest.raises(NotImplementedError):
        adapter._get_current_grants_config(relation)
