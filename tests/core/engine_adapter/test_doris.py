# SPDX-License-Identifier: Apache-2.0
import typing as t

import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import expressions as exp
from sqlglot import parse_one

from sqlmesh.core.engine_adapter.doris import DorisEngineAdapter
from sqlmesh.core.engine_adapter.shared import CatalogSupport, DataObject, DataObjectType
from sqlmesh.utils.errors import SQLMeshError
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.doris, pytest.mark.engine]


def test_create_view(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"))
    adapter.create_view("test_view", parse_one("SELECT a FROM tbl"), replace=False)
    # check view properties
    adapter.create_view(
        "test_view",
        parse_one("SELECT a FROM tbl"),
        replace=False,
        view_properties={"a": exp.convert(1)},
    )

    assert to_sql_calls(adapter) == [
        "CREATE OR REPLACE VIEW `test_view` AS SELECT `a` FROM `tbl`",
        "CREATE VIEW `test_view` AS SELECT `a` FROM `tbl`",
        "CREATE VIEW `test_view` AS SELECT `a` FROM `tbl`",
    ]


def test_create_view_with_comment(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_view(
        "test_view",
        parse_one("SELECT a FROM tbl"),
        replace=True,
        target_columns_to_types={"a": exp.DataType.build("INT")},
        table_description="test_description",
        column_descriptions={"a": "test_column_description"},
    )
    adapter.create_view(
        "test_view",
        parse_one("SELECT a FROM tbl"),
        replace=False,
        target_columns_to_types={"a": exp.DataType.build("INT")},
        table_description="test_description",
        column_descriptions={"a": "test_column_description"},
    )

    assert to_sql_calls(adapter) == [
        "CREATE OR REPLACE VIEW `test_view` (`a` COMMENT 'test_column_description') COMMENT 'test_description' AS SELECT `a` FROM `tbl`",
        "CREATE VIEW `test_view` (`a` COMMENT 'test_column_description') COMMENT 'test_description' AS SELECT `a` FROM `tbl`",
    ]


def test_create_materialized_view(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
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

    assert to_sql_calls(adapter) == [
        "DROP MATERIALIZED VIEW IF EXISTS `test_view`",
        "CREATE MATERIALIZED VIEW `test_view` (`a`) AS SELECT `a` FROM `tbl`",
        "CREATE MATERIALIZED VIEW `test_view` (`a`) AS SELECT `a` FROM `tbl`",
    ]

    adapter.cursor.reset_mock()
    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM tbl"),
        replace=False,
        materialized=True,
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        column_descriptions={"a": "test_column_description", "b": "test_column_description"},
    )
    adapter.create_view(
        "test_view", parse_one("SELECT a, b FROM tbl"), replace=False, materialized=True
    )

    assert to_sql_calls(adapter) == [
        "CREATE MATERIALIZED VIEW `test_view` (`a` COMMENT 'test_column_description', `b` COMMENT 'test_column_description') AS SELECT `a`, `b` FROM `tbl`",
        "CREATE MATERIALIZED VIEW `test_view` AS SELECT `a`, `b` FROM `tbl`",
    ]


def test_create_table(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT")},
        column_descriptions={"a": "test_column_description"},
        table_properties={"unique_key": exp.Column(this=exp.Identifier(this="a", quoted=True))},
    )
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT")},
        column_descriptions={"a": "test_column_description"},
        table_properties={"duplicate_key": exp.Column(this=exp.Identifier(this="a", quoted=True))},
    )
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT")},
        table_description="test_description",
        column_descriptions={"a": "test_column_description"},
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT COMMENT 'test_column_description') UNIQUE KEY (`a`) DISTRIBUTED BY HASH (`a`) BUCKETS 10",
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT COMMENT 'test_column_description') DUPLICATE KEY (`a`)",
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT COMMENT 'test_column_description') COMMENT 'test_description'",
    ]

    adapter.cursor.reset_mock()


def test_create_table_like(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    adapter.create_table_like("target_table", "source_table")

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `target_table` LIKE `source_table`",
    ]


def test_create_schema(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_schema("test_schema")

    assert to_sql_calls(adapter) == [
        "CREATE DATABASE IF NOT EXISTS `test_schema`",
    ]


def test_drop_schema(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.drop_schema("test_schema")
    adapter.drop_schema("test_schema", ignore_if_not_exists=False)

    assert to_sql_calls(adapter) == [
        "DROP DATABASE IF EXISTS `test_schema`",
        "DROP DATABASE `test_schema`",
    ]


def test_rename_table(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    adapter.rename_table("old_table", "new_table")
    adapter.cursor.execute.assert_called_once_with("ALTER TABLE `old_table` RENAME `new_table`")


def test_replace_by_key(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    temp_table = parse_one("temp_table")
    mocker.patch.object(adapter, "_get_temp_table", return_value=temp_table)
    mocker.patch.object(adapter, "ctas")
    mocker.patch.object(adapter, "delete_from")
    mocker.patch.object(adapter, "drop_table")

    columns_to_types = {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")}
    source_query = parse_one("SELECT a, b FROM src")
    target_table = "target_table"

    adapter._replace_by_key(target_table, source_query, columns_to_types, [exp.column("a")], True)
    adapter._replace_by_key(target_table, source_query, columns_to_types, [exp.column("a")], False)
    adapter._replace_by_key(
        target_table, source_query, columns_to_types, [exp.column("a"), exp.column("b")], True
    )
    adapter._replace_by_key(
        target_table, source_query, columns_to_types, [exp.column("a"), exp.column("b")], False
    )

    assert to_sql_calls(adapter, identify=True) == [
        "INSERT INTO `target_table` (`a`, `b`) SELECT `a`, `b` FROM (SELECT `a` AS `a`, `b` AS `b`, ROW_NUMBER() OVER (PARTITION BY `a` ORDER BY `a`) AS _row_number FROM `temp_table`) AS _t WHERE _row_number = 1",
        "INSERT INTO `target_table` (`a`, `b`) SELECT `a`, `b` FROM `temp_table`",
        "INSERT INTO `target_table` (`a`, `b`) SELECT `a`, `b` FROM (SELECT `a` AS `a`, `b` AS `b`, ROW_NUMBER() OVER (PARTITION BY `a`, `b` ORDER BY `a`, `b`) AS _row_number FROM `temp_table`) AS _t WHERE _row_number = 1",
        "INSERT INTO `target_table` (`a`, `b`) SELECT `a`, `b` FROM `temp_table`",
    ]


def test_create_index(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    adapter.create_index("test_table", "test_index", ("cola",))
    adapter.cursor.execute.assert_called_once_with(
        "CREATE INDEX IF NOT EXISTS `test_index` ON `test_table`(`cola`)"
    )


def test_create_table_with_distributed_by(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    distributed_by = {
        "expressions": ["a", "b"],
        "kind": "HASH",
        "buckets": 8,
    }
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_properties={"distributed_by": distributed_by},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT, `b` INT) DISTRIBUTED BY HASH (`a`, `b`) BUCKETS 8",
    ]

    adapter.cursor.execute.reset_mock()

    distributed_by = {
        "expressions": None,
        "kind": "RANDOM",
        "buckets": None,
    }
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_properties={"distributed_by": distributed_by},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT, `b` INT) DISTRIBUTED BY RANDOM",
    ]

    adapter.cursor.execute.reset_mock()

    distributed_by = {
        "expressions": ["a"],
        "kind": "HASH",
        "buckets": "AUTO",
    }
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_properties={"distributed_by": distributed_by},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT, `b` INT) DISTRIBUTED BY HASH (`a`) BUCKETS AUTO",
    ]


def test_create_table_with_properties(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT")},
        table_properties={
            "refresh_interval": "86400",
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT) PROPERTIES ('refresh_interval'='86400')",
    ]


def test_create_table_with_partitioned_by(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "test_table",
        target_columns_to_types={"a": exp.DataType.build("INT"), "b": exp.DataType.build("DATE")},
        partitioned_by=[exp.Literal.string("RANGE(b)")],
        table_properties={
            "partitions": exp.Literal.string(
                "FROM ('2000-11-14') TO ('2021-11-14') INTERVAL 2 YEAR"
            )
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`a` INT, `b` DATE) PARTITION BY RANGE (`b`) (FROM ('2000-11-14') TO ('2021-11-14') INTERVAL 2 YEAR)",
    ]


def test_create_table_with_range_partitioned_by_with_partitions(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "test_table",
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "waiter_id": exp.DataType.build("INT"),
            "customer_id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATETIME"),
        },
        partitioned_by=[exp.Literal.string("RANGE(ds)")],
        table_properties={
            "partitions": exp.Tuple(
                expressions=[
                    exp.Literal.string('PARTITION `p2023` VALUES [("2023-01-01"), ("2024-01-01"))'),
                    exp.Literal.string('PARTITION `p2024` VALUES [("2024-01-01"), ("2025-01-01"))'),
                    exp.Literal.string('PARTITION `p2025` VALUES [("2025-01-01"), ("2026-01-01"))'),
                    exp.Literal.string("PARTITION `other` VALUES LESS THAN MAXVALUE"),
                ]
            ),
            "distributed_by": exp.Tuple(
                expressions=[
                    exp.EQ(
                        this=exp.Column(this=exp.Identifier(this="kind", quoted=True)),
                        expression=exp.Literal.string("HASH"),
                    ),
                    exp.EQ(
                        this=exp.Column(this=exp.Identifier(this="expressions", quoted=True)),
                        expression=exp.Column(this=exp.Identifier(this="id", quoted=True)),
                    ),
                    exp.EQ(
                        this=exp.Column(this=exp.Identifier(this="buckets", quoted=True)),
                        expression=exp.Literal.number(10),
                    ),
                ]
            ),
            "replication_allocation": exp.Literal.string("tag.location.default: 3"),
            "in_memory": exp.Literal.string("false"),
            "storage_format": exp.Literal.string("V2"),
            "disable_auto_compaction": exp.Literal.string("false"),
        },
    )

    expected_sql = (
        "CREATE TABLE IF NOT EXISTS `test_table` "
        "(`id` INT, `waiter_id` INT, `customer_id` INT, `ds` DATETIME) "
        "PARTITION BY RANGE (`ds`) "
        '(PARTITION `p2023` VALUES [("2023-01-01"), ("2024-01-01")), '
        'PARTITION `p2024` VALUES [("2024-01-01"), ("2025-01-01")), '
        'PARTITION `p2025` VALUES [("2025-01-01"), ("2026-01-01")), '
        "PARTITION `other` VALUES LESS THAN MAXVALUE) "
        "DISTRIBUTED BY HASH (`id`) BUCKETS 10 "
        "PROPERTIES ("
        "'replication_allocation'='tag.location.default: 3', "
        "'in_memory'='false', "
        "'storage_format'='V2', "
        "'disable_auto_compaction'='false')"
    )

    assert to_sql_calls(adapter) == [expected_sql]


def test_create_table_with_list_partitioned_by(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "test_table",
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "status": exp.DataType.build("VARCHAR(10)"),
        },
        partitioned_by=[exp.Literal.string("LIST(status)")],
        table_properties={
            "partitions": exp.Tuple(
                expressions=[
                    exp.Literal.string('PARTITION `active` VALUES IN ("active", "pending")'),
                    exp.Literal.string('PARTITION `inactive` VALUES IN ("inactive", "disabled")'),
                ]
            ),
        },
    )

    expected_sql = (
        "CREATE TABLE IF NOT EXISTS `test_table` "
        "(`id` INT, `status` VARCHAR(10)) "
        "PARTITION BY LIST (`status`) "
        '(PARTITION `active` VALUES IN ("active", "pending"), '
        'PARTITION `inactive` VALUES IN ("inactive", "disabled"))'
    )

    assert to_sql_calls(adapter) == [expected_sql]


def test_create_table_with_range_partitioned_by_anonymous_function(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    """Test that RANGE(ds) function call syntax generates correct SQL without duplicate RANGE."""
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "test_table",
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATETIME"),
        },
        # This simulates how partitioned_by RANGE(ds) gets parsed from model definition
        partitioned_by=[exp.Anonymous(this="RANGE", expressions=[exp.to_column("ds")])],
        table_properties={
            "partitions": exp.Literal.string(
                'FROM ("2000-11-14") TO ("2099-11-14") INTERVAL 1 MONTH'
            )
        },
    )

    expected_sql = (
        "CREATE TABLE IF NOT EXISTS `test_table` "
        "(`id` INT, `ds` DATETIME) "
        "PARTITION BY RANGE (`ds`) "
        '(FROM ("2000-11-14") TO ("2099-11-14") INTERVAL 1 MONTH)'
    )

    assert to_sql_calls(adapter) == [expected_sql]


def test_create_materialized_view_with_duplicate_key(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_view(
        "test_mv",
        parse_one("SELECT id, status, COUNT(*) as cnt FROM orders GROUP BY id, status"),
        materialized=True,
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "status": exp.DataType.build("VARCHAR(10)"),
            "cnt": exp.DataType.build("BIGINT"),
        },
        view_properties={
            "duplicate_key": exp.Tuple(
                expressions=[
                    exp.to_column("id"),
                    exp.to_column("status"),
                ]
            ),
        },
    )

    expected_sqls = [
        "DROP MATERIALIZED VIEW IF EXISTS `test_mv`",
        (
            "CREATE MATERIALIZED VIEW `test_mv` "
            "(`id`, `status`, `cnt`) "
            "DUPLICATE KEY (`id`, `status`) "
            "AS SELECT `id`, `status`, COUNT(*) AS `cnt` FROM `orders` GROUP BY `id`, `status`"
        ),
    ]

    assert to_sql_calls(adapter) == expected_sqls


def test_create_full_materialized_view(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    view_properties: t.Dict[str, exp.Expr] = {
        "build": exp.Literal.string("IMMEDIATE"),
        "refresh": exp.Tuple(
            expressions=[
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="method", quoted=True)),
                    expression=exp.Literal.string("AUTO"),
                ),
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="kind", quoted=True)),
                    expression=exp.Literal.string("SCHEDULE"),
                ),
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="every", quoted=True)),
                    expression=exp.Literal.number(1),
                ),
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="unit", quoted=True)),
                    expression=exp.Literal.string("DAY"),
                ),
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="starts", quoted=True)),
                    expression=exp.Literal.string("2024-12-01 20:30:00"),
                ),
            ],
        ),
        "distributed_by": exp.Tuple(
            expressions=[
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="kind", quoted=True)),
                    expression=exp.Literal.string("HASH"),
                ),
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="expressions", quoted=True)),
                    expression=exp.Column(this=exp.Identifier(this="orderkey", quoted=True)),
                ),
                exp.EQ(
                    this=exp.Column(this=exp.Identifier(this="buckets", quoted=True)),
                    expression=exp.Literal.number(2),
                ),
            ]
        ),
        "unique_key": exp.to_column("orderkey"),
        "replication_num": exp.Literal.string("1"),
    }
    materialized_properties = {
        "partitioned_by": [
            parse_one("DATE_TRUNC(o_orderdate, 'MONTH')", dialect="doris"),
        ],
        "clustered_by": [],
        "partition_interval_unit": None,
    }
    columns_to_types = {
        "orderdate": exp.DataType.build("DATE"),
        "orderkey": exp.DataType.build("INT"),
        "partkey": exp.DataType.build("INT"),
    }
    column_descriptions = {
        "orderdate": "order date",
        "orderkey": "order key",
        "partkey": "part key",
    }
    query = parse_one(
        """
        SELECT 
        o_orderdate, 
        l_orderkey, 
        l_partkey 
        FROM 
        orders 
        LEFT JOIN lineitem ON l_orderkey = o_orderkey 
        LEFT JOIN partsupp ON ps_partkey = l_partkey 
        and l_suppkey = ps_suppkey
        """
    )
    adapter.create_view(
        "complete_mv",
        query,
        replace=False,
        materialized=True,
        target_columns_to_types=columns_to_types,
        column_descriptions=column_descriptions,
        table_description="test_description",
        view_properties=view_properties,
        materialized_properties=materialized_properties,
    )
    expected_sqls = [
        "CREATE MATERIALIZED VIEW `complete_mv` (`orderdate` COMMENT 'order date', `orderkey` COMMENT 'order key', `partkey` COMMENT 'part key') "
        "BUILD IMMEDIATE REFRESH AUTO ON SCHEDULE EVERY 1 DAY STARTS '2024-12-01 20:30:00' KEY (`orderkey`) COMMENT 'test_description' "
        "PARTITION BY (DATE_TRUNC(`o_orderdate`, 'MONTH')) "
        "DISTRIBUTED BY HASH (`orderkey`) BUCKETS 2 PROPERTIES ('replication_num'='1') "
        "AS SELECT `o_orderdate`, `l_orderkey`, `l_partkey` FROM `orders` LEFT JOIN `lineitem` ON `l_orderkey` = `o_orderkey` LEFT JOIN `partsupp` ON `ps_partkey` = `l_partkey` AND `l_suppkey` = `ps_suppkey`",
    ]
    sql_calls = to_sql_calls(adapter)

    # Remove extra spaces for comparison
    def norm(s):
        return " ".join(s.split())

    for expected_sql in expected_sqls:
        assert any(norm(expected_sql) == norm(sql) for sql in sql_calls), (
            f"Expected SQL not found.\nExpected: {expected_sql}\nGot: {sql_calls}"
        )


def test_create_table_with_single_string_distributed_by(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    """Test creating table with distributed_by where expressions is a single string."""
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    distributed_by = {
        "expressions": "recordid",  # Single string instead of array
        "kind": "HASH",
        "buckets": 10,
    }
    adapter.create_table(
        "test_table",
        target_columns_to_types={
            "recordid": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR"),
        },
        table_properties={"distributed_by": distributed_by},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_table` (`recordid` INT, `name` VARCHAR) DISTRIBUTED BY HASH (`recordid`) BUCKETS 10",
    ]


def test_delete_from_with_subquery(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # Test DELETE FROM with IN subquery
    adapter.delete_from(
        "test_schema_xtll416o.test_table",
        "id IN (SELECT id FROM test_schema_xtll416o.temp_test_table_ik29031e)",
    )

    # Test DELETE FROM with NOT IN subquery (must NOT rewrite to USING ... WHERE <>)
    adapter.delete_from(
        "test_schema_xtll416o.test_table",
        "id NOT IN (SELECT id FROM test_schema_xtll416o.temp_test_table_ik29031e)",
    )

    # Test simple DELETE FROM (should use base implementation)
    adapter.delete_from("test_schema_xtll416o.test_table", "id = 1")

    assert to_sql_calls(adapter) == [
        "DELETE FROM `test_schema_xtll416o`.`test_table` AS `_t1` USING (SELECT `id` FROM `test_schema_xtll416o`.`temp_test_table_ik29031e`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id`",
        "DELETE FROM `test_schema_xtll416o`.`test_table` WHERE NOT `id` IN (SELECT `id` FROM `test_schema_xtll416o`.`temp_test_table_ik29031e`)",
        "DELETE FROM `test_schema_xtll416o`.`test_table` WHERE `id` = 1",
    ]


def test_delete_from_with_complex_subquery(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # Test DELETE FROM with complex subquery (multiple columns)
    adapter.delete_from(
        "test_schema_xtll416o.test_table",
        "(id, name) IN (SELECT id, name FROM test_schema_xtll416o.temp_test_table_ik29031e WHERE active = 1)",
    )

    # Test DELETE FROM with subquery that has WHERE clause
    adapter.delete_from(
        "test_schema_xtll416o.test_table",
        "id IN (SELECT id FROM test_schema_xtll416o.temp_test_table_ik29031e WHERE created_date > '2024-01-01')",
    )

    assert to_sql_calls(adapter) == [
        "DELETE FROM `test_schema_xtll416o`.`test_table` AS `_t1` USING (SELECT `id`, `name` FROM `test_schema_xtll416o`.`temp_test_table_ik29031e` WHERE `active` = 1) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`name` = `_t2`.`name`",
        "DELETE FROM `test_schema_xtll416o`.`test_table` AS `_t1` USING (SELECT `id` FROM `test_schema_xtll416o`.`temp_test_table_ik29031e` WHERE `created_date` > '2024-01-01') AS `_t2` WHERE `_t1`.`id` = `_t2`.`id`",
    ]


def test_delete_from_fallback_to_base(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # Test DELETE FROM with simple conditions (should use base implementation)
    adapter.delete_from("test_table", "id = 1")
    adapter.delete_from("test_table", "name = 'test' AND status = 'active'")
    adapter.delete_from("test_table", "id IN (1, 2, 3)")  # Simple IN with values, not subquery

    assert to_sql_calls(adapter) == [
        "DELETE FROM `test_table` WHERE `id` = 1",
        "DELETE FROM `test_table` WHERE `name` = 'test' AND `status` = 'active'",
        "DELETE FROM `test_table` WHERE `id` IN (1, 2, 3)",
    ]


def test_delete_from_full_table(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # Test DELETE FROM with WHERE TRUE (should use TRUNCATE)
    adapter.delete_from("test_table", exp.true())

    assert to_sql_calls(adapter) == [
        "TRUNCATE TABLE `test_table`",
    ]


def test_create_table_olap_key_prefix_reordering_and_order_by(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "events",
        target_columns_to_types={
            "payload": exp.DataType.build("VARCHAR(255)"),
            "id": exp.DataType.build("BIGINT"),
            "ds": exp.DataType.build("DATE"),
            "updated_at": exp.DataType.build("DATETIME"),
        },
        unique_key=[exp.to_column("id"), exp.to_column("ds")],
        table_properties={
            "order_by": exp.Tuple(expressions=[exp.to_column("updated_at"), exp.to_column("id")]),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `events` (`id` BIGINT, `ds` DATE, `payload` VARCHAR(255), `updated_at` DATETIME) UNIQUE KEY (`id`, `ds`) ORDER BY (`updated_at`, `id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10",
    ]


def test_create_table_aggregate_key_and_clustered_by_buckets(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "metrics_agg",
        target_columns_to_types={
            "total": exp.DataType.build("BIGINT"),
            "tenant_id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATE"),
        },
        clustered_by=[exp.to_column("tenant_id")],
        table_properties={
            "aggregate_key": exp.Tuple(
                expressions=[exp.to_column("tenant_id"), exp.to_column("ds")]
            ),
            "buckets": exp.Literal.number(16),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `metrics_agg` (`tenant_id` INT, `ds` DATE, `total` BIGINT) AGGREGATE KEY (`tenant_id`, `ds`) DISTRIBUTED BY HASH (`tenant_id`) BUCKETS 16",
    ]


def test_create_table_auto_partition_and_unannotated_partitioned_by_guard(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    # Without partitions or auto_partition, OLAP partitioned_by is safely omitted
    adapter.create_table(
        "olap_no_partitions",
        target_columns_to_types={"id": exp.DataType.build("INT"), "ds": exp.DataType.build("DATE")},
        partitioned_by=[exp.to_column("ds")],
    )
    # With auto_partition = true, emits AUTO PARTITION BY RANGE (...) () and marks range partition column NOT NULL
    adapter.create_table(
        "olap_auto_partition",
        target_columns_to_types={
            "payload": exp.DataType.build("STRING", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
        partitioned_by=[parse_one("RANGE(DATE_TRUNC(ds, 'DAY'))", dialect="doris")],
        table_properties={"auto_partition": exp.Literal.string("true")},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `olap_no_partitions` (`id` INT, `ds` DATE)",
        "CREATE TABLE IF NOT EXISTS `olap_auto_partition` (`id` INT, `ds` DATE NOT NULL, `payload` STRING) AUTO PARTITION BY RANGE (DATE_TRUNC(`ds`, 'DAY')) ()",
    ]


def test_iceberg_multi_catalog_ddl_and_dml(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    assert adapter.catalog_support == CatalogSupport.FULL_SUPPORT
    assert adapter.default_catalog is None

    # 1. Explicit table_format='iceberg' on 2-part table name emits ENGINE=iceberg and PARTITION BY LIST (...) ()
    adapter.create_table(
        "silver.iceberg_events",
        target_columns_to_types={
            "id": exp.DataType.build("BIGINT"),
            "ds": exp.DataType.build("DATE"),
            "region": exp.DataType.build("STRING"),
        },
        table_format="iceberg",
        partitioned_by=[parse_one("DAY(ds)", dialect="doris"), exp.to_column("region")],
        unique_key=[exp.to_column("id")],
        table_properties={"write-format": exp.Literal.string("parquet")},
    )

    # 2. 3-part external Iceberg catalog CTAS omits ENGINE=iceberg (invalid in Doris CTAS grammar)
    adapter.ctas(
        "iceberg_cat.silver.events_ctas",
        parse_one("SELECT id, ds FROM bronze.events"),
        target_columns_to_types={
            "id": exp.DataType.build("BIGINT"),
            "ds": exp.DataType.build("DATE"),
        },
    )

    # 3. replace_query on existing Iceberg table uses atomic INSERT OVERWRITE TABLE
    mocker.patch.object(
        adapter,
        "get_data_object",
        return_value=DataObject(
            catalog="iceberg_cat", schema="silver", name="events_ctas", type=DataObjectType.TABLE
        ),
    )
    adapter.replace_query(
        "iceberg_cat.silver.events_ctas",
        parse_one("SELECT id, ds FROM bronze.events"),
        target_columns_to_types={
            "id": exp.DataType.build("BIGINT"),
            "ds": exp.DataType.build("DATE"),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `silver`.`iceberg_events` (`id` BIGINT, `ds` DATE, `region` STRING) ENGINE=iceberg PARTITION BY LIST (DAY(`ds`), `region`) () PROPERTIES ('write-format'='parquet')",
        "CREATE TABLE IF NOT EXISTS `iceberg_cat`.`silver`.`events_ctas` AS SELECT CAST(`id` AS BIGINT) AS `id`, CAST(`ds` AS DATE) AS `ds` FROM (SELECT `id`, `ds` FROM `bronze`.`events`) AS `_subquery`",
        "INSERT OVERWRITE TABLE `iceberg_cat`.`silver`.`events_ctas` (`id`, `ds`) SELECT `id`, `ds` FROM `bronze`.`events`",
    ]


def test_columns_normalization_largeint_and_struct(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.cursor.fetchall.return_value = [
        ("id", "LARGEINT", "No", "true", None, ""),
        ("price", "DECIMALV3(9, 0)", "Yes", "false", None, ""),
        ("is_active", "TINYINT(1)", "Yes", "false", None, ""),
        ("count_val", "INT(11)", "Yes", "false", None, ""),
        ("meta", "STRUCT<f1:INT(11), f2:VARCHAR(20)>", "Yes", "false", None, ""),
    ]

    cols = adapter.columns("test_db.test_table")
    assert cols == {
        "id": exp.DataType.build("INT128"),
        "price": exp.DataType.build("DECIMAL(9, 0)", dialect="doris"),
        "is_active": exp.DataType.build("BOOLEAN", dialect="doris"),
        "count_val": exp.DataType.build("INT", dialect="doris"),
        "meta": exp.DataType.build("STRUCT<f1:INT, f2:VARCHAR(20)>", dialect="doris"),
    }

    adapter.cursor.reset_mock()
    adapter.create_table(
        "test_db.struct_tbl",
        target_columns_to_types={"id": cols["id"], "meta": cols["meta"]},
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `test_db`.`struct_tbl` (`id` LARGEINT, `meta` STRUCT<`f1`: INT, `f2`: VARCHAR(20)>)",
    ]


def test_get_data_objects_with_catalog_and_mv(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter, patch_get_data_objects=False)
    mocker.patch.object(
        adapter,
        "fetchall",
        return_value=[
            ("silver", "tbl1", "table"),
            ("silver", "mv1", "materialized_view"),
        ],
    )

    objs = adapter._get_data_objects("iceberg_cat.silver")
    assert len(objs) == 2
    assert objs[0].type == DataObjectType.TABLE
    assert objs[0].catalog == "iceberg_cat"
    assert objs[0].schema_name == "silver"
    assert objs[0].name == "tbl1"
    assert objs[1].type == DataObjectType.MATERIALIZED_VIEW
    assert objs[1].name == "mv1"


def test_comments_escaping(make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter]):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    table_comment_sql = adapter._build_create_comment_table_exp(
        exp.to_table("db.tbl"), " O'Reilly's \"table\" \\ test "
    )
    col_comment_sql = adapter._build_create_comment_column_exp(
        exp.to_table("db.tbl"), "col'1", ' Col\'s "comment" '
    )
    assert (
        table_comment_sql
        == "ALTER TABLE `db`.`tbl` MODIFY COMMENT 'O''Reilly''s \"table\" \\\\ test'"
    )
    assert (
        col_comment_sql
        == "ALTER TABLE `db`.`tbl` MODIFY COLUMN `col'1` COMMENT 'Col''s \"comment\"'"
    )


def test_delete_from_between_concat_ws_and_literal_fallback(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. BETWEEN rewrite in delete_from
    adapter.delete_from("test_table", "ds BETWEEN '2024-01-01' AND '2024-01-31'")

    # 2. _replace_by_key / LogicalMergeMixin CONCAT_WS unwrap into multi-column USING
    key_exp = exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", exp.column("id"), exp.column("ds"))
    merge_where = key_exp.isin(query=exp.select(key_exp).from_("src_table"))
    adapter.delete_from("target_table", where=merge_where)

    assert to_sql_calls(adapter) == [
        "DELETE FROM `test_table` WHERE `ds` >= '2024-01-01' AND `ds` <= '2024-01-31'",
        "DELETE FROM `target_table` AS `_t1` USING (SELECT `id`, `ds` FROM `src_table`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`ds` = `_t2`.`ds`",
    ]

    # 3. Fallback to zero-OR batched _delete_by_literal_keys when USING fails on DUPLICATE KEY / Iceberg
    adapter.cursor.reset_mock()
    mocker.patch.object(
        adapter,
        "fetchall",
        return_value=[("tenant_a", 1), ("tenant_a", 2), ("tenant_a", None)],
    )
    adapter._delete_by_literal_keys(
        "dup_table",
        [exp.column("tenant"), exp.column("id")],
        parse_one("SELECT tenant, id FROM src_table", into=exp.Select),
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `dup_table` WHERE `tenant` = 'tenant_a' AND `id` IN (1, 2)",
        "DELETE FROM `dup_table` WHERE `tenant` = 'tenant_a' AND `id` IS NULL",
    ]


def test_execute_operational_error_reconnect(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    class OperationalError(Exception):
        pass

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    conn_mock = mocker.MagicMock()
    mocker.patch.object(adapter._connection_pool, "get", return_value=conn_mock)
    close_spy = mocker.spy(adapter._connection_pool, "close")

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OperationalError(2013, "Lost connection to MySQL server during query")

    adapter.cursor.execute.side_effect = side_effect
    adapter._execute("WITH `cte` AS (SELECT 1) SELECT * FROM `cte`")

    assert call_count == 2
    conn_mock.ping.assert_called_once_with(reconnect=True)

    # Read timeout (2013 with TimeoutError / "timed out") must NOT be retried even for SELECT
    conn_mock.reset_mock()
    close_spy.reset_mock()
    timeout_calls = 0

    def timeout_side_effect(*args, **kwargs):
        nonlocal timeout_calls
        timeout_calls += 1
        raise OperationalError(
            2013, "Lost connection to MySQL server during query (timed out)"
        ) from TimeoutError("timed out")

    adapter.cursor.execute.side_effect = timeout_side_effect
    with pytest.raises(OperationalError):
        adapter._execute("SELECT 1")

    assert timeout_calls == 1
    conn_mock.ping.assert_not_called()
    close_spy.assert_called_once()

    # Write statements (e.g. INSERT or CTE-prefixed INSERT) must NOT be retried after OperationalError(2013) to avoid duplicating rows
    for unsafe_write_sql in (
        "INSERT INTO `tbl` VALUES (1)",
        "WITH `cte` AS (SELECT 1) INSERT INTO `tbl` SELECT * FROM `cte`",
    ):
        conn_mock.reset_mock()
        close_spy.reset_mock()
        write_calls = 0

        def write_side_effect(*args, **kwargs):
            nonlocal write_calls
            write_calls += 1
            raise OperationalError(2013, "Lost connection to MySQL server during query")

        adapter.cursor.execute.side_effect = write_side_effect
        with pytest.raises(OperationalError):
            adapter._execute(unsafe_write_sql)

        assert write_calls == 1
        conn_mock.ping.assert_not_called()
        close_spy.assert_called_once()


def test_external_catalog_view_error_and_drop_noop(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    from sqlmesh.utils.errors import SQLMeshError

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # Regular view creation on an external catalog raises SQLMeshError pointing to virtual_environment_mode and environment_catalog_mapping
    with pytest.raises(
        SQLMeshError,
        match=r"Doris only supports creating views in the 'internal' catalog, got 'iceberg_cat'.*virtual_environment_mode: dev_only.*environment_catalog_mapping",
    ):
        adapter.create_view("iceberg_cat.silver__dev.events", parse_one("SELECT 1 AS id"))

    # Drop view on external catalog remains a safe no-op during cleanup
    adapter.drop_view("iceberg_cat.silver__dev.events")
    assert to_sql_calls(adapter) == []

    # Materialized view on external catalog also raises SQLMeshError
    with pytest.raises(
        SQLMeshError,
        match="Doris only supports creating materialized views in the 'internal' catalog",
    ):
        adapter.create_view(
            "iceberg_cat.silver.events_mv",
            parse_one("SELECT 1 AS id"),
            materialized=True,
        )

    # Materialized view with invalid property raises SQLMeshError BEFORE dropping existing MV
    with pytest.raises(SQLMeshError, match="Invalid materialized view build mode"):
        adapter.create_view(
            "silver.events_mv",
            parse_one("SELECT 1 AS id"),
            replace=True,
            materialized=True,
            view_properties={"build": exp.Literal.string("INVALID_MODE")},
        )
    assert to_sql_calls(adapter) == []

    # Invalid refresh_trigger also raises SQLMeshError instead of silently falling back to AUTO
    with pytest.raises(SQLMeshError, match="Invalid materialized view refresh trigger"):
        adapter.create_view(
            "silver.events_mv",
            parse_one("SELECT 1 AS id"),
            replace=True,
            materialized=True,
            view_properties={"refresh_trigger": exp.Literal.string("ON DEMAND")},
        )
    assert to_sql_calls(adapter) == []


def test_duplicate_key_with_buckets_and_random_buckets_fallback(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "dup_bucketed",
        target_columns_to_types={"id": exp.DataType.build("INT"), "val": exp.DataType.build("INT")},
        table_properties={
            "duplicate_key": exp.Tuple(expressions=[exp.to_column("id")]),
            "buckets": exp.Literal.number(16),
        },
    )
    adapter.create_table(
        "random_bucketed",
        target_columns_to_types={"id": exp.DataType.build("INT"), "val": exp.DataType.build("INT")},
        table_properties={
            "buckets": exp.Literal.number(8),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `dup_bucketed` (`id` INT, `val` INT) DUPLICATE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 16",
        "CREATE TABLE IF NOT EXISTS `random_bucketed` (`id` INT, `val` INT) DISTRIBUTED BY RANDOM BUCKETS 8",
    ]


def test_truncate_table_fallback_on_unsupported_truncate(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch.object(
        adapter,
        "columns",
        return_value={"id": exp.DataType.build("BIGINT"), "ds": exp.DataType.build("DATE")},
    )

    call_count = 0

    def side_effect(sql, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1 and "TRUNCATE" in str(sql):
            raise Exception("errCode = 2, detailMessage = Not support truncate table on iceberg")

    adapter.cursor.execute.side_effect = side_effect
    adapter._truncate_table("silver.iceberg_two_part")

    assert to_sql_calls(adapter) == [
        "TRUNCATE TABLE `silver`.`iceberg_two_part`",
        "DELETE FROM `silver`.`iceberg_two_part` WHERE NOT `id` IS NULL",
        "DELETE FROM `silver`.`iceberg_two_part` WHERE `id` IS NULL",
    ]


def test_row_diff_mixin_concat_columns(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    concat_expr = adapter.concat_columns(
        {
            "id": exp.DataType.build("INT"),
            "flag": exp.DataType.build("BOOLEAN"),
            "ts": exp.DataType.build("DATETIME"),
        }
    )
    assert (
        concat_expr.sql("doris")
        == "CONCAT(COALESCE(CAST(CAST(id AS BIGINT) AS VARCHAR), ''), ',', COALESCE(CAST(CAST(flag AS INT) AS VARCHAR), ''), ',', COALESCE(CAST(DATE_FORMAT(ts, '%Y-%m-%d %T.%f') AS VARCHAR), ''))"
    )


def test_temp_table_length_and_catalog_switch_and_wrapped_concat_ws(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. _get_temp_table never exceeds Doris's 64-character identifier limit
    long_table = "db." + ("a" * 60)
    temp_tbl = adapter._get_temp_table(long_table)
    assert len(temp_tbl.name) <= 64

    # 2. set_current_catalog preserves backtick quoting on hyphenated catalog names
    adapter.set_current_catalog("iceberg-cat")

    # 3. delete_from unwraps parenthesized CONCAT_WS(...)
    key_exp = exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", exp.column("id"), exp.column("ds"))
    wrapped_where = exp.Paren(this=key_exp).isin(query=exp.select(key_exp).from_("src_table"))
    adapter.delete_from("target_table", where=wrapped_where)

    assert to_sql_calls(adapter) == [
        "SWITCH `iceberg-cat`",
        "DELETE FROM `target_table` AS `_t1` USING (SELECT `id`, `ds` FROM `src_table`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`ds` = `_t2`.`ds`",
    ]


def test_insert_overwrite_by_partition_unwraps_wrappers_and_transforms(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch.object(
        adapter,
        "_get_temp_table",
        return_value=exp.table_("__temp_tbl", db="db"),
    )
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[("2024-01-15 00:00:00", "2024-01-16 00:00:00")],
    )

    adapter.insert_overwrite_by_partition(
        "db.events",
        parse_one("SELECT id, ds FROM db.src", read="doris"),
        partitioned_by=[
            parse_one("RANGE(DATE_TRUNC(ds, 'day'))", read="doris"),
        ],
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATETIME"),
        },
    )

    sql_calls = to_sql_calls(adapter)
    assert (
        "DELETE FROM `db`.`events` WHERE DATE_TRUNC(`ds`, 'day') IN (SELECT DISTINCT DATE_TRUNC(`ds`, 'day') FROM `db`.`__temp_tbl`)"
        not in sql_calls
    )
    assert (
        "SELECT DISTINCT DATE_TRUNC(`ds`, 'day') AS `_low_0`, DATE_ADD(DATE_TRUNC(`ds`, 'day'), INTERVAL 1 DAY) AS `_high_0` FROM `db`.`__temp_tbl`"
        in sql_calls
    )
    assert (
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-15 00:00:00' AND `ds` < '2024-01-16 00:00:00'"
        in sql_calls
    )

    # Unsupported non-range partition transforms (e.g. BUCKET) raise a clear SQLMeshError
    with pytest.raises(
        SQLMeshError,
        match="Doris insert_overwrite_by_partition does not support partition expression",
    ):
        adapter.insert_overwrite_by_partition(
            "db.events",
            parse_one("SELECT id, ds FROM db.src", read="doris"),
            partitioned_by=[parse_one("BUCKET(16, id)", read="doris")],
            target_columns_to_types={
                "id": exp.DataType.build("INT"),
                "ds": exp.DataType.build("DATETIME"),
            },
        )


def test_sync_materialized_view_drop_and_replace(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    adapter.create_view(
        "db.sync_mv",
        parse_one(
            "SELECT region, SUM(amount) AS total FROM db.orders GROUP BY region", read="doris"
        ),
        replace=True,
        materialized=True,
        view_properties={
            "materialized_type": exp.Literal.string("sync"),
            "source_table": exp.Literal.string("db.orders"),
        },
    )

    assert to_sql_calls(adapter) == [
        "USE `db`",
        "DROP MATERIALIZED VIEW IF EXISTS `sync_mv` ON `db`.`orders`",
        "USE `db`",
        "CREATE MATERIALIZED VIEW `sync_mv` AS SELECT `region` AS `mv_region`, SUM(`amount`) AS `total` FROM `db`.`orders` GROUP BY `region`",
    ]

    adapter.cursor.execute.reset_mock()

    adapter.create_view(
        "sync_mv",
        parse_one("SELECT id FROM db.orders", read="doris"),
        replace=True,
        materialized=True,
        view_properties={
            "materialized_type": exp.Literal.string("SYNC"),
            "source_table": exp.to_table("db.orders"),
        },
    )

    assert to_sql_calls(adapter) == [
        "USE `db`",
        "DROP MATERIALIZED VIEW IF EXISTS `sync_mv` ON `db`.`orders`",
        "USE `db`",
        "CREATE MATERIALIZED VIEW `sync_mv` AS SELECT `id` AS `mv_id` FROM `db`.`orders`",
    ]

    adapter.cursor.execute.reset_mock()

    # source_table parsed as exp.Column (from MODEL physical_properties) and 3-part internal.db.orders
    adapter.create_view(
        "sync_mv",
        parse_one("SELECT id FROM orders", read="doris"),
        replace=True,
        materialized=True,
        view_properties={
            "materialized_type": exp.Literal.string("SYNC"),
            "source_table": parse_one("internal.db.orders", read="doris"),
        },
    )

    assert to_sql_calls(adapter) == [
        "USE `db`",
        "DROP MATERIALIZED VIEW IF EXISTS `sync_mv` ON `db`.`orders`",
        "USE `db`",
        "CREATE MATERIALIZED VIEW `sync_mv` AS SELECT `id` AS `mv_id` FROM `orders`",
    ]


def test_delete_with_subquery_fallback_and_cardinality_aware_composite_batching(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # When DELETE ... USING fails on DUPLICATE/AGGREGATE KEY OLAP tables, fallback to literal batching
    # and pick the highest-cardinality column (id: 3 values) to batch in IN (...) while grouping by low-cardinality ds (1 value).
    mocker.patch.object(
        adapter,
        "fetchall",
        return_value=[(1, "2024-01-01"), (2, "2024-01-01"), (3, "2024-01-01")],
    )

    first_delete = True

    def execute_side_effect(sql, *args, **kwargs):
        nonlocal first_delete
        if first_delete and "USING" in str(sql):
            first_delete = False
            raise Exception("errCode = 2, detailMessage = delete using only support unique key")

    adapter.cursor.execute.side_effect = execute_side_effect

    key_exp = exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", exp.column("id"), exp.column("ds"))
    where = exp.Paren(this=key_exp).isin(query=exp.select(key_exp).from_("src_table"))
    adapter.delete_from("target_table", where=where)

    assert to_sql_calls(adapter) == [
        "DELETE FROM `target_table` AS `_t1` USING (SELECT `id`, `ds` FROM `src_table`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`ds` = `_t2`.`ds`",
        "DELETE FROM `target_table` WHERE `ds` = '2024-01-01' AND `id` IN (1, 2, 3)",
    ]


def test_rewrite_between_in_where_preserves_not_between_and_wraps_or(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    raw_where = parse_one(
        "(ds BETWEEN '2024-01-01' AND '2024-01-07' OR status = 'expired') AND id NOT BETWEEN 10 AND 20",
        read="doris",
    )
    rewritten_where = adapter._rewrite_between_in_where(raw_where.copy())
    assert (
        adapter._to_sql(rewritten_where)
        == "((`ds` >= '2024-01-01' AND `ds` <= '2024-01-07') OR `status` = 'expired') AND NOT `id` BETWEEN 10 AND 20"
    )

    adapter.delete_from(
        "target_table",
        where=raw_where,
    )

    assert to_sql_calls(adapter) == [
        "DELETE FROM `target_table` WHERE `ds` >= '2024-01-01' AND `ds` <= '2024-01-07' AND `id` < 10",
        "DELETE FROM `target_table` WHERE `ds` >= '2024-01-01' AND `ds` <= '2024-01-07' AND `id` > 20",
        "DELETE FROM `target_table` WHERE `status` = 'expired' AND `id` < 10",
        "DELETE FROM `target_table` WHERE `status` = 'expired' AND `id` > 20",
    ]


@pytest.mark.parametrize(
    "key_cols,rows",
    [
        (["id"], [(1,), (None,), (2,), (3,)]),
        (["id", "ds"], [(1, "2024-01-01"), (2, "2024-01-01"), (None, "2024-01-01"), (1, None)]),
        (
            ["tenant", "region", "id"],
            [
                ("t1", "us", 10),
                ("t1", "us", 20),
                ("t1", "us", None),
                ("t1", None, 30),
                (None, None, None),
            ],
        ),
    ],
)
def test_delete_by_literal_keys_doris_delete_handler_invariants(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
    mocker: MockerFixture,
    key_cols: t.List[str],
    rows: t.List[t.Tuple[t.Any, ...]],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch.object(adapter, "fetchall", return_value=rows)

    adapter._delete_by_literal_keys(
        exp.to_table("db.target_tbl"),
        [exp.column(c) for c in key_cols],
        parse_one(f"SELECT {', '.join(key_cols)} FROM db.src_tbl", read="doris", into=exp.Select),
    )

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) >= 1
    for sql in sql_calls:
        parsed = parse_one(sql, read="doris")
        assert isinstance(parsed, exp.Delete)
        # Doris DeleteHandler forbids OR and BETWEEN in DELETE WHERE predicates
        assert parsed.find(exp.Or) is None
        assert parsed.find(exp.Between) is None


@pytest.mark.parametrize(
    "non_key_type",
    [
        "FLOAT",
        "DOUBLE",
        "ARRAY<INT>",
        "MAP<STRING, INT>",
        "STRUCT<a: INT>",
        "JSON",
        "VARIANT",
        "BITMAP",
        "HLL",
        "QUANTILE_STATE",
        "AGG_STATE(sum(INT))",
    ],
)
def test_olap_key_type_and_prefix_invariants(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
    non_key_type: str,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.create_table(
        "db.complex_tbl",
        target_columns_to_types={
            "bad_first_col": exp.DataType.build(non_key_type, dialect="doris", udt=True),
            "valid_key": exp.DataType.build("BIGINT", dialect="doris"),
        },
    )

    sql_calls = to_sql_calls(adapter)
    assert len(sql_calls) == 1
    # valid_key must be moved to column index 0 ahead of non-keyable bad_first_col
    assert sql_calls[0].startswith(
        "CREATE TABLE IF NOT EXISTS `db`.`complex_tbl` (`valid_key` BIGINT, `bad_first_col`"
    )


def test_adjust_physical_properties_for_incremental(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    assert adapter.RECREATE_MATERIALIZED_VIEW_ON_EVALUATION is False
    assert adapter.RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES == frozenset(
        {"excluded_trigger_tables", "source_table"}
    )

    # Single unique_key promoted when no explicit key is present (table qualifiers stripped)
    props = adapter.adjust_physical_properties_for_incremental(
        {},
        requires_delete_capable_table=True,
        unique_key=[exp.to_column("src.id")],
        model_name="db.incremental_model",
    )
    assert props == {"unique_key": exp.to_column("id")}

    # Composite unique_key promoted as exp.Tuple
    props_composite = adapter.adjust_physical_properties_for_incremental(
        {},
        requires_delete_capable_table=True,
        unique_key=[exp.to_column("id"), exp.to_column("ds")],
        model_name="db.incremental_model",
    )
    assert props_composite == {
        "unique_key": exp.Tuple(expressions=[exp.to_column("id"), exp.to_column("ds")])
    }

    # Explicit duplicate_key in physical_properties is preserved without overwriting
    props_explicit = adapter.adjust_physical_properties_for_incremental(
        {"duplicate_key": exp.to_column("id")},
        requires_delete_capable_table=True,
        unique_key=[exp.to_column("id")],
        model_name="db.incremental_model",
    )
    assert props_explicit == {"duplicate_key": exp.to_column("id")}

    # Verify end-to-end CREATE TABLE with promoted unique_key reorders key columns first and emits UNIQUE KEY
    adapter.create_table(
        "db.incremental_model",
        target_columns_to_types={
            "name": exp.DataType.build("VARCHAR(50)", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        table_properties=props,
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`incremental_model` (`id` INT, `name` VARCHAR(50)) UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10"
    ]


def test_merge_with_identifier_unique_key(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="abcd1234")
    adapter.merge(
        target_table="db.target",
        source_table=parse_one("SELECT id, ds FROM db.src"),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("VARCHAR(32)", dialect="doris"),
        },
        unique_key=[exp.to_identifier("id")],
    )
    sql_calls = to_sql_calls(adapter)
    assert any(
        call.startswith(
            "DELETE FROM `db`.`target` AS `_t1` USING (SELECT `id` FROM `db`.`__temp_target_abcd1234`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id`"
        )
        for call in sql_calls
    )


def test_list_partition_ast_and_random_distributed_by(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    adapter.create_table(
        "db.list_tbl",
        target_columns_to_types={
            "region": exp.DataType.build("VARCHAR(16)", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        partitioned_by=[parse_one("LIST(region)", read="doris")],
        table_properties={
            "partitions": exp.Literal.string("PARTITION p_na VALUES IN ('US', 'CA')"),
            "distributed_by": parse_one("RANDOM()", read="doris"),
            "buckets": exp.Literal.number(8),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`list_tbl` (`region` VARCHAR(16), `id` INT) PARTITION BY LIST (`region`) (PARTITION p_na VALUES IN ('US', 'CA')) DISTRIBUTED BY RANDOM BUCKETS 8"
    ]


def test_key_model_mutual_exclusion_and_missing_column_validation(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    with pytest.raises(SQLMeshError, match="at most one key model"):
        adapter.create_table(
            "db.bad_keys",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={
                "unique_key": exp.to_column("id"),
                "duplicate_key": exp.to_column("id"),
            },
        )

    with pytest.raises(SQLMeshError, match="not found in table schema columns"):
        adapter.create_table(
            "db.missing_key",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={
                "unique_key": exp.to_column("nonexistent_col"),
            },
        )

    with pytest.raises(SQLMeshError, match="only supported on UNIQUE KEY tables"):
        adapter.create_table(
            "db.order_by_without_uk",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={
                "duplicate_key": exp.to_column("id"),
                "order_by": exp.to_column("name"),
            },
        )

    # Case-insensitive key column matching resolves to the canonical schema column name
    adapter.create_table(
        "db.case_insensitive_uk",
        target_columns_to_types={
            "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        table_properties={
            "unique_key": exp.to_column("ID"),
            "distributed_by": "HASH(ID) BUCKETS 8",
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`case_insensitive_uk` (`id` INT, `name` VARCHAR(32)) UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 8"
    ]

    with pytest.raises(
        SQLMeshError, match="Doris UNIQUE KEY tables do not support DISTRIBUTED BY RANDOM"
    ):
        adapter.create_table(
            "db.uk_random_dist",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={
                "unique_key": exp.to_column("id"),
                "distributed_by": "RANDOM BUCKETS 8",
            },
        )

    with pytest.raises(
        SQLMeshError, match="Doris distributed_by columns .* not found in table schema columns"
    ):
        adapter.create_table(
            "db.missing_dist_col",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={
                "distributed_by": "HASH(missing_col) BUCKETS 8",
            },
        )

    # Explicit distributed_by on UNIQUE KEY / AGGREGATE KEY must be a subset of key columns
    with pytest.raises(
        SQLMeshError,
        match="Doris UNIQUE KEY tables require distributed_by columns to be key columns",
    ):
        adapter.create_table(
            "db.uk_non_key_dist",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={
                "unique_key": exp.to_column("id"),
                "distributed_by": "HASH(name) BUCKETS 8",
            },
        )

    # Cross-engine clustered_by on non-key column for UNIQUE KEY table falls back to key column
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "db.uk_clustered_by_fallback",
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
        },
        clustered_by=[exp.to_column("name")],
        table_properties={
            "unique_key": exp.to_column("id"),
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`uk_clustered_by_fallback` (`id` INT, `name` VARCHAR(32)) UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10"
    ]

    # Missing partitioned_by column raises SQLMeshError when partitioning is active
    with pytest.raises(
        SQLMeshError, match="Doris partitioned_by columns .* not found in table schema columns"
    ):
        adapter.create_table(
            "db.missing_part_col",
            target_columns_to_types={
                "id": exp.DataType.build("INT", dialect="doris"),
                "ds": exp.DataType.build("DATE", dialect="doris"),
            },
            partitioned_by=[parse_one("RANGE(missing_ds)", read="doris")],
            table_properties={"auto_partition": exp.Literal.string("true")},
        )


def test_ctas_reorders_projections_in_place_without_double_subquery(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    adapter.ctas(
        "db.ctas_reordered",
        parse_one("SELECT name, id FROM db.src", read="doris"),
        target_columns_to_types={
            "name": exp.DataType.build("VARCHAR(50)", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        table_properties={"unique_key": exp.to_column("id")},
    )

    # Unannotated CTAS (target_columns_to_types=None) also reorders Select projections for key prefix
    adapter.ctas(
        "db.ctas_unannotated_reordered",
        parse_one("SELECT name, id FROM db.src", read="doris"),
        table_properties={"unique_key": exp.to_column("id")},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`ctas_reordered` UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10 AS SELECT CAST(`id` AS INT) AS `id`, CAST(`name` AS VARCHAR(50)) AS `name` FROM (SELECT `name`, `id` FROM `db`.`src`) AS `_subquery`",
        "CREATE TABLE IF NOT EXISTS `db`.`ctas_unannotated_reordered` UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10 AS SELECT `id`, `name` FROM `db`.`src`",
    ]


def test_get_data_objects_reclassifies_mtmv_via_mv_infos(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter, patch_get_data_objects=False)
    mocker.patch.object(
        adapter,
        "fetchall",
        side_effect=[
            [("silver", "regular_tbl", "BASE TABLE"), ("silver", "async_mv", "BASE TABLE")],
            [("async_mv",)],
        ],
    )

    objects = adapter._get_data_objects("silver")
    assert [(o.name, o.type) for o in objects] == [
        ("regular_tbl", DataObjectType.TABLE),
        ("async_mv", DataObjectType.MATERIALIZED_VIEW),
    ]


def test_delete_from_boolean_literal_simplification(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # FALSE is a no-op
    adapter.delete_from("db.tbl", where=exp.false())
    assert to_sql_calls(adapter) == []

    # Compound AND TRUE is simplified away so Doris DeleteHandler does not reject boolean literal
    adapter.delete_from("db.tbl", where="id = 1 AND TRUE")
    assert to_sql_calls(adapter) == ["DELETE FROM `db`.`tbl` WHERE `id` = 1"]

    adapter.cursor.execute.reset_mock()
    adapter.delete_from("db.tbl", where="TRUE")
    assert to_sql_calls(adapter) == ["TRUNCATE TABLE `db`.`tbl`"]

    # Target-qualified extra condition in DELETE ... USING is rewritten to alias _t1
    adapter.cursor.execute.reset_mock()
    adapter.delete_from(
        "db.target",
        where="target.ds = '2024-01-01' AND id IN (SELECT id FROM db.src)",
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`target` AS `_t1` USING (SELECT `id` FROM `db`.`src`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`ds` = '2024-01-01'"
    ]

    # Target-qualified condition in simple DELETE FROM is stripped to unqualified column for Doris DeleteHandler
    adapter.cursor.execute.reset_mock()
    adapter.delete_from(
        "db.target",
        where="target.ds = '2024-01-01' AND target.id = 5",
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`target` WHERE `ds` = '2024-01-01' AND `id` = 5"
    ]


def test_resolve_table_refs_in_physical_properties_for_mv_and_sync_mv(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    from sqlmesh.core.dialect import parse
    from sqlmesh.core.model import SqlModel, load_sql_based_model
    from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    base_model = t.cast(
        SqlModel,
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name doris_db.orders,
                    kind FULL,
                    dialect doris,
                    columns (id INT, amount INT)
                );
                SELECT 1 AS id, 10 AS amount;
                """,
                default_dialect="doris",
            )
        ),
    )
    base_snapshot = Snapshot.from_node(base_model, nodes={}, ttl="in 1 week")
    base_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    physical_tbl = exp.to_table(base_snapshot.table_name())
    expected_physical = f"{physical_tbl.db}.{physical_tbl.name}"
    snapshots = {base_snapshot.name: base_snapshot}

    # 1. Async MTMV with excluded_trigger_tables resolves model ref to physical snapshot table
    async_mv_model = t.cast(
        SqlModel,
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name doris_db.orders_mtmv,
                    kind VIEW (materialized true),
                    dialect doris,
                    columns (id INT, amount INT),
                    physical_properties (
                        build = IMMEDIATE,
                        refresh = AUTO,
                        excluded_trigger_tables = doris_db.orders
                    )
                );
                SELECT id, amount FROM doris_db.orders;
                """,
                default_dialect="doris",
            )
        ),
    )
    rendered_async_props = async_mv_model.render_physical_properties(
        snapshots=snapshots, engine_adapter=adapter
    )
    adapter.create_view(
        async_mv_model.name,
        async_mv_model.render_query() or parse_one("SELECT id, amount FROM doris_db.orders"),
        replace=False,
        materialized=True,
        target_columns_to_types=async_mv_model.columns_to_types,
        view_properties=rendered_async_props,
    )
    async_ddl = to_sql_calls(adapter)[-1]
    assert f"'excluded_trigger_tables'='{expected_physical}'" in async_ddl

    # 2. Sync MV with source_table resolves model ref to physical snapshot table for DROP ... ON
    adapter.cursor.execute.reset_mock()
    sync_mv_model = t.cast(
        SqlModel,
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name doris_db.orders_sync_mv,
                    kind VIEW (materialized true),
                    dialect doris,
                    physical_properties (
                        materialized_type = SYNC,
                        source_table = doris_db.orders
                    )
                );
                SELECT id, SUM(amount) AS total FROM doris_db.orders GROUP BY id;
                """,
                default_dialect="doris",
            )
        ),
    )
    rendered_sync_props = sync_mv_model.render_physical_properties(
        snapshots=snapshots, engine_adapter=adapter
    )
    adapter.create_view(
        sync_mv_model.name,
        sync_mv_model.render_query(snapshots=snapshots)
        or parse_one("SELECT id, SUM(amount) AS total FROM doris_db.orders GROUP BY id"),
        replace=True,
        materialized=True,
        view_properties=rendered_sync_props,
    )
    sync_calls = to_sql_calls(adapter)
    assert sync_calls[0] == "USE `doris_db`"
    assert sync_calls[1] == (
        f"DROP MATERIALIZED VIEW IF EXISTS `orders_sync_mv` ON `{physical_tbl.db}`.`{physical_tbl.name}`"
    )
    assert sync_calls[-1].startswith("CREATE MATERIALIZED VIEW `orders_sync_mv` AS ")


def test_dataframe_seed_all_string_coercion_and_batch_size(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    import pandas as pd

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    assert adapter.DEFAULT_BATCH_SIZE == 500

    # 1. All-TEXT schema (e.g. from columns_to_types_from_df on a string CSV seed) coerces first column to VARCHAR(255)
    adapter.create_table(
        "db.country_seed",
        target_columns_to_types={
            "country_code": exp.DataType.build("text"),
            "country_name": exp.DataType.build("text"),
        },
    )

    # 2. Schema with DOUBLE + TEXT moves TEXT column to position 0 and coerces it to VARCHAR(255)
    adapter.create_table(
        "db.metric_seed",
        target_columns_to_types={
            "score": exp.DataType.build("double"),
            "metric_name": exp.DataType.build("text"),
        },
    )

    # 3. Explicit unique_key on a TEXT column coerces the key column to VARCHAR(255)
    adapter.create_table(
        "db.explicit_text_uk",
        target_columns_to_types={
            "payload": exp.DataType.build("text"),
            "code": exp.DataType.build("text"),
        },
        table_properties={"unique_key": exp.to_column("code")},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`country_seed` (`country_code` VARCHAR(255), `country_name` STRING)",
        "CREATE TABLE IF NOT EXISTS `db`.`metric_seed` (`metric_name` VARCHAR(255), `score` DOUBLE)",
        "CREATE TABLE IF NOT EXISTS `db`.`explicit_text_uk` (`code` VARCHAR(255), `payload` STRING) UNIQUE KEY (`code`) DISTRIBUTED BY HASH (`code`) BUCKETS 10",
    ]

    # 4. Explicit unique_key on a non-coercible invalid key type (DOUBLE) raises SQLMeshError
    with pytest.raises(SQLMeshError, match="not supported as an OLAP key column"):
        adapter.create_table(
            "db.bad_double_uk",
            target_columns_to_types={
                "score": exp.DataType.build("DOUBLE", dialect="doris"),
                "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            },
            table_properties={"unique_key": exp.to_column("score")},
        )

    # 5. DataFrame CTAS with pretty_sql=True and 500 rows succeeds without RecursionError
    pretty_adapter = make_mocked_engine_adapter(DorisEngineAdapter, pretty_sql=True)
    df = pd.DataFrame({"code": [f"c_{i}" for i in range(500)], "val": list(range(500))})
    pretty_adapter.ctas("db.df_seed", df)
    df_sql_calls = to_sql_calls(pretty_adapter)
    assert len(df_sql_calls) == 1
    assert "CREATE TABLE IF NOT EXISTS `db`.`df_seed`" in df_sql_calls[0]


def test_iceberg_bucket_and_truncate_partition_transforms(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    from sqlmesh.core.dialect import parse
    from sqlmesh.core.model import SqlModel, load_sql_based_model

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    iceberg_model = t.cast(
        SqlModel,
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name iceberg_catalog.silver.curated_events,
                    kind FULL,
                    dialect doris,
                    table_format iceberg,
                    partitioned_by (day(event_ts), bucket(16, user_id), truncate(10, region))
                );
                SELECT
                    user_id::BIGINT AS user_id,
                    event_ts::DATETIME AS event_ts,
                    region::STRING AS region
                FROM iceberg_catalog.bronze.raw_events;
                """,
                default_dialect="doris",
            )
        ),
    )

    adapter.create_table(
        iceberg_model.name,
        target_columns_to_types=iceberg_model.columns_to_types or {},
        table_format=iceberg_model.table_format,
        partitioned_by=iceberg_model.partitioned_by,
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `iceberg_catalog`.`silver`.`curated_events` (`user_id` BIGINT, `event_ts` DATETIME, `region` STRING) ENGINE=iceberg PARTITION BY LIST (DAY(`event_ts`), BUCKET(16, `user_id`), TRUNCATE(10, `region`)) ()"
    ]


def test_ctas_auto_partition_non_nullable_and_union_key_reordering(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. CTAS with AUTO PARTITION BY RANGE wraps partition column projection in NON_NULLABLE(...)
    adapter.ctas(
        "db.ctas_auto_part",
        parse_one("SELECT payload, id, ds FROM db.src", read="doris"),
        target_columns_to_types={
            "payload": exp.DataType.build("STRING", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
        partitioned_by=[parse_one("RANGE(DATE_TRUNC(ds, 'DAY'))", read="doris")],
        table_properties={"auto_partition": exp.Literal.string("true")},
    )

    # 2. Unannotated CTAS with UNION ALL reorders projections in all branches for unique_key prefix
    adapter.ctas(
        "db.ctas_union_reordered",
        parse_one(
            "SELECT name, id FROM db.src1 UNION ALL SELECT name, id FROM db.src2", read="doris"
        ),
        table_properties={"unique_key": exp.to_column("id")},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`ctas_auto_part` AUTO PARTITION BY RANGE (DATE_TRUNC(`ds`, 'DAY')) () AS SELECT CAST(`id` AS INT) AS `id`, NON_NULLABLE(CAST(`ds` AS DATE)) AS `ds`, CAST(`payload` AS STRING) AS `payload` FROM (SELECT `payload`, `id`, `ds` FROM `db`.`src`) AS `_subquery`",
        "CREATE TABLE IF NOT EXISTS `db`.`ctas_union_reordered` UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10 AS SELECT `id`, `name` FROM `db`.`src1` UNION ALL SELECT `id`, `name` FROM `db`.`src2`",
    ]


def test_delete_tautology_and_union_subquery_and_dedup_partition_overwrite(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Literal tautology 1=1 routes to TRUNCATE TABLE; contradiction 1=0 is a no-op
    adapter.delete_from("db.tbl", where="1 = 1")
    adapter.delete_from("db.tbl", where="1 = 0")

    # 2. IN subquery with UNION resolves the projected column name from the first SELECT branch
    adapter.delete_from(
        "db.tbl",
        where="id IN (SELECT src_id FROM db.a UNION SELECT src_id FROM db.b)",
    )

    # 3. IN subquery with unaliased expression aliases the projection for _t2 join
    adapter.delete_from(
        "db.tbl",
        where="id IN (SELECT other_id + 1 FROM db.a)",
    )

    assert to_sql_calls(adapter) == [
        "TRUNCATE TABLE `db`.`tbl`",
        "DELETE FROM `db`.`tbl` AS `_t1` USING (SELECT `src_id` FROM `db`.`a` UNION SELECT `src_id` FROM `db`.`b`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`src_id`",
        "DELETE FROM `db`.`tbl` AS `_t1` USING (SELECT `other_id` + 1 AS `_col_0` FROM `db`.`a`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`_col_0`",
    ]

    # 4. insert_overwrite_by_partition deduplicates time transforms to finest unit (MONTH) and strips table qualifiers
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "_get_temp_table",
        return_value=exp.table_("__temp_tbl", db="db"),
    )
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[("2024-01-01", "2024-02-01")],
    )
    adapter.insert_overwrite_by_partition(
        "db.events",
        parse_one("SELECT id, ds FROM db.src", read="doris"),
        partitioned_by=[
            parse_one("YEAR(src.ds)", read="doris"),
            parse_one("MONTH(src.ds)", read="doris"),
        ],
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATE"),
        },
    )
    overwrite_calls = to_sql_calls(adapter)
    assert (
        "SELECT DISTINCT DATE_TRUNC(`ds`, 'month') AS `_low_0`, DATE_ADD(DATE_TRUNC(`ds`, 'month'), INTERVAL 1 MONTH) AS `_high_0` FROM `db`.`__temp_tbl`"
        in overwrite_calls
    )
    assert (
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2024-02-01'"
        in overwrite_calls
    )


def test_sync_mv_inferred_source_table_on_replace_and_unqualified_get_data_objects(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter, patch_get_data_objects=False)

    # 1. Sync MV with replace=True and no explicit source_table in view_properties infers base table from query
    mocker.patch.object(adapter, "get_data_object", return_value=None)
    adapter.create_view(
        "sync_rollup_mv",
        parse_one("SELECT id, SUM(amount) AS total FROM prod_db.orders GROUP BY id", read="doris"),
        replace=True,
        materialized=True,
        view_properties={"materialized_type": exp.var("SYNC")},
    )
    assert to_sql_calls(adapter) == [
        "USE `prod_db`",
        "DROP MATERIALIZED VIEW IF EXISTS `sync_rollup_mv` ON `prod_db`.`orders`",
        "USE `prod_db`",
        "CREATE MATERIALIZED VIEW `sync_rollup_mv` AS SELECT `id` AS `mv_id`, SUM(`amount`) AS `total` FROM `prod_db`.`orders` GROUP BY `id`",
    ]

    # 2. _get_data_objects with empty schema uses DATABASE() instead of empty string literal
    adapter.cursor.execute.reset_mock()
    fetchall_spy = mocker.patch.object(
        adapter,
        "fetchall",
        side_effect=[
            [("active_db", "tbl1", "BASE TABLE")],
            [],
        ],
    )
    objs = adapter._get_data_objects("")
    assert len(objs) == 1
    assert objs[0].schema_name == "active_db"
    first_query_sql = fetchall_spy.call_args_list[0].args[0].sql("doris")
    assert "table_schema = DATABASE()" in first_query_sql
    second_query_sql = fetchall_spy.call_args_list[1].args[0]
    assert "mv_infos('database' = 'active_db')" in second_query_sql


def test_ctas_string_key_coercion_and_internal_iceberg_ctas(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. CTAS with explicit unique_key on TEXT column coerces CAST(code AS VARCHAR(255)) and reorders key first
    adapter.ctas(
        "db.seed_uk_ctas",
        parse_one("SELECT payload, code FROM db.raw_seed", read="doris"),
        target_columns_to_types={
            "payload": exp.DataType.build("text"),
            "code": exp.DataType.build("text"),
        },
        table_properties={"unique_key": exp.to_column("code")},
    )

    # 2. CTAS with all-TEXT columns (implicit DUPLICATE KEY on first column) coerces first column CAST to VARCHAR(255)
    adapter.ctas(
        "db.seed_dup_ctas",
        parse_one("SELECT country_code, country_name FROM db.raw_countries", read="doris"),
        target_columns_to_types={
            "country_code": exp.DataType.build("text"),
            "country_name": exp.DataType.build("text"),
        },
    )

    # 3. 2-part internal Iceberg CTAS (table_format="iceberg") creates table with ENGINE=iceberg and inserts rows
    adapter.ctas(
        "silver.iceberg_events",
        parse_one("SELECT id, ds FROM bronze.events", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("BIGINT"),
            "ds": exp.DataType.build("DATE"),
        },
        table_format="iceberg",
        partitioned_by=[parse_one("DAY(ds)", dialect="doris")],
        table_properties={"write-format": exp.Literal.string("parquet")},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`seed_uk_ctas` UNIQUE KEY (`code`) DISTRIBUTED BY HASH (`code`) BUCKETS 10 AS SELECT CAST(`code` AS VARCHAR(255)) AS `code`, CAST(`payload` AS STRING) AS `payload` FROM (SELECT `payload`, `code` FROM `db`.`raw_seed`) AS `_subquery`",
        "CREATE TABLE IF NOT EXISTS `db`.`seed_dup_ctas` AS SELECT CAST(`country_code` AS VARCHAR(255)) AS `country_code`, CAST(`country_name` AS STRING) AS `country_name` FROM (SELECT `country_code`, `country_name` FROM `db`.`raw_countries`) AS `_subquery`",
        "CREATE TABLE IF NOT EXISTS `silver`.`iceberg_events` (`id` BIGINT, `ds` DATE) ENGINE=iceberg PARTITION BY LIST (DAY(`ds`)) () PROPERTIES ('write-format'='parquet')",
        "INSERT INTO `silver`.`iceberg_events` (`id`, `ds`) SELECT CAST(`id` AS BIGINT) AS `id`, CAST(`ds` AS DATE) AS `ds` FROM (SELECT `id`, `ds` FROM `bronze`.`events`) AS `_subquery`",
    ]


def test_self_referencing_replace_query_on_new_table(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch.object(adapter, "get_data_object", return_value=None)

    adapter.replace_query(
        "db.self_ref_tbl",
        parse_one("SELECT id, name FROM db.self_ref_tbl", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("VARCHAR(50)"),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`self_ref_tbl` (`id` INT, `name` VARCHAR(50))",
        "INSERT INTO `db`.`self_ref_tbl` (`id`, `name`) SELECT CAST(`id` AS INT) AS `id`, CAST(`name` AS VARCHAR(50)) AS `name` FROM (SELECT `id`, `name` FROM `db`.`self_ref_tbl`) AS `_subquery`",
    ]


def test_delete_literal_tuple_in_and_subquery_duplicate_aliases_and_concat_ws_guard(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Literal tuple IN (e.g. from SQLMesh state sync snapshot_id_filter) rewrites to grouped single-column IN
    adapter.delete_from(
        "sqlmesh._snapshots",
        parse_one(
            "(name, identifier) IN (('model_a', 'id_1'), ('model_a', 'id_2'), ('model_b', 'id_3'))",
            read="doris",
        ),
    )

    # 2. Composite subquery selecting duplicate short column names (`u.id, m.id`) aliases the second column (`_col_1`)
    adapter.delete_from(
        "db.pair_links",
        parse_one(
            "(a_id, b_id) IN (SELECT u.id, m.id FROM db.users u JOIN db.members m ON u.org_id = m.org_id)",
            read="doris",
        ),
    )

    # 3. CONCAT_WS on LHS with a single non-CONCAT_WS column in RHS subquery does not unwrap into mismatched arity
    adapter.delete_from(
        "db.target_tbl",
        parse_one(
            "CONCAT_WS('__', a, b) IN (SELECT combined_key FROM db.staging_keys)",
            read="doris",
        ),
    )

    assert to_sql_calls(adapter) == [
        "DELETE FROM `sqlmesh`.`_snapshots` WHERE `name` = 'model_a' AND `identifier` IN ('id_1', 'id_2')",
        "DELETE FROM `sqlmesh`.`_snapshots` WHERE `name` = 'model_b' AND `identifier` IN ('id_3')",
        "DELETE FROM `db`.`pair_links` AS `_t1` USING (SELECT `u`.`id`, `m`.`id` AS `_col_1` FROM `db`.`users` AS `u` JOIN `db`.`members` AS `m` ON `u`.`org_id` = `m`.`org_id`) AS `_t2` WHERE `_t1`.`a_id` = `_t2`.`id` AND `_t1`.`b_id` = `_t2`.`_col_1`",
        "DELETE FROM `db`.`target_tbl` WHERE CONCAT_WS('__', `a`, `b`) IN (SELECT `combined_key` FROM `db`.`staging_keys`)",
    ]


def test_to_sql_root_int128_and_nested_struct_pretty_sql_and_fetchdf_reconnect(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    import pandas as pd

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Root exp.DataType INT128 replacement
    assert adapter._to_sql(exp.DataType.build("INT128")) == "LARGEINT"

    # 2. Nested STRUCT with INT128 under _pretty_sql = True
    adapter._pretty_sql = True
    nested_struct = exp.DataType.build(
        "STRUCT<outer_id: INT128, nested: STRUCT<inner_id: INT128, label: VARCHAR(20)>>",
        dialect="doris",
    )
    assert (
        adapter._to_sql(nested_struct)
        == "STRUCT<`outer_id`: LARGEINT, `nested`: STRUCT<`inner_id`: LARGEINT, `label`: VARCHAR(20)>>"
    )
    adapter._pretty_sql = False

    # 3. _fetch_native_df retries once on non-timeout OperationalError(2013) disconnect
    class OperationalError(Exception):
        pass

    close_spy = mocker.spy(adapter._connection_pool, "close")
    fetch_calls = 0

    def fake_read_sql_query(sql, con, *args, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            raise OperationalError(2013, "Lost connection to MySQL server during query")
        return pd.DataFrame({"id": [1]})

    mocker.patch("pandas.io.sql.read_sql_query", side_effect=fake_read_sql_query)
    df = adapter.fetchdf("SELECT 1 AS id")
    assert len(df) == 1
    assert fetch_calls == 2
    assert close_spy.call_count == 1

    # 4. _fetch_native_df does NOT retry non-read-only queries on disconnect
    fetch_calls = 0
    close_spy.reset_mock()
    with pytest.raises(OperationalError):
        adapter.fetchdf("WITH `cte` AS (SELECT 1) INSERT INTO `tbl` SELECT * FROM `cte`")
    assert fetch_calls == 1
    assert close_spy.call_count == 1


def test_strip_for_update_and_multi_col_index_and_partition_by_alias_and_deferred_mv_audit_refresh(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Strip FOR UPDATE locks from SELECT statements (e.g. EnvironmentState.finalize)
    locked_select = (
        exp.select("plan_id")
        .from_("sqlmesh._environments")
        .where(exp.column("name").eq("prod"))
        .lock()
    )
    adapter.execute(locked_select)

    # 2. Multi-column composite index creation is skipped gracefully (Doris only supports single-column indexes)
    adapter.create_index("sqlmesh._snapshots", "idx_snapshots", ("name", "identifier"))

    # 3. partition_by alias in table_properties works for OLAP AUTO PARTITION BY RANGE
    adapter.create_table(
        "db.alias_part_tbl",
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATE"),
        },
        table_properties={
            "partition_by": parse_one("RANGE(DATE_TRUNC(ds, 'day'))", read="doris"),
            "auto_partition": exp.Literal.string("true"),
        },
    )

    # 4. Async materialized view with build='DEFERRED' and has_audits=True triggers REFRESH MATERIALIZED VIEW ... AUTO
    adapter.create_view(
        "db.deferred_audited_mv",
        parse_one("SELECT id, ds FROM db.alias_part_tbl", read="doris"),
        replace=False,
        materialized=True,
        materialized_properties={"has_audits": True},
        view_properties={
            "build": exp.Literal.string("DEFERRED"),
            "refresh": exp.Literal.string("COMPLETE ON MANUAL"),
            "partition_by": exp.Tuple(expressions=[exp.column("ds")]),
        },
    )

    assert to_sql_calls(adapter) == [
        "SELECT `plan_id` FROM `sqlmesh`.`_environments` WHERE `name` = 'prod'",
        "CREATE TABLE IF NOT EXISTS `db`.`alias_part_tbl` (`id` INT, `ds` DATE NOT NULL) AUTO PARTITION BY RANGE (DATE_TRUNC(`ds`, 'DAY')) ()",
        "CREATE MATERIALIZED VIEW `db`.`deferred_audited_mv` BUILD DEFERRED REFRESH COMPLETE ON MANUAL PARTITION BY (`ds`) AS SELECT `id`, `ds` FROM `db`.`alias_part_tbl`",
        "REFRESH MATERIALIZED VIEW `db`.`deferred_audited_mv` AUTO",
    ]


def test_state_sync_or_key_delete_and_boolean_column_and_alter_key_coercion_guard(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    from sqlmesh.core.snapshot import SnapshotId, SnapshotNameVersion
    from sqlmesh.core.state_sync.db.utils import snapshot_id_filter, snapshot_name_version_filter

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    assert adapter.SUPPORTS_TUPLE_IN is False

    # 1. snapshot_id_filter with SUPPORTS_TUPLE_IN=False produces OR of AND equalities;
    #    delete_from decomposes it into zero-OR grouped IN deletes.
    sids = [
        SnapshotId(name="db.model_a", identifier="id_1"),
        SnapshotId(name="db.model_a", identifier="id_2"),
        SnapshotId(name="db.model_b", identifier="id_3"),
    ]
    for where in snapshot_id_filter(adapter, sids, batch_size=100):
        adapter.delete_from("sqlmesh._snapshots", where=where)

    # 2. IntervalState._delete_intervals_by_dev_version passes `where.and_(exp.column("is_dev"))`
    snvs = [
        SnapshotNameVersion(name="db.model_a", version="v1"),
        SnapshotNameVersion(name="db.model_a", version="v2"),
    ]
    for where in snapshot_name_version_filter(
        adapter, snvs, version_column_name="dev_version", alias=None, batch_size=100
    ):
        adapter.delete_from("sqlmesh._intervals", where.and_(exp.column("is_dev")))

    # 3. EnvironmentState.delete_expired_environments passes single-column OR `environment_name = 'dev1' OR environment_name = 'dev2'`
    adapter.delete_from(
        "sqlmesh._environment_statements",
        where=exp.or_(
            exp.column("environment_name").eq(exp.Literal.string("dev_1")),
            exp.column("environment_name").eq(exp.Literal.string("dev_2")),
        ),
    )

    assert to_sql_calls(adapter) == [
        "DELETE FROM `sqlmesh`.`_snapshots` WHERE `name` = 'db.model_a' AND `identifier` IN ('id_1', 'id_2')",
        "DELETE FROM `sqlmesh`.`_snapshots` WHERE `name` = 'db.model_b' AND `identifier` IN ('id_3')",
        "DELETE FROM `sqlmesh`.`_intervals` WHERE `name` = 'db.model_a' AND `dev_version` IN ('v1', 'v2') AND `is_dev` = TRUE",
        "DELETE FROM `sqlmesh`.`_environment_statements` WHERE `environment_name` IN ('dev_1', 'dev_2')",
    ]

    # 4. get_alter_operations suppresses invalid VARCHAR(255) -> STRING type change on an OLAP key column
    #    while preserving ADD COLUMN and non-key column widening.
    adapter.cursor.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            # DESCRIBE db.current_tbl: `code` is the OLAP key column coerced to VARCHAR(255), `notes` is a non-key VARCHAR(100)
            [
                ("code", "VARCHAR(255)", "Yes", "true", None, ""),
                ("notes", "VARCHAR(100)", "Yes", "false", None, ""),
            ],
            # DESCRIBE db.target_tbl: `id` was added, `code` is STRING, `notes` widened to STRING
            [
                ("id", "INT", "Yes", "true", None, ""),
                ("code", "TEXT", "Yes", "false", None, ""),
                ("notes", "TEXT", "Yes", "false", None, ""),
            ],
        ],
    )
    ops = adapter.get_alter_operations("db.current_tbl", "db.target_tbl")
    adapter.cursor.reset_mock()
    adapter.alter_table(ops)
    assert to_sql_calls(adapter) == [
        "ALTER TABLE `db`.`current_tbl` ADD COLUMN `id` INT",
        "ALTER TABLE `db`.`current_tbl` MODIFY COLUMN `notes` STRING",
    ]


def test_unannotated_view_column_comments_and_starrocks_property_parity(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Unannotated VIEW and MATERIALIZED VIEW with column_descriptions (as called by ViewStrategy.create
    #    where target_columns_to_types=None) infer projection column names to emit column comments.
    adapter.create_view(
        "db.unannotated_view",
        parse_one("SELECT id, name FROM db.users", read="doris"),
        replace=False,
        table_description="Users view",
        column_descriptions={"id": "Primary user ID"},
    )
    adapter.create_view(
        "db.unannotated_mv",
        parse_one("SELECT id, name FROM db.users", read="doris"),
        replace=False,
        materialized=True,
        table_description="Users MV",
        column_descriptions={"id": "Primary user ID"},
    )

    # 2. Parenthesized string forms for unique_key and order_by, structured distributed_by with `columns=...`,
    #    and tuple generic properties (e.g. excluded_trigger_tables = ('db.t1', 'db.t2')).
    adapter.create_table(
        "db.paren_keys_tbl",
        target_columns_to_types={
            "name": exp.DataType.build("VARCHAR(50)"),
            "id": exp.DataType.build("INT"),
            "dt": exp.DataType.build("DATE"),
        },
        table_properties={
            "unique_key": exp.Literal.string("(id, dt)"),
            "order_by": exp.Literal.string("'(dt, id)'"),
            "distributed_by": exp.Tuple(
                expressions=[
                    exp.EQ(this=exp.column("columns"), expression=exp.Literal.string("(id, dt)")),
                    exp.EQ(this=exp.column("buckets"), expression=exp.Literal.number(12)),
                ]
            ),
            "excluded_trigger_tables": exp.Tuple(
                expressions=[exp.Literal.string("db.t1"), exp.Literal.string("db.t2")]
            ),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE VIEW `db`.`unannotated_view` COMMENT 'Users view' AS SELECT `id`, `name` FROM `db`.`users`",
        "CREATE MATERIALIZED VIEW `db`.`unannotated_mv` (`id` COMMENT 'Primary user ID', `name`) COMMENT 'Users MV' AS SELECT `id`, `name` FROM `db`.`users`",
        "CREATE TABLE IF NOT EXISTS `db`.`paren_keys_tbl` (`id` INT, `dt` DATE, `name` VARCHAR(50)) UNIQUE KEY (`id`, `dt`) ORDER BY (`dt`, `id`) DISTRIBUTED BY HASH (`id`, `dt`) BUCKETS 12 PROPERTIES ('excluded_trigger_tables'='db.t1,db.t2')",
    ]

    # 3. Fail-fast validation for unsupported primary_key, typo `partition`, conflicting partition definitions,
    #    and invalid RANDOM(col) / column-less HASH distribution.
    cols = {"id": exp.DataType.build("INT"), "dt": exp.DataType.build("DATE")}

    with pytest.raises(SQLMeshError, match="does not support 'primary_key'"):
        adapter.create_table(
            "db.bad_pk",
            target_columns_to_types=cols,
            table_properties={"primary_key": exp.to_column("id")},
        )

    with pytest.raises(SQLMeshError, match="Invalid property 'partition'"):
        adapter.create_table(
            "db.bad_partition_typo",
            target_columns_to_types=cols,
            table_properties={"partition": exp.to_column("dt")},
        )

    with pytest.raises(SQLMeshError, match="Multiple partition definitions"):
        adapter.create_table(
            "db.conflict_part",
            target_columns_to_types=cols,
            partitioned_by=[exp.to_column("dt")],
            table_properties={"partition_by": exp.to_column("dt")},
        )

    with pytest.raises(
        SQLMeshError, match="DISTRIBUTED BY RANDOM does not accept column expressions"
    ):
        adapter.create_table(
            "db.bad_random_cols",
            target_columns_to_types=cols,
            table_properties={"distributed_by": exp.Literal.string("RANDOM(id)")},
        )

    with pytest.raises(
        SQLMeshError, match="DISTRIBUTED BY HASH requires at least one column expression"
    ):
        adapter.create_table(
            "db.bad_hash_no_cols",
            target_columns_to_types=cols,
            table_properties={"distributed_by": exp.Literal.string("HASH BUCKETS 10")},
        )


def test_is_bool_rewrites_negative_or_keys_union_delete_and_table_diff_ctas(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. `IS TRUE` / `IS FALSE` / `IS NOT TRUE` in UPDATE (e.g. SnapshotState.unpause_snapshots) and DELETE
    adapter.update_table(
        "sqlmesh._snapshots",
        {"unpaused_ts": 1700000000},
        where=exp.and_(
            exp.column("forward_only").is_(exp.true()),
            exp.column("name").eq(exp.Literal.string("db.m1")),
        ),
    )
    adapter.delete_from(
        "sqlmesh._intervals",
        where=exp.and_(
            exp.column("is_dev").is_(exp.true()).not_(),
            exp.column("name").eq(exp.Literal.string("db.m1")),
        ),
    )

    # 2. Top-level OR of key rows with negative number literals and bare boolean / IS TRUE conjuncts
    adapter.delete_from(
        "sqlmesh._intervals",
        where=parse_one(
            "(id = -1 AND is_dev) OR (id = -2 AND NOT is_dev)",
            read="doris",
        ),
    )

    # 3. DELETE ... USING with a UNION ALL subquery containing duplicate/unaliased projection names
    union_where = exp.Tuple(expressions=[exp.column("k1"), exp.column("k2")]).isin(
        query=parse_one(
            "SELECT a.id, b.id FROM db.a AS a JOIN db.b AS b ON a.id = b.id "
            "UNION ALL "
            "SELECT c.id, d.id FROM db.c AS c JOIN db.d AS d ON c.id = d.id",
            read="doris",
        )
    )
    adapter.delete_from("db.target_tbl", where=union_where)

    # 4. TableDiff-style unannotated CTAS (`SELECT *, CASE ... AS row_full_match FROM __stats`)
    #    promotes the integer indicator column (`s_exists`) to index 0 of `__stats` so string/float
    #    first columns do not fail Doris's implicit DUPLICATE KEY rule.
    table_diff_query = parse_one(
        "WITH `__source` AS (SELECT `s`.`name` AS `name`, `s`.`id` AS `__sqlmesh_join_key` FROM `db`.`src` AS `s`), "
        "`__target` AS (SELECT `t`.`name` AS `name`, `t`.`id` AS `__sqlmesh_join_key` FROM `db`.`tgt` AS `t`), "
        "`__stats` AS ("
        "SELECT `s`.`name` AS `s__name`, `t`.`name` AS `t__name`, "
        "IF(NOT `s`.`__sqlmesh_join_key` IS NULL, 1, 0) AS `s_exists`, "
        "IF(NOT `t`.`__sqlmesh_join_key` IS NULL, 1, 0) AS `t_exists` "
        "FROM `__source` AS `s` LEFT JOIN `__target` AS `t` ON `s`.`__sqlmesh_join_key` = `t`.`__sqlmesh_join_key`"
        ") "
        "SELECT *, CASE WHEN `s__name` = `t__name` THEN 1 ELSE 0 END AS `row_full_match` FROM `__stats`",
        read="doris",
    )
    adapter.ctas("sqlmesh_temp.diff", table_diff_query)

    assert to_sql_calls(adapter) == [
        "UPDATE `sqlmesh`.`_snapshots` SET `unpaused_ts` = 1700000000 WHERE `forward_only` = TRUE AND `name` = 'db.m1'",
        "DELETE FROM `sqlmesh`.`_intervals` WHERE `is_dev` = FALSE AND `name` = 'db.m1'",
        "DELETE FROM `sqlmesh`.`_intervals` WHERE `id` = -1 AND `is_dev` IN (TRUE)",
        "DELETE FROM `sqlmesh`.`_intervals` WHERE `id` = -2 AND `is_dev` IN (FALSE)",
        "DELETE FROM `db`.`target_tbl` AS `_t1` USING (SELECT `a`.`id`, `b`.`id` AS `_col_1` FROM `db`.`a` AS `a` JOIN `db`.`b` AS `b` ON `a`.`id` = `b`.`id` UNION ALL SELECT `c`.`id`, `d`.`id` FROM `db`.`c` AS `c` JOIN `db`.`d` AS `d` ON `c`.`id` = `d`.`id`) AS `_t2` WHERE `_t1`.`k1` = `_t2`.`id` AND `_t1`.`k2` = `_t2`.`_col_1`",
        "CREATE TABLE IF NOT EXISTS `sqlmesh_temp`.`diff` AS WITH `__source` AS (SELECT `s`.`name` AS `name`, `s`.`id` AS `__sqlmesh_join_key` FROM `db`.`src` AS `s`), `__target` AS (SELECT `t`.`name` AS `name`, `t`.`id` AS `__sqlmesh_join_key` FROM `db`.`tgt` AS `t`), `__stats` AS (SELECT CASE WHEN NOT `s`.`__sqlmesh_join_key` IS NULL THEN 1 ELSE 0 END AS `s_exists`, CASE WHEN NOT `t`.`__sqlmesh_join_key` IS NULL THEN 1 ELSE 0 END AS `t_exists`, `s`.`name` AS `s__name`, `t`.`name` AS `t__name` FROM `__source` AS `s` LEFT JOIN `__target` AS `t` ON `s`.`__sqlmesh_join_key` = `t`.`__sqlmesh_join_key`) SELECT *, CASE WHEN `s__name` = `t__name` THEN 1 ELSE 0 END AS `row_full_match` FROM `__stats`",
    ]

    # 5. Unannotated 2-part/internal Iceberg CTAS raises a clear SQLMeshError instead of silently dropping ENGINE=iceberg
    with pytest.raises(
        SQLMeshError,
        match="Doris does not support `CREATE TABLE ... AS SELECT` with `ENGINE=iceberg`",
    ):
        adapter.ctas(
            "silver.unannotated_iceberg",
            parse_one("SELECT id, ds FROM bronze.events", read="doris"),
            table_format="iceberg",
        )


def test_table_type_keys_random_distribution_mv_exists_and_tuple_ref_resolution(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    from sqlmesh.core.dialect import parse
    from sqlmesh.core.model import SqlModel, load_sql_based_model
    from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    cols = {
        "payload": exp.DataType.build("VARCHAR(50)"),
        "id": exp.DataType.build("INT"),
        "dt": exp.DataType.build("DATE"),
    }

    # 1. StarRocks-style `table_type` ('PRIMARY' | 'UNIQUE' | 'DUPLICATE' | 'AGGREGATE') + `keys`
    adapter.create_table(
        "db.sr_primary",
        target_columns_to_types=cols,
        table_properties={
            "table_type": exp.Literal.string("PRIMARY"),
            "keys": exp.Tuple(expressions=[exp.to_column("id"), exp.to_column("dt")]),
            "buckets": exp.Literal.number(8),
        },
    )
    adapter.create_table(
        "db.sr_duplicate_random",
        target_columns_to_types=cols,
        table_properties={
            "table_type": exp.Literal.string("DUPLICATE"),
            "keys": exp.Tuple(expressions=[exp.to_column("id")]),
            "distributed_by": exp.Tuple(
                expressions=[
                    exp.EQ(this=exp.column("random"), expression=exp.Literal.string("true")),
                    exp.EQ(this=exp.column("buckets"), expression=exp.Literal.number(6)),
                ]
            ),
        },
    )
    adapter.create_table(
        "db.sr_aggregate",
        target_columns_to_types={"id": exp.DataType.build("INT"), "dt": exp.DataType.build("DATE")},
        table_properties={
            "table_type": exp.Literal.string("AGGREGATE"),
            "keys": exp.Literal.string("(id, dt)"),
            "buckets": exp.Literal.number(4),
        },
    )

    # 2. `create_view(..., materialized=True, replace=False, exists=True)` emits IF NOT EXISTS
    adapter.create_view(
        "db.mv_if_not_exists",
        parse_one("SELECT id FROM db.sr_primary", read="doris"),
        replace=False,
        materialized=True,
        exists=True,
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`sr_primary` (`id` INT, `dt` DATE, `payload` VARCHAR(50)) UNIQUE KEY (`id`, `dt`) DISTRIBUTED BY HASH (`id`) BUCKETS 8",
        "CREATE TABLE IF NOT EXISTS `db`.`sr_duplicate_random` (`id` INT, `payload` VARCHAR(50), `dt` DATE) DUPLICATE KEY (`id`) DISTRIBUTED BY RANDOM BUCKETS 6",
        "CREATE TABLE IF NOT EXISTS `db`.`sr_aggregate` (`id` INT, `dt` DATE) AGGREGATE KEY (`id`, `dt`) DISTRIBUTED BY HASH (`id`) BUCKETS 4",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS `db`.`mv_if_not_exists` AS SELECT `id` FROM `db`.`sr_primary`",
    ]

    # 3. Validation errors for invalid `table_type` / missing `keys` / conflicting `random=true`
    with pytest.raises(SQLMeshError, match="requires 'keys' to be specified"):
        adapter.create_table(
            "db.bad_tt_no_keys",
            target_columns_to_types=cols,
            table_properties={"table_type": exp.Literal.string("UNIQUE")},
        )

    with pytest.raises(SQLMeshError, match="Invalid Doris table_type"):
        adapter.create_table(
            "db.bad_tt_val",
            target_columns_to_types=cols,
            table_properties={
                "table_type": exp.Literal.string("UNKNOWN_MODEL"),
                "keys": exp.to_column("id"),
            },
        )

    with pytest.raises(SQLMeshError, match="Conflicting distributed_by configuration"):
        adapter.create_table(
            "db.bad_dist_conflict",
            target_columns_to_types=cols,
            table_properties={
                "distributed_by": exp.Tuple(
                    expressions=[
                        exp.EQ(this=exp.column("kind"), expression=exp.Literal.string("HASH")),
                        exp.EQ(this=exp.column("random"), expression=exp.Literal.string("true")),
                    ]
                )
            },
        )

    # 4. Tuple `excluded_trigger_tables` + `default_catalog="internal"` resolves 2-part model refs
    #    and strips the leading `internal.` catalog prefix from the physical table name in Doris MTMV DDL.
    adapter.cursor.execute.reset_mock()
    base_model = t.cast(
        SqlModel,
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name doris_db.orders,
                    kind FULL,
                    dialect doris,
                    columns (id INT, amount INT)
                );
                SELECT 1 AS id, 10 AS amount;
                """,
                default_dialect="doris",
            ),
            default_catalog="internal",
        ),
    )
    base_snapshot = Snapshot.from_node(base_model, nodes={}, ttl="in 1 week")
    base_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    physical_tbl = exp.to_table(base_snapshot.table_name())
    expected_physical = f"{physical_tbl.db}.{physical_tbl.name}"
    snapshots = {base_snapshot.name: base_snapshot}

    tuple_mv_model = t.cast(
        SqlModel,
        load_sql_based_model(
            parse(
                """
                MODEL (
                    name doris_db.orders_tuple_mtmv,
                    kind VIEW (materialized true),
                    dialect doris,
                    columns (id INT, amount INT),
                    physical_properties (
                        build = IMMEDIATE,
                        refresh = AUTO,
                        excluded_trigger_tables = ('doris_db.orders', 'ext_cat.ext_db.raw_events')
                    )
                );
                SELECT id, amount FROM doris_db.orders;
                """,
                default_dialect="doris",
            ),
            default_catalog="internal",
        ),
    )
    rendered_tuple_props = tuple_mv_model.render_physical_properties(
        snapshots=snapshots, engine_adapter=adapter
    )
    adapter.create_view(
        tuple_mv_model.name,
        tuple_mv_model.render_query() or parse_one("SELECT id, amount FROM doris_db.orders"),
        replace=False,
        materialized=True,
        target_columns_to_types=tuple_mv_model.columns_to_types,
        view_properties=rendered_tuple_props,
    )
    tuple_mv_ddl = to_sql_calls(adapter)[-1]
    assert (
        f"'excluded_trigger_tables'='{expected_physical},ext_cat.ext_db.raw_events'" in tuple_mv_ddl
    )


def test_time_range_delete_cast_unwrap_partition_key_prefix_scd_type_2_and_reconnect_catalog(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    from datetime import datetime
    from sqlmesh.utils.date import to_time_column

    adapter = make_mocked_engine_adapter(DorisEngineAdapter, default_catalog="internal")

    # 1. insert_overwrite_by_time_partition with `to_time_column` (`CAST('...' AS DATE)`) unwraps
    #    CAST literals in top-level DELETE WHERE while preserving CAST in INSERT SELECT WHERE.
    adapter.insert_overwrite_by_time_partition(
        "db.events",
        parse_one("SELECT id, ds FROM db.src_events", read="doris"),
        start="2024-01-01",
        end="2024-01-31",
        time_column="ds",
        time_formatter=lambda v, _: to_time_column(
            v, exp.DataType.build("DATE", dialect="doris"), "doris"
        ),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
    )

    time_overwrite_calls = to_sql_calls(adapter)
    assert time_overwrite_calls == [
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` <= '2024-01-31'",
        "INSERT INTO `db`.`events` (`id`, `ds`) SELECT `id`, `ds` FROM (SELECT `id`, `ds` FROM `db`.`src_events`) AS `_subquery` WHERE `ds` BETWEEN CAST('2024-01-01' AS DATE) AND CAST('2024-01-31' AS DATE)",
    ]

    # 2. Implicit DUPLICATE KEY prefix ordering for partitioned_by / time_column:
    #    (a) TEXT partition column coerced to VARCHAR(255) and moved before DOUBLE;
    #    (b) DATE partition column moved before VARCHAR column (which cuts off Doris implicit key);
    #    (c) DATE partition column moved within the first 3 columns (`shortkey_max_column_count = 3`).
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "db.part_text_col",
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "amount": exp.DataType.build("DOUBLE", dialect="doris"),
            "ds": exp.DataType.build("TEXT", dialect="doris"),
        },
        partitioned_by=[exp.to_column("ds")],
    )
    adapter.create_table(
        "db.part_after_varchar",
        target_columns_to_types={
            "code": exp.DataType.build("VARCHAR(100)", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
            "amount": exp.DataType.build("DOUBLE", dialect="doris"),
        },
        partitioned_by=[exp.to_column("ds")],
    )
    adapter.create_table(
        "db.part_fourth_col",
        target_columns_to_types={
            "c1": exp.DataType.build("INT", dialect="doris"),
            "c2": exp.DataType.build("INT", dialect="doris"),
            "c3": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
        partitioned_by=[exp.to_column("ds")],
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`part_text_col` (`id` INT, `ds` VARCHAR(255), `amount` DOUBLE)",
        "CREATE TABLE IF NOT EXISTS `db`.`part_after_varchar` (`ds` DATE, `code` VARCHAR(100), `amount` DOUBLE)",
        "CREATE TABLE IF NOT EXISTS `db`.`part_fourth_col` (`c1` INT, `c2` INT, `ds` DATE, `c3` INT)",
    ]

    # 3. SCD Type 2 on an existing table when `default_catalog="internal"` does not mutate `target_table`
    #    in `temp_table` and properly rewrites `db.scd_tbl` references to `__temp_scd_tbl_...`.
    adapter.cursor.execute.reset_mock()
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="abcd1234")
    mocker.patch.object(
        adapter,
        "get_data_object",
        return_value=DataObject(
            catalog="internal", schema="db", name="scd_tbl", type=DataObjectType.TABLE
        ),
    )
    adapter.scd_type_2_by_time(
        target_table="db.scd_tbl",
        source_table=t.cast(
            exp.Select,
            parse_one("SELECT id, name, updated_at FROM db.src_tbl", read="doris"),
        ),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "name": exp.DataType.build("VARCHAR(50)", dialect="doris"),
            "updated_at": exp.DataType.build("DATETIME", dialect="doris"),
            "valid_from": exp.DataType.build("DATETIME", dialect="doris"),
            "valid_to": exp.DataType.build("DATETIME", dialect="doris"),
        },
        execution_time=datetime(2024, 1, 2, 0, 0, 0),
    )
    scd_calls = to_sql_calls(adapter)
    insert_scd_sql = next(c for c in scd_calls if c.startswith("INSERT INTO `db`.`scd_tbl`"))
    assert "FROM `internal`.`db`.`__temp_scd_tbl_abcd1234`" in insert_scd_sql
    assert "FROM `db`.`scd_tbl`" not in insert_scd_sql

    # 4. Reconnect in `_execute` restores active catalog set via `set_current_catalog` and falls back
    #    to `self._connection_pool.close()` when `conn.ping(reconnect=True)` raises on a closed socket.
    class InterfaceError(Exception):
        pass

    adapter.cursor.execute.reset_mock()
    adapter.set_current_catalog("iceberg_cat")
    adapter.cursor.execute.reset_mock()

    conn_mock = mocker.MagicMock()
    conn_mock.ping.side_effect = InterfaceError(0, "Already closed")
    mocker.patch.object(adapter._connection_pool, "get", return_value=conn_mock)
    close_spy = mocker.spy(adapter._connection_pool, "close")

    exec_attempt = 0

    def reconnect_side_effect(sql, *args, **kwargs):
        nonlocal exec_attempt
        exec_attempt += 1
        if exec_attempt == 1:
            raise InterfaceError(0, "Already closed")

    adapter.cursor.execute.side_effect = reconnect_side_effect
    adapter._execute("SELECT 1")

    assert close_spy.call_count == 1
    assert exec_attempt == 1
    assert to_sql_calls(adapter) == [
        "SWITCH `iceberg_cat`",
        "SELECT 1",
    ]


def test_thread_local_state_isolation_time_transform_partition_delete_table_diff_and_pm_features(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    from datetime import date, datetime
    from decimal import Decimal
    import threading
    import pandas as pd
    from sqlmesh.cli.project_init import InitCliMode, ProjectTemplate, _gen_config

    adapter = make_mocked_engine_adapter(
        DorisEngineAdapter, default_catalog="internal", database="analytics"
    )
    assert adapter.SUPPORTS_QUERY_EXECUTION_TRACKING is True

    # 1. Thread-local state isolation for `_active_catalog`, `_needs_catalog_restore`, and
    #    `_last_described_key_cols` across worker threads, `with_settings()`, and `pool.close()`.
    adapter._active_catalog = "internal"
    adapter._needs_catalog_restore = False
    adapter._last_described_key_cols = {"main_key"}

    cloned = t.cast(DorisEngineAdapter, adapter.with_settings(correlation_id="test_corr_id"))
    assert cloned._active_catalog == "internal"
    assert cloned._last_described_key_cols == {"main_key"}

    worker_results: t.Dict[str, t.Any] = {}

    def worker_fn() -> None:
        worker_results["initial_cat"] = adapter._active_catalog
        worker_results["initial_keys"] = set(adapter._last_described_key_cols)
        adapter._active_catalog = "iceberg_worker"
        adapter._needs_catalog_restore = True
        adapter._last_described_key_cols = {"worker_key"}
        worker_results["updated_cat"] = cloned._active_catalog
        worker_results["updated_keys"] = set(cloned._last_described_key_cols)

    thread = threading.Thread(target=worker_fn)
    thread.start()
    thread.join()

    assert worker_results == {
        "initial_cat": None,
        "initial_keys": set(),
        "updated_cat": "iceberg_worker",
        "updated_keys": {"worker_key"},
    }
    # Main thread state was not overwritten by worker thread and survives connection pool close
    adapter._connection_pool.close()
    assert adapter._active_catalog == "internal"
    assert adapter._needs_catalog_restore is False
    assert adapter._last_described_key_cols == {"main_key"}

    # 2. Reconnect in `_restore_active_catalog_on_reconnect` restores both `SWITCH <default_catalog>`
    #    and `USE <database>` when `database` is configured on the default catalog.
    adapter.cursor.execute.reset_mock()
    adapter._needs_catalog_restore = True
    adapter._restore_active_catalog_on_reconnect("SELECT 1")
    assert to_sql_calls(adapter) == [
        "SWITCH `internal`",
        "USE `analytics`",
    ]

    # 3. TableDiff-style query with 4 integer indicators and DOUBLE/STRING user columns promotes all
    #    indicators to the front of `__stats` (covering all 3 slots of `shortkey_max_column_count = 3`).
    adapter.cursor.execute.reset_mock()
    four_indicator_diff = parse_one(
        "WITH `__stats` AS ("
        "SELECT `s`.`score` AS `s__score`, `t`.`score` AS `t__score`, `s`.`bio` AS `s__bio`, "
        "IF(NOT `s`.`k` IS NULL, 1, 0) AS `s_exists`, "
        "IF(NOT `t`.`k` IS NULL, 1, 0) AS `t_exists`, "
        "IF(NOT `s`.`k` IS NULL AND NOT `t`.`k` IS NULL, 1, 0) AS `row_joined`, "
        "IF(`s`.`id` IS NULL AND `t`.`id` IS NULL, 1, 0) AS `null_grain` "
        "FROM `db`.`s` LEFT JOIN `db`.`t` ON `s`.`k` = `t`.`k`"
        ") SELECT *, CASE WHEN `s__score` = `t__score` THEN 1 ELSE 0 END AS `row_full_match` FROM `__stats`",
        read="doris",
    )
    adapter.ctas("sqlmesh_temp.diff_4", four_indicator_diff)
    diff_sql = to_sql_calls(adapter)[0]
    assert (
        "`__stats` AS (SELECT CASE WHEN NOT `s`.`k` IS NULL THEN 1 ELSE 0 END AS `s_exists`, "
        "CASE WHEN NOT `t`.`k` IS NULL THEN 1 ELSE 0 END AS `t_exists`, "
        "CASE WHEN NOT `s`.`k` IS NULL AND NOT `t`.`k` IS NULL THEN 1 ELSE 0 END AS `row_joined`, "
        "CASE WHEN `s`.`id` IS NULL AND `t`.`id` IS NULL THEN 1 ELSE 0 END AS `null_grain`, "
        "`s`.`score` AS `s__score`" in diff_sql
    )

    # 4. Non-star unannotated CTAS infers explicit CAST types (`DOUBLE`, `STRING`, `INT`) and
    #    prefers known valid key columns (`id` INT) or coerces `STRING` to `VARCHAR(255)` at column 0.
    adapter.cursor.execute.reset_mock()
    adapter.ctas(
        "db.unannotated_cast_reordered",
        parse_one(
            "SELECT CAST(score AS DOUBLE) AS score, raw_col, CAST(id AS INT) AS id FROM db.src",
            read="doris",
        ),
    )
    adapter.ctas(
        "db.unannotated_cast_string",
        parse_one(
            "SELECT CAST(score AS DOUBLE) AS score, CAST(label AS STRING) AS label FROM db.src",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`unannotated_cast_reordered` AS SELECT CAST(`id` AS INT) AS `id`, CAST(`score` AS DOUBLE) AS `score`, `raw_col` FROM `db`.`src`",
        "CREATE TABLE IF NOT EXISTS `db`.`unannotated_cast_string` AS SELECT CAST(`label` AS VARCHAR(255)) AS `label`, CAST(`score` AS DOUBLE) AS `score` FROM `db`.`src`",
    ]

    # 5. `replace_query` on an existing table and `scd_type_2_by_column` when column 0 is `DOUBLE`
    #    or `TEXT` reorder/coerce `target_columns_to_types` (and `source_columns`).
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "get_data_object",
        return_value=DataObject(
            catalog="internal", schema="db", name="existing_seed", type=DataObjectType.TABLE
        ),
    )
    seed_df = pd.DataFrame([{"score": 9.5, "code": "us"}])
    adapter.replace_query(
        "db.existing_seed",
        seed_df,
        target_columns_to_types={
            "score": exp.DataType.build("DOUBLE", dialect="doris"),
            "code": exp.DataType.build("TEXT", dialect="doris"),
        },
        source_columns=["score", "code"],
    )
    replace_calls = to_sql_calls(adapter)
    assert replace_calls == [
        "TRUNCATE TABLE `db`.`existing_seed`",
        "INSERT INTO `db`.`existing_seed` (`code`, `score`) SELECT CAST(`code` AS VARCHAR(255)) AS `code`, CAST(`score` AS DOUBLE) AS `score` FROM (SELECT 'us' AS `code`, 9.5 AS `score`) AS `t`",
    ]

    adapter.cursor.execute.reset_mock()
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="efgh5678")
    adapter.scd_type_2_by_column(
        target_table="db.scd_col_tbl",
        source_table=t.cast(
            exp.Select,
            parse_one("SELECT score, id FROM db.src_tbl", read="doris"),
        ),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        check_columns=[exp.column("score")],
        target_columns_to_types={
            "score": exp.DataType.build("DOUBLE", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
            "valid_from": exp.DataType.build("DATETIME", dialect="doris"),
            "valid_to": exp.DataType.build("DATETIME", dialect="doris"),
        },
        execution_time=datetime(2024, 1, 2, 0, 0, 0),
    )
    scd_col_calls = to_sql_calls(adapter)
    assert scd_col_calls[0].startswith(
        "CREATE TABLE IF NOT EXISTS `db`.`scd_col_tbl` (`id` INT, `score` DOUBLE, `valid_from` DATETIME, `valid_to` DATETIME)"
    )

    # 6. `_delete_by_literal_keys` with Python `date`, `datetime`, and `Decimal` values from PyMySQL
    #    emits plain string/number literals (`'2024-01-01'`, `12.5`) instead of `CAST('...' AS DATE)`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "fetchall",
        return_value=[(date(2024, 1, 1), datetime(2024, 1, 1, 12, 30, 0), Decimal("12.50"))],
    )
    adapter._delete_by_literal_keys(
        "db.typed_keys",
        [exp.column("dt"), exp.column("ts"), exp.column("amt")],
        parse_one("SELECT dt, ts, amt FROM db.staging", read="doris", into=exp.Select),
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`typed_keys` WHERE `dt` = '2024-01-01' AND `ts` = '2024-01-01 12:30:00' AND `amt` IN (12.50)"
    ]

    # 7. `sqlmesh init doris` generates a working `state_connection` block (`type: duckdb`).
    doris_init_cfg = _gen_config(
        engine_type="doris",
        settings=None,
        start="2024-01-01",
        template=ProjectTemplate.DEFAULT,
        cli_mode=InitCliMode.DEFAULT,
    )
    assert "state_connection:\n      type: duckdb\n      database: state.db" in doris_init_cfg
    assert "dialect: doris" in doris_init_cfg


def test_union_time_transform_delete_agg_ctas_mv_df_order_and_limit_fold(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    import pandas as pd
    from sqlmesh.core.config.connection import DorisConnectionConfig

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. `_delete_by_time_transform_keys` uses the source subquery's projection column (`src_ds`)
    #    when it differs from the target column (`target_ds`), and supports `UNION ALL` subqueries.
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            [("2024-01-15", "2024-01-16")],
            [("2024-01-01", "2025-01-01")],
        ],
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "DATE_TRUNC(target_ds, 'day') IN (SELECT DATE_TRUNC(src_ds, 'day') FROM db.staging)",
            read="doris",
        ),
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "YEAR(ds) IN (SELECT YEAR(ds) FROM db.s1 UNION ALL SELECT YEAR(ds) FROM db.s2)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT DISTINCT DATE_TRUNC(`src_ds`, 'day') AS `_low_0`, DATE_ADD(DATE_TRUNC(`src_ds`, 'day'), INTERVAL 1 DAY) AS `_high_0` FROM `db`.`staging`",
        "DELETE FROM `db`.`events` WHERE `target_ds` >= '2024-01-15' AND `target_ds` < '2024-01-16'",
        "SELECT DISTINCT DATE_TRUNC(`_col_0`, 'year') AS `_low_0`, DATE_ADD(DATE_TRUNC(`_col_0`, 'year'), INTERVAL 1 YEAR) AS `_high_0` FROM (SELECT `ds` AS `_col_0` FROM `db`.`s1` UNION ALL SELECT `ds` FROM `db`.`s2`) AS `_sq`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2025-01-01'",
    ]

    # 2. `CONCAT_WS` multi-column unwrap in `delete_from` supports `UNION ALL` subqueries,
    #    `_execute_delete_with_subquery` aliases unaliased string literals (`'us' AS _col_0`),
    #    and `_delete_key_rows` deduplicates repeated literal keys.
    adapter.cursor.execute.reset_mock()
    key_exp = exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", exp.column("id"), exp.column("ds"))
    union_subq = exp.union(
        exp.select(key_exp).from_("db.s1"),
        exp.select(key_exp).from_("db.s2"),
        distinct=False,
    )
    adapter.delete_from("db.target", where=key_exp.isin(query=union_subq))
    adapter.delete_from(
        "db.target",
        where=parse_one("(region, id) IN (SELECT 'us', id FROM db.staging)", read="doris"),
    )
    adapter.delete_from(
        "db.target",
        where=parse_one(
            "(region, id) IN (('us', 1), ('us', 1), ('us', 2))",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`target` AS `_t1` USING (SELECT `id`, `ds` FROM `db`.`s1` UNION ALL SELECT `id`, `ds` FROM `db`.`s2`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`ds` = `_t2`.`ds`",
        "DELETE FROM `db`.`target` AS `_t1` USING (SELECT 'us' AS `_col_0`, `id` FROM `db`.`staging`) AS `_t2` WHERE `_t1`.`region` = `_t2`.`_col_0` AND `_t1`.`id` = `_t2`.`id`",
        "DELETE FROM `db`.`target` WHERE `region` = 'us' AND `id` IN (1, 2)",
    ]

    # 3. Unannotated CTAS with aggregate/complex expressions at column 0 (`AVG(...)`, `ARRAY(...)`)
    #    infers non-key types (`DOUBLE`, `ARRAY<INT>`) and promotes valid key columns (`store_id`, `cnt`) to column 0.
    adapter.cursor.execute.reset_mock()
    adapter.ctas(
        "db.agg_ctas",
        parse_one(
            "SELECT AVG(price) AS avg_price, CAST(store_id AS INT) AS store_id FROM db.sales GROUP BY store_id",
            read="doris",
        ),
    )
    adapter.ctas(
        "db.array_ctas",
        parse_one("SELECT ARRAY(1, 2) AS arr, COUNT(*) AS cnt FROM db.src", read="doris"),
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`agg_ctas` AS SELECT CAST(`store_id` AS INT) AS `store_id`, AVG(`price`) AS `avg_price` FROM `db`.`sales` GROUP BY `store_id`",
        "CREATE TABLE IF NOT EXISTS `db`.`array_ctas` AS SELECT COUNT(*) AS `cnt`, ARRAY(1, 2) AS `arr` FROM `db`.`src`",
    ]

    # 4. `_create_materialized_view` with a DataFrame whose column order (`b`, `a`) differs from
    #    `target_columns_to_types` (`a`, `b`) reorders the DataFrame columns before `itertuples()`.
    adapter.cursor.execute.reset_mock()
    mv_df = pd.DataFrame([{"b": "hello", "a": 42}])
    adapter.create_view(
        "db.df_mv",
        mv_df,
        replace=False,
        materialized=True,
        target_columns_to_types={
            "a": exp.DataType.build("INT", dialect="doris"),
            "b": exp.DataType.build("VARCHAR(20)", dialect="doris"),
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE MATERIALIZED VIEW `db`.`df_mv` (`a`, `b`) AS SELECT CAST(`a` AS INT) AS `a`, CAST(`b` AS VARCHAR(20)) AS `b` FROM (SELECT 42 AS `a`, 'hello' AS `b`) AS `t`",
    ]

    # 5. `get_alter_operations` suppresses destructive `DROP` + `ADD` pairs on OLAP key columns
    #    when the target column type (`TEXT`) is an invalid OLAP key type.
    adapter.cursor.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            [("code", "VARCHAR(255)", "Yes", "true", None, "")],
            [("code", "TEXT", "Yes", "false", None, "")],
        ],
    )
    ops_destructive = adapter.get_alter_operations(
        "db.current_tbl", "db.target_tbl", ignore_destructive=False
    )
    assert ops_destructive == []

    # 6. `_to_sql` folds constant integer arithmetic inside `exp.Limit` (`LIMIT 10 + 1` -> `LIMIT 11`)
    #    so built-in `number_of_rows` audit queries execute cleanly on Doris.
    audit_query = parse_one(
        "SELECT COUNT(*) FROM (SELECT 1 FROM db.events WHERE TRUE LIMIT 10 + 1) AS _q HAVING COUNT(*) <= 10",
        read="doris",
    )
    assert (
        adapter._to_sql(audit_query)
        == "SELECT COUNT(*) FROM (SELECT 1 FROM `db`.`events` WHERE TRUE LIMIT 11) AS `_q` HAVING COUNT(*) <= 10"
    )

    # 7. Lockstep `UNION ALL` CTAS reordering when only one branch has explicit types (`AVG(score)`, `CAST(id AS INT)`)
    #    plus `NON_NULLABLE(...)` wrapping across all `UNION ALL` branches when `auto_partition = true`.
    adapter.cursor.execute.reset_mock()
    adapter.ctas(
        "db.union_lockstep_ctas",
        parse_one(
            "SELECT AVG(score) AS score, CAST(id AS INT) AS id FROM db.s1 GROUP BY id "
            "UNION ALL SELECT score, id FROM db.s2",
            read="doris",
        ),
    )
    adapter.ctas(
        "db.union_auto_part_ctas",
        parse_one(
            "SELECT id, ds FROM db.s1 UNION ALL SELECT id, ds FROM db.s2",
            read="doris",
        ),
        partitioned_by=[parse_one("RANGE(ds)", read="doris")],
        table_properties={"auto_partition": exp.Literal.string("true")},
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`union_lockstep_ctas` AS SELECT CAST(`id` AS INT) AS `id`, AVG(`score`) AS `score` FROM `db`.`s1` GROUP BY `id` UNION ALL SELECT `id`, `score` FROM `db`.`s2`",
        "CREATE TABLE IF NOT EXISTS `db`.`union_auto_part_ctas` AUTO PARTITION BY RANGE (`ds`) () AS SELECT `id`, NON_NULLABLE(`ds`) AS `ds` FROM `db`.`s1` UNION ALL SELECT `id`, NON_NULLABLE(`ds`) FROM `db`.`s2`",
    ]

    # 8. Explicit `duplicate_key` with active OLAP `partitioned_by` automatically includes the
    #    partition column in `DUPLICATE KEY` and the leading schema prefix (required by Doris FE).
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "db.dup_part_tbl",
        {
            "score": exp.DataType.build("DOUBLE", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        partitioned_by=[parse_one("RANGE(ds)", read="doris")],
        table_properties={
            "duplicate_key": exp.Tuple(expressions=[exp.Literal.string("id")]),
            "auto_partition": exp.Literal.string("true"),
            "buckets": exp.Literal.number(8),
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`dup_part_tbl` (`id` INT, `ds` DATE NOT NULL, `score` DOUBLE) DUPLICATE KEY (`id`, `ds`) AUTO PARTITION BY RANGE (`ds`) () DISTRIBUTED BY HASH (`id`) BUCKETS 8"
    ]

    # 9. `_delete_by_time_transform_keys` handles pre-extracted integer `YEAR` columns (`SELECT yr`),
    #    `GROUP BY` inside `UNION ALL` branches, and `NULL` partition values (`IS NULL`).
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            [(2024,), (None,)],
            [("2024-01-01", "2025-01-01")],
        ],
    )
    adapter.delete_from(
        "db.events",
        where=parse_one("YEAR(ds) IN (SELECT yr FROM db.staging)", read="doris"),
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "YEAR(ds) IN (SELECT YEAR(ds) FROM db.s1 GROUP BY YEAR(ds) UNION ALL SELECT YEAR(ds) FROM db.s2)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT DISTINCT `yr` AS `_val_0` FROM `db`.`staging`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2025-01-01'",
        "DELETE FROM `db`.`events` WHERE `ds` IS NULL",
        "SELECT DISTINCT DATE_TRUNC(`_col_0`, 'year') AS `_low_0`, DATE_ADD(DATE_TRUNC(`_col_0`, 'year'), INTERVAL 1 YEAR) AS `_high_0` FROM (SELECT `ds` AS `_col_0` FROM `db`.`s1` UNION ALL SELECT `ds` FROM `db`.`s2`) AS `_sq`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2025-01-01'",
    ]

    # 10. `insert_overwrite_by_partition` with empty `partitioned_by=[]` delegates cleanly to `replace_query`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "get_data_object",
        return_value=DataObject(
            catalog="internal", schema="db", name="unpart_tbl", type=DataObjectType.TABLE
        ),
    )
    adapter.insert_overwrite_by_partition(
        "db.unpart_tbl",
        parse_one("SELECT 1 AS id", read="doris"),
        partitioned_by=[],
        target_columns_to_types={"id": exp.DataType.build("INT", dialect="doris")},
    )
    assert to_sql_calls(adapter) == [
        "TRUNCATE TABLE `db`.`unpart_tbl`",
        "INSERT INTO `db`.`unpart_tbl` (`id`) SELECT 1 AS `id`",
    ]

    # 11. `DorisConnectionConfig` with `ssl=True` populates a non-empty `ssl` dict so PyMySQL enables TLS.
    ssl_conn_cfg = DorisConnectionConfig(host="fe.internal", user="root", ssl=True)
    assert ssl_conn_cfg._static_connection_kwargs["ssl"] == {"check_hostname": False}
    assert bool(ssl_conn_cfg._static_connection_kwargs["ssl"]) is True


def test_schema_differ_text_widening_largeint_and_key_protection(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Widening non-key value columns (`INT -> TEXT`, `DATE -> DATETIME(6)`) emits in-place
    #    `MODIFY COLUMN` operations instead of destructive `DROP` + `ADD`.
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            [
                ("id", "INT", "Yes", "true", None, ""),
                ("val", "INT", "Yes", "false", None, ""),
                ("dt", "DATE", "Yes", "false", None, ""),
            ],
            [
                ("id", "INT", "Yes", "true", None, ""),
                ("val", "TEXT", "Yes", "false", None, ""),
                ("dt", "DATETIME(6)", "Yes", "false", None, ""),
            ],
        ],
    )
    ops_widen = adapter.get_alter_operations(
        "db.current_tbl", "db.target_tbl", ignore_destructive=True
    )
    assert len(ops_widen) == 2
    adapter.cursor.execute.reset_mock()
    adapter.alter_table(ops_widen)
    assert to_sql_calls(adapter) == [
        "ALTER TABLE `db`.`current_tbl` MODIFY COLUMN `val` STRING",
        "ALTER TABLE `db`.`current_tbl` MODIFY COLUMN `dt` DATETIME(6)",
    ]

    # 2. `LARGEINT` vs `INT128` is treated as mutually compatible and produces zero alter operations.
    mocker.patch.object(
        adapter,
        "columns",
        side_effect=[
            {"id": exp.DataType.build("LARGEINT", udt=True)},
            {"id": exp.DataType.build("INT128")},
        ],
    )
    assert adapter.get_alter_operations("db.current_tbl", "db.target_tbl") == []

    # 3. Changing an OLAP key column to an incompatible valid key type (`DATETIME -> INT`)
    #    is blocked from emitting a destructive `DROP COLUMN` + `ADD COLUMN` on a key column,
    #    while non-key column destructive replacements (`DATETIME -> INT`) are still emitted when `ignore_destructive=False`.
    mocker.stopall()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            [
                ("k_ts", "DATETIME", "Yes", "true", None, ""),
                ("v_ts", "DATETIME", "Yes", "false", None, ""),
            ],
            [
                ("k_ts", "INT", "Yes", "true", None, ""),
                ("v_ts", "INT", "Yes", "false", None, ""),
            ],
        ],
    )
    ops_key_protected = adapter.get_alter_operations(
        "db.current_tbl", "db.target_tbl", ignore_destructive=False
    )
    adapter.cursor.execute.reset_mock()
    adapter.alter_table(ops_key_protected)
    assert to_sql_calls(adapter) == [
        "ALTER TABLE `db`.`current_tbl` DROP COLUMN `v_ts`",
        "ALTER TABLE `db`.`current_tbl` ADD COLUMN `v_ts` INT",
    ]


def test_union_ctas_string_coercion_and_duplicate_key_list_partition_coercion(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Lockstep `UNION ALL` unannotated CTAS where column 0 is non-key (`AVG(score)`) and column 1
    #    is `CAST(label AS STRING)`: both branches are reordered in lockstep AND cast `label` to `VARCHAR(255)`.
    adapter.ctas(
        "db.union_str_ctas",
        parse_one(
            "SELECT AVG(score) AS score, CAST(label AS STRING) AS label FROM db.s1 GROUP BY label "
            "UNION ALL SELECT score, label FROM db.s2",
            read="doris",
        ),
    )

    # 2. Explicit `duplicate_key = ('id')` combined with `PARTITION BY LIST(region)` where `region` is `TEXT`:
    #    `region` is automatically appended to `DUPLICATE KEY`, moved into the key prefix, AND coerced to `VARCHAR(255)`.
    adapter.create_table(
        "db.dup_list_part_tbl",
        {
            "score": exp.DataType.build("DOUBLE", dialect="doris"),
            "region": exp.DataType.build("TEXT", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        partitioned_by=[parse_one("LIST(region)", read="doris")],
        table_properties={
            "duplicate_key": exp.Tuple(expressions=[exp.Literal.string("id")]),
            "partitions": exp.Literal.string("PARTITION p_us VALUES IN ('us')"),
            "buckets": exp.Literal.number(4),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`union_str_ctas` AS SELECT CAST(`label` AS VARCHAR(255)) AS `label`, AVG(`score`) AS `score` FROM `db`.`s1` GROUP BY `label` UNION ALL SELECT CAST(`label` AS VARCHAR(255)), `score` FROM `db`.`s2`",
        "CREATE TABLE IF NOT EXISTS `db`.`dup_list_part_tbl` (`id` INT, `region` VARCHAR(255), `score` DOUBLE) DUPLICATE KEY (`id`, `region`) PARTITION BY LIST (`region`) (PARTITION p_us VALUES IN ('us')) DISTRIBUTED BY HASH (`id`) BUCKETS 4",
    ]


def test_doris_connection_config_tls_matrix():
    from sqlmesh.core.config.connection import DorisConnectionConfig

    # 1. `ssl_verify_identity=True` without explicit `ssl=True` still populates `kwargs["ssl"]` with `check_hostname=True`.
    cfg_identity = DorisConnectionConfig(host="fe.internal", user="root", ssl_verify_identity=True)
    assert cfg_identity._static_connection_kwargs["ssl"] == {"check_hostname": True}

    # 2. `ssl_verify_cert=True` populates `verify_mode=True` in `kwargs["ssl"]`.
    cfg_cert = DorisConnectionConfig(host="fe.internal", user="root", ssl_verify_cert=True)
    assert cfg_cert._static_connection_kwargs["ssl"] == {"verify_mode": True}

    # 3. `ssl_disabled=True` overrides `ssl=True` and omits the `ssl` dictionary.
    cfg_disabled = DorisConnectionConfig(
        host="fe.internal", user="root", ssl=True, ssl_disabled=True
    )
    assert "ssl" not in cfg_disabled._static_connection_kwargs


def test_doris_snapshot_evaluator_end_to_end_model_kinds(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
    make_snapshot: t.Callable,
    mocker: MockerFixture,
):
    from sqlmesh.core.model import load_sql_based_model
    from sqlmesh.core.dialect import parse as d_parse
    from sqlmesh.core.snapshot import SnapshotChangeCategory, SnapshotEvaluator

    adapter = make_mocked_engine_adapter(DorisEngineAdapter, default_catalog="internal")
    adapter.cursor.fetchall.return_value = []
    mocker.patch.object(DorisEngineAdapter, "_get_data_objects", return_value=[])
    mocker.patch.object(DorisEngineAdapter, "table_exists", return_value=True)
    mocker.patch.object(
        DorisEngineAdapter,
        "get_data_object",
        side_effect=lambda t: DataObject(
            catalog="internal",
            schema=exp.to_table(t).db or "db",
            name=exp.to_table(t).name,
            type=DataObjectType.TABLE,
        ),
    )
    evaluator = SnapshotEvaluator(adapter)

    # 1. INCREMENTAL_BY_TIME_RANGE model evaluation through SnapshotEvaluator
    inc_time_model = load_sql_based_model(
        d_parse(
            """
            MODEL (
                name db.inc_time,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds
                ),
                dialect doris,
                physical_properties (
                    duplicate_key = (id),
                    auto_partition = true,
                    buckets = 4
                ),
                partitioned_by RANGE(ds)
            );
            SELECT CAST(id AS INT) AS id, CAST(ds AS DATE) AS ds, CAST(amount AS DOUBLE) AS amount
            FROM db.raw_events
            WHERE ds BETWEEN @start_ds AND @end_ds
            """,
            default_dialect="doris",
        ),
        default_catalog="internal",
    )
    inc_time_snap = make_snapshot(inc_time_model)
    inc_time_snap.categorize_as(SnapshotChangeCategory.BREAKING)
    inc_time_snap.add_interval("2023-12-31", "2023-12-31")

    adapter.cursor.execute.reset_mock()
    evaluator.create([inc_time_snap], {})
    evaluator.evaluate(
        inc_time_snap,
        start="2024-01-01",
        end="2024-01-02",
        execution_time="2024-01-03",
        snapshots={inc_time_snap.name: inc_time_snap},
    )
    time_sql = to_sql_calls(adapter)
    assert any(
        "DUPLICATE KEY (`id`, `ds`) AUTO PARTITION BY RANGE (`ds`) () DISTRIBUTED BY HASH (`id`) BUCKETS 4"
        in s
        for s in time_sql
    )
    assert any(
        "DELETE FROM `internal`.`sqlmesh__db`.`db__inc_time__" in s
        and "WHERE `ds` >= '2024-01-01' AND `ds` <= '2024-01-02'" in s
        for s in time_sql
    )
    assert any("INSERT INTO `internal`.`sqlmesh__db`.`db__inc_time__" in s for s in time_sql)

    # 2. INCREMENTAL_BY_UNIQUE_KEY model evaluation through SnapshotEvaluator
    inc_uk_model = load_sql_based_model(
        d_parse(
            """
            MODEL (
                name db.inc_uk,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key id
                ),
                dialect doris,
                physical_properties (
                    buckets = 8
                )
            );
            SELECT CAST(id AS INT) AS id, CAST(status AS VARCHAR(32)) AS status
            FROM db.raw_users
            """,
            default_dialect="doris",
        ),
        default_catalog="internal",
    )
    inc_uk_snap = make_snapshot(inc_uk_model)
    inc_uk_snap.categorize_as(SnapshotChangeCategory.BREAKING)
    inc_uk_snap.add_interval("2023-12-31", "2023-12-31")

    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.return_value = [
        ("id", "INT", "Yes", "true", None, ""),
        ("status", "VARCHAR(32)", "Yes", "false", None, ""),
    ]
    evaluator.create([inc_uk_snap], {})
    evaluator.evaluate(
        inc_uk_snap,
        start="2024-01-01",
        end="2024-01-02",
        execution_time="2024-01-03",
        snapshots={inc_uk_snap.name: inc_uk_snap},
    )
    uk_sql = to_sql_calls(adapter)
    assert any("UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 8" in s for s in uk_sql)
    assert any("INSERT INTO `internal`.`sqlmesh__db`.`db__inc_uk__" in s for s in uk_sql)
    assert not any("MERGE INTO" in s for s in uk_sql)

    # 3. SCD_TYPE_2_BY_TIME model evaluation through SnapshotEvaluator
    scd_model = load_sql_based_model(
        d_parse(
            """
            MODEL (
                name db.scd_users,
                kind SCD_TYPE_2_BY_TIME (
                    unique_key id,
                    updated_at_name updated_at,
                    time_data_type DATETIME
                ),
                columns (
                    id INT,
                    status VARCHAR(32),
                    updated_at DATETIME,
                    valid_from DATETIME,
                    valid_to DATETIME
                ),
                dialect doris,
                physical_properties (
                    buckets = 4
                )
            );
            SELECT CAST(id AS INT) AS id, CAST(status AS VARCHAR(32)) AS status, CAST(updated_at AS DATETIME) AS updated_at
            FROM db.raw_users
            """,
            default_dialect="doris",
        ),
        default_catalog="internal",
    )
    scd_snap = make_snapshot(scd_model)
    scd_snap.categorize_as(SnapshotChangeCategory.BREAKING)
    scd_snap.add_interval("2023-12-31", "2023-12-31")

    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.return_value = [
        ("id", "INT", "Yes", "true", None, ""),
        ("status", "VARCHAR(32)", "Yes", "true", None, ""),
        ("updated_at", "DATETIME", "Yes", "false", None, ""),
        ("valid_from", "DATETIME", "Yes", "false", None, ""),
        ("valid_to", "DATETIME", "Yes", "false", None, ""),
    ]
    evaluator.create([scd_snap], {})
    evaluator.evaluate(
        scd_snap,
        start="2024-01-01",
        end="2024-01-02",
        execution_time="2024-01-03",
        snapshots={scd_snap.name: scd_snap},
    )
    scd_sql = to_sql_calls(adapter)
    assert any(
        "CREATE TABLE IF NOT EXISTS `internal`.`sqlmesh__db`.`db__scd_users__" in s
        and "DISTRIBUTED BY RANDOM BUCKETS 4" in s
        for s in scd_sql
    )
    assert any("TRUNCATE TABLE `internal`.`sqlmesh__db`.`db__scd_users__" in s for s in scd_sql)
    assert any("INSERT INTO `internal`.`sqlmesh__db`.`db__scd_users__" in s for s in scd_sql)

    # 4. INCREMENTAL_BY_PARTITION model evaluation through SnapshotEvaluator
    inc_part_model = load_sql_based_model(
        d_parse(
            """
            MODEL (
                name db.inc_part,
                kind INCREMENTAL_BY_PARTITION,
                dialect doris,
                partitioned_by LIST(region),
                physical_properties (
                    duplicate_key = (id),
                    partitions = "PARTITION p_us VALUES IN ('us')",
                    buckets = 4
                )
            );
            SELECT CAST(id AS INT) AS id, CAST(region AS VARCHAR(16)) AS region, CAST(amount AS DOUBLE) AS amount
            FROM db.raw_regional_sales
            """,
            default_dialect="doris",
        ),
        default_catalog="internal",
    )
    inc_part_snap = make_snapshot(inc_part_model)
    inc_part_snap.categorize_as(SnapshotChangeCategory.BREAKING)
    inc_part_snap.add_interval("2023-12-31", "2023-12-31")

    adapter.cursor.execute.reset_mock()
    adapter.cursor.fetchall.return_value = [
        ("id", "INT", "Yes", "true", None, ""),
        ("region", "VARCHAR(16)", "Yes", "true", None, ""),
        ("amount", "DOUBLE", "Yes", "false", None, ""),
    ]
    evaluator.create([inc_part_snap], {})
    evaluator.evaluate(
        inc_part_snap,
        start="2024-01-01",
        end="2024-01-02",
        execution_time="2024-01-03",
        snapshots={inc_part_snap.name: inc_part_snap},
    )
    part_sql = to_sql_calls(adapter)
    assert any(
        "DUPLICATE KEY (`id`, `region`) PARTITION BY LIST (`region`) (PARTITION p_us VALUES IN ('us')) DISTRIBUTED BY HASH (`id`) BUCKETS 4"
        in s
        for s in part_sql
    )
    assert any(
        "DELETE FROM `internal`.`sqlmesh__db`.`db__inc_part__" in s
        and "USING (SELECT DISTINCT `region` FROM `internal`.`sqlmesh__db`.`__temp_db__inc_part__"
        in s
        and "WHERE `_t1`.`region` = `_t2`.`region`" in s
        for s in part_sql
    )
    assert any("INSERT INTO `internal`.`sqlmesh__db`.`db__inc_part__" in s for s in part_sql)


def test_unannotated_unknown_columns_ctas_and_sync_mv_and_schema_tmp_truncation(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Unannotated SqlModel passes `target_columns_to_types` with `UNKNOWN` types to `ctas`:
    #    must reorder the `SELECT` projections so `id` (the `unique_key`) is column 0.
    adapter.ctas(
        "db.unannotated_uk_model",
        parse_one("SELECT name, id FROM db.src", read="doris"),
        target_columns_to_types={
            "name": exp.DataType.build("UNKNOWN"),
            "id": exp.DataType.build("UNKNOWN"),
        },
        table_properties={"unique_key": exp.to_column("id")},
    )

    # 2. Unannotated SqlModel with `UNKNOWN` column types and `AVG(score)` at column 0:
    #    falls through to `_reorder_unannotated_ctas_query` and moves `CAST(id AS INT)` to column 0.
    adapter.ctas(
        "db.unannotated_agg_model",
        parse_one(
            "SELECT AVG(score) AS score, CAST(id AS INT) AS id FROM db.src GROUP BY id",
            read="doris",
        ),
        target_columns_to_types={
            "score": exp.DataType.build("UNKNOWN"),
            "id": exp.DataType.build("UNKNOWN"),
        },
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`unannotated_uk_model` UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10 AS SELECT `id`, `name` FROM `db`.`src`",
        "CREATE TABLE IF NOT EXISTS `db`.`unannotated_agg_model` AS SELECT CAST(`id` AS INT) AS `id`, AVG(`score`) AS `score` FROM `db`.`src` GROUP BY `id`",
    ]

    # 3. Sync MV (`materialized_type='SYNC'`) with `target_columns_to_types` and `table_description`:
    #    must NOT wrap the query in `SELECT CAST(...) FROM (...) AS _subquery`, must NOT emit `COMMENT`,
    #    and `drop_view(view_name, materialized=True)` without `view_properties` still emits `ON <source_table>`.
    adapter.cursor.execute.reset_mock()
    adapter.create_view(
        "db.sync_mv_typed",
        parse_one("SELECT id, SUM(amount) AS total FROM db.orders GROUP BY id", read="doris"),
        replace=False,
        materialized=True,
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "total": exp.DataType.build("BIGINT", dialect="doris"),
        },
        table_description="Sync rollup MV",
        view_properties={
            "materialized_type": exp.Literal.string("SYNC"),
            "source_table": exp.Literal.string("db.orders"),
        },
    )
    adapter.drop_view("db.sync_mv_typed", materialized=True)

    assert to_sql_calls(adapter) == [
        "USE `db`",
        "CREATE MATERIALIZED VIEW `sync_mv_typed` AS SELECT `id` AS `mv_id`, SUM(`amount`) AS `total` FROM `db`.`orders` GROUP BY `id`",
        "USE `db`",
        "DROP MATERIALIZED VIEW IF EXISTS `sync_mv_typed` ON `db`.`orders`",
    ]

    # 4. Overlong `_schema_tmp` table identifier (> 64 chars) is deterministically shortened to <= 64 chars
    #    across `CREATE TABLE`, `DESCRIBE`, and `DROP TABLE`.
    adapter.cursor.execute.reset_mock()
    long_schema_tmp = f"db.{'very_long_model_name_' * 3}_schema_tmp"
    assert len(exp.to_table(long_schema_tmp).name) > 64
    adapter.cursor.fetchall.return_value = [("id", "INT", "Yes", "true", None, "")]
    adapter.ctas(long_schema_tmp, parse_one("SELECT 1 AS id", read="doris"))
    adapter.columns(long_schema_tmp)
    adapter.drop_table(long_schema_tmp)

    schema_tmp_calls = to_sql_calls(adapter)
    assert len(schema_tmp_calls) == 3
    created_tbl = parse_one(schema_tmp_calls[0], read="doris").this
    assert len(created_tbl.name) <= 64
    assert created_tbl.name.endswith("_schema_tmp")
    assert f"`{created_tbl.name}`" in schema_tmp_calls[1]
    assert f"`{created_tbl.name}`" in schema_tmp_calls[2]


def test_nested_delete_and_mixed_union_time_transform_and_merge_text_preservation(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter], mocker: MockerFixture
):
    from sqlmesh.core.config.connection import DorisConnectionConfig

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Nested parenthesized `AND`s with `IN (SELECT ...)` and extra `OR` condition:
    #    extracts the subquery and normalizes `(status = 'x' OR status = 'y')` into `status IN ('x', 'y')`.
    adapter.delete_from(
        "db.target",
        where=parse_one(
            "(tenant_id = 1 AND id IN (SELECT id FROM db.staging)) AND (status = 'x' OR status = 'y')",
            read="doris",
        ),
    )

    # 2. Single-column `id IN (SELECT * FROM db.staging_ids)` falls back to literal key batching
    #    instead of emitting invalid `SELECT * AS _col_0`.
    mocker.patch.object(adapter, "fetchall", return_value=[(10,), (20,)])
    adapter.delete_from(
        "db.target",
        where=parse_one("id IN (SELECT * FROM db.staging_ids)", read="doris"),
    )

    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`target` AS `_t1` USING (SELECT `id` FROM `db`.`staging`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`tenant_id` = 1 AND `_t1`.`status` IN ('x', 'y')",
        "DELETE FROM `db`.`target` WHERE `id` IN (10, 20)",
    ]

    # 3. `_delete_by_time_transform_keys`:
    #    (a) Mixed `UNION ALL` branches (`SELECT YEAR(ds) FROM db.s1 UNION ALL SELECT yr FROM db.s2`)
    #        use `branch_is_raw_col` instead of wrapping integer `yr` in `DATE_TRUNC(yr, 'year')`.
    #    (b) `GROUP BY` with aggregate `MAX(region)` in `WHERE`/`HAVING` is preserved instead of stripping `GROUP BY`.
    adapter.cursor.execute.reset_mock()
    mocker.stopall()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        side_effect=[
            [(2024,)],
            [("2024-01-01", "2024-01-02")],
        ],
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "YEAR(ds) IN (SELECT YEAR(ds) FROM db.s1 UNION ALL SELECT yr FROM db.s2)",
            read="doris",
        ),
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "DATE_TRUNC(ds, 'day') IN (SELECT DATE_TRUNC(ds, 'day') FROM db.s1 GROUP BY DATE_TRUNC(ds, 'day') HAVING COUNT(*) > 1)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT DISTINCT `_col_0` AS `_val_0` FROM (SELECT YEAR(`ds`) AS `_col_0` FROM `db`.`s1` UNION ALL SELECT `yr` FROM `db`.`s2`) AS `_sq`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2025-01-01'",
        "SELECT DISTINCT DATE_TRUNC(`_col_0`, 'day') AS `_low_0`, DATE_ADD(DATE_TRUNC(`_col_0`, 'day'), INTERVAL 1 DAY) AS `_high_0` FROM (SELECT DATE_TRUNC(`ds`, 'DAY') AS `_col_0` FROM `db`.`s1` GROUP BY DATE_TRUNC(`ds`, 'DAY') HAVING COUNT(*) > 1) AS `_sq`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2024-01-02'",
    ]

    # 4. `merge` with `{"notes": TEXT, "code": TEXT}` and `unique_key=[code]`:
    #    `temp_table` uses `UNIQUE KEY (code)` so `code` is coerced to `VARCHAR(255)` while non-key `notes` stays `STRING` (`TEXT`).
    adapter.cursor.execute.reset_mock()
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="abcd1234")
    adapter.merge(
        target_table="db.notes_tbl",
        source_table=parse_one("SELECT notes, code FROM db.staging_notes", read="doris"),
        target_columns_to_types={
            "notes": exp.DataType.build("TEXT", dialect="doris"),
            "code": exp.DataType.build("TEXT", dialect="doris"),
        },
        unique_key=[exp.to_column("code")],
    )
    merge_calls = to_sql_calls(adapter)
    assert (
        merge_calls[0]
        == "CREATE TABLE `db`.`__temp_notes_tbl_abcd1234` UNIQUE KEY (`code`) DISTRIBUTED BY HASH (`code`) BUCKETS 10 AS SELECT CAST(`code` AS VARCHAR(255)) AS `code`, CAST(`notes` AS STRING) AS `notes` FROM (SELECT `notes`, `code` FROM `db`.`staging_notes`) AS `_subquery`"
    )

    # 5. `DorisConnectionConfig` TLS dictionary with `ssl_verify_cert=False` sets `check_hostname=False`
    #    (preventing Python 3.13 `SSLContext` `ValueError`) and preserves `ca` when `ssl` is a dict.
    cfg_ca_no_verify = DorisConnectionConfig(
        host="fe.internal",
        user="root",
        ssl={"ca": "/path/to/ca.pem"},
        ssl_verify_cert=False,
    )
    assert cfg_ca_no_verify._static_connection_kwargs["ssl"] == {
        "ca": "/path/to/ca.pem",
        "verify_mode": False,
        "check_hostname": False,
    }


def test_doris_schema_tmp_shortening_across_fetchall_fetchdf_and_data_objects(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter, patch_get_data_objects=False)
    long_tmp_name = "temp_very_long_model_name_exceeding_sixty_four_characters_abcdefgh_schema_tmp"
    assert len(long_tmp_name) > 64
    short_tmp_name = adapter._shorten_schema_tmp_name(long_tmp_name)
    assert len(short_tmp_name) <= 64
    assert short_tmp_name.endswith("_schema_tmp")

    # 1. `fetchall` / `_to_sql` rewrites Table, TableAlias, and Column.table qualifiers for >64-char `_schema_tmp`
    mocker.patch.object(adapter.cursor, "fetchall", return_value=[(1,)])
    query_with_qualifiers = parse_one(
        f"SELECT `{long_tmp_name}`.`id` FROM `db`.`{long_tmp_name}` AS `{long_tmp_name}`",
        read="doris",
    )
    assert adapter.fetchall(query_with_qualifiers) == [(1,)]
    assert to_sql_calls(adapter) == [
        f"SELECT `{short_tmp_name}`.`id` FROM `db`.`{short_tmp_name}` AS `{short_tmp_name}`"
    ]

    # 2. `fetchdf` (`_fetch_native_df`) normalizes >64-char `_schema_tmp` identifiers before `pd.read_sql_query`
    mock_read_sql = mocker.patch(
        "pandas.io.sql.read_sql_query",
        return_value=pd.DataFrame({"id": [1]}),
    )
    df = adapter.fetchdf(query_with_qualifiers)
    assert list(df["id"]) == [1]
    assert (
        mock_read_sql.call_args[0][0]
        == f"SELECT `{short_tmp_name}`.`id` FROM `db`.`{short_tmp_name}` AS `{short_tmp_name}`"
    )

    # 3. `_get_data_objects` queries `information_schema.tables` using the shortened `_schema_tmp` name
    #    and restores the caller's original unshortened name on the returned `DataObject`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "fetchall",
        side_effect=[
            [("db", short_tmp_name, "BASE TABLE")],
            [],
        ],
    )
    objs = adapter._get_data_objects("db", object_names={long_tmp_name})
    assert len(objs) == 1
    assert objs[0].name == long_tmp_name
    query_arg = adapter.fetchall.call_args_list[0][0][0]
    rendered_info_schema_sql = adapter._to_sql(query_arg)
    assert short_tmp_name.lower() in rendered_info_schema_sql.lower()
    assert long_tmp_name.lower() not in rendered_info_schema_sql.lower()


def test_doris_delete_by_time_transform_validation_and_deduplication(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Non-YEAR date-part extract `MONTH(ds) IN (SELECT MONTH(src_ds) ...)` raises `SQLMeshError`
    #    instead of silently deleting an entire calendar month.
    with pytest.raises(
        SQLMeshError,
        match="cannot rewrite non-year date-part predicate",
    ):
        adapter.delete_from(
            "db.events",
            where=parse_one(
                "MONTH(ds) IN (SELECT MONTH(src_ds) FROM db.staging)",
                read="doris",
            ),
        )

    # 2. Mismatched subquery time-transform unit (`DATE_TRUNC(ds, 'day') IN (SELECT YEAR(src_ds) ...)`)
    #    raises `SQLMeshError`.
    with pytest.raises(
        SQLMeshError,
        match="with mismatched subquery projection",
    ):
        adapter.delete_from(
            "db.events",
            where=parse_one(
                "DATE_TRUNC(ds, 'day') IN (SELECT YEAR(src_ds) FROM db.staging)",
                read="doris",
            ),
        )

    # 3. `YEAR(ds) IN (SELECT ds FROM db.staging)` where `staging.ds` returns ISO date strings
    #    in the same year parses the year prefix and deduplicates identical `DELETE` statements.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[("2024-05-10",), ("2024-08-20",)],
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "YEAR(ds) IN (SELECT ds FROM db.staging)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT DISTINCT `ds` AS `_val_0` FROM `db`.`staging`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2025-01-01'",
    ]


def test_doris_replace_by_key_merge_and_partition_overwrite_edge_cases(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="abcd1234")

    # 1. `insert_overwrite_by_partition` / `_replace_by_key` with `{"notes": TEXT, "region": VARCHAR(16)}`
    #    and `partitioned_by=[region]` moves `region` to column 0 in the temp table so `notes` stays `STRING`
    #    (never truncated to `VARCHAR(255)`).
    adapter.insert_overwrite_by_partition(
        table_name="db.notes_by_region",
        query_or_df=parse_one("SELECT notes, region FROM db.src_notes", read="doris"),
        partitioned_by=[exp.to_column("region")],
        target_columns_to_types={
            "notes": exp.DataType.build("TEXT", dialect="doris"),
            "region": exp.DataType.build("VARCHAR(16)", dialect="doris"),
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE `db`.`__temp_notes_by_region_abcd1234` AS SELECT CAST(`region` AS VARCHAR(16)) AS `region`, CAST(`notes` AS STRING) AS `notes` FROM (SELECT `notes`, `region` FROM `db`.`src_notes`) AS `_subquery`",
        "DELETE FROM `db`.`notes_by_region` AS `_t1` USING (SELECT DISTINCT `region` FROM `db`.`__temp_notes_by_region_abcd1234`) AS `_t2` WHERE `_t1`.`region` = `_t2`.`region`",
        "INSERT INTO `db`.`notes_by_region` (`region`, `notes`) SELECT `region`, `notes` FROM `db`.`__temp_notes_by_region_abcd1234`",
        "DROP TABLE IF EXISTS `db`.`__temp_notes_by_region_abcd1234`",
    ]

    # 2. `merge` when `target_columns_to_types` contains `UNKNOWN` falls back to `self.columns(target_table)`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "columns",
        return_value={
            "id": exp.DataType.build("INT", dialect="doris"),
            "val": exp.DataType.build("VARCHAR(64)", dialect="doris"),
        },
    )
    adapter.merge(
        target_table="db.unknown_cols_target",
        source_table=parse_one("SELECT id, val FROM db.staging", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("UNKNOWN"),
            "val": exp.DataType.build("UNKNOWN"),
        },
        unique_key=[exp.to_column("id")],
    )
    assert (
        to_sql_calls(adapter)[0]
        == "CREATE TABLE `db`.`__temp_unknown_cols_target_abcd1234` UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10 AS SELECT CAST(`id` AS INT) AS `id`, CAST(`val` AS VARCHAR(64)) AS `val` FROM (SELECT `id`, `val` FROM `db`.`staging`) AS `_subquery`"
    )

    # 3. `merge` with 2-part table name and `table_format="iceberg"` routes to Iceberg `_replace_by_key`
    #    (no OLAP `UNIQUE KEY` or `DISTRIBUTED BY` on the temp table, and literal-key DELETE instead of DELETE ... USING).
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[(10,)],
    )
    adapter.merge(
        target_table="iceberg_db.events",
        source_table=parse_one("SELECT id, payload FROM iceberg_db.staging", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "payload": exp.DataType.build("STRING", dialect="doris"),
        },
        unique_key=[exp.to_column("id")],
        table_format="iceberg",
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE `iceberg_db`.`__temp_events_abcd1234` (`id` INT, `payload` STRING) ENGINE=iceberg",
        "INSERT INTO `iceberg_db`.`__temp_events_abcd1234` (`id`, `payload`) SELECT CAST(`id` AS INT) AS `id`, CAST(`payload` AS STRING) AS `payload` FROM (SELECT `id`, `payload` FROM `iceberg_db`.`staging`) AS `_subquery`",
        "SELECT DISTINCT `id` FROM `iceberg_db`.`__temp_events_abcd1234`",
        "DELETE FROM `iceberg_db`.`events` WHERE `id` IN (10)",
        "INSERT INTO `iceberg_db`.`events` (`id`, `payload`) SELECT `id`, `payload` FROM (SELECT `id` AS `id`, `payload` AS `payload`, ROW_NUMBER() OVER (PARTITION BY `id` ORDER BY `id`) AS _row_number FROM `iceberg_db`.`__temp_events_abcd1234`) AS _t WHERE _row_number = 1",
        "DROP TABLE IF EXISTS `iceberg_db`.`__temp_events_abcd1234`",
    ]

    # 4. `insert_overwrite_by_time_partition` with DataFrame and `{"val": DOUBLE, "ds": DATE, "id": INT}`
    #    reorders `target_columns_to_types` and `source_columns` so the temp table created by `_values_to_sql`
    #    places a valid OLAP key column (`ds` / `id`) before `val DOUBLE`.
    adapter.cursor.execute.reset_mock()
    df = pd.DataFrame(
        {
            "val": [3.14],
            "ds": ["2024-01-01"],
            "id": [42],
        }
    )
    adapter.insert_overwrite_by_time_partition(
        table_name="db.metrics",
        query_or_df=df,
        start="2024-01-01",
        end="2024-01-01",
        time_formatter=lambda x, _: exp.Literal.string(str(x)),
        time_column="ds",
        target_columns_to_types={
            "val": exp.DataType.build("DOUBLE", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
    )
    time_part_calls = to_sql_calls(adapter)
    assert time_part_calls == [
        "DELETE FROM `db`.`metrics` WHERE `ds` >= '2024-01-01 00:00:00+00:00' AND `ds` <= '2024-01-01 23:59:59.999999+00:00'",
        "INSERT INTO `db`.`metrics` (`ds`, `val`, `id`) SELECT `ds`, `val`, `id` FROM (SELECT CAST(`ds` AS DATE) AS `ds`, CAST(`val` AS DOUBLE) AS `val`, CAST(`id` AS INT) AS `id` FROM (SELECT '2024-01-01' AS `ds`, 3.14 AS `val`, 42 AS `id`) AS `t`) AS `_subquery` WHERE `ds` BETWEEN '2024-01-01 00:00:00+00:00' AND '2024-01-01 23:59:59.999999+00:00'",
    ]


def test_doris_unannotated_ctas_union_all_and_cte_column_reordering(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. `UNION ALL` where Branch 1 has `1 AS metric` (INT) and Branch 2 has `1.5 AS metric` (DOUBLE):
    #    Doris widens `metric` to DOUBLE across the UNION, so `id` (INT across all branches) is promoted to col 0.
    adapter.ctas(
        table_name="db.union_metrics",
        query_or_df=parse_one(
            "SELECT 1 AS metric, 10 AS id UNION ALL SELECT 1.5 AS metric, 20 AS id",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`union_metrics` AS SELECT 10 AS `id`, 1 AS `metric` UNION ALL SELECT 20 AS `id`, 1.5 AS `metric`"
    ]

    # 2. CTE (`WITH agg AS (...) SELECT avg_score, id FROM agg`) propagates CTE column types to the outer SELECT
    #    and reorders `id` before `avg_score` (`AVG` -> `DOUBLE`).
    adapter.cursor.execute.reset_mock()
    adapter.ctas(
        table_name="db.cte_explicit_cols",
        query_or_df=parse_one(
            "WITH agg AS (SELECT AVG(score) AS avg_score, CAST(id AS INT) AS id FROM db.src GROUP BY id) "
            "SELECT avg_score, id FROM agg",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`cte_explicit_cols` AS WITH `agg` AS (SELECT AVG(`score`) AS `avg_score`, CAST(`id` AS INT) AS `id` FROM `db`.`src` GROUP BY `id`) SELECT `id`, `avg_score` FROM `agg`"
    ]

    # 3. CTE with `SELECT * FROM agg` reorders the inner CTE projection when the CTE starts with `AVG(score)`.
    adapter.cursor.execute.reset_mock()
    adapter.ctas(
        table_name="db.cte_star",
        query_or_df=parse_one(
            "WITH agg AS (SELECT AVG(score) AS avg_score, CAST(id AS INT) AS id FROM db.src GROUP BY id) "
            "SELECT * FROM agg",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`cte_star` AS WITH `agg` AS (SELECT CAST(`id` AS INT) AS `id`, AVG(`score`) AS `avg_score` FROM `db`.`src` GROUP BY `id`) SELECT * FROM `agg`"
    ]

    # 4. `_reorder_olap_columns` prefers coercible `STRING` over `UNKNOWN` when column 0 is `DOUBLE`.
    reordered_cols = adapter._reorder_olap_columns(
        {
            "metric": exp.DataType.build("DOUBLE", dialect="doris"),
            "mystery": exp.DataType.build("UNKNOWN"),
            "label": exp.DataType.build("STRING", dialect="doris"),
        }
    )
    assert list(reordered_cols.keys()) == ["label", "metric", "mystery"]
    assert reordered_cols["label"] == exp.DataType.build("VARCHAR(255)", dialect="doris")


def test_doris_sync_mv_source_tables_persistence_and_isolation(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Creating a sync MV in `db1` and `db2` with the same view name does not collide,
    #    survives `adapter.with_settings()`, and `drop_view` with `materialized_type='SYNC'`
    #    (without `source_table` in `view_properties`) still finds the recorded base table.
    adapter.create_view(
        "db1.shared_mv",
        parse_one("SELECT id, SUM(val) AS total FROM db1.orders GROUP BY id", read="doris"),
        replace=False,
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
    )
    adapter.create_view(
        "db2.shared_mv",
        parse_one("SELECT id, SUM(val) AS total FROM db2.events GROUP BY id", read="doris"),
        replace=False,
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
    )

    cloned_adapter = adapter.with_settings()
    adapter.cursor.execute.reset_mock()
    cloned_adapter.drop_view(
        "db1.shared_mv",
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
    )
    cloned_adapter.drop_view(
        "db2.shared_mv",
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
    )
    assert to_sql_calls(adapter) == [
        "USE `db1`",
        "DROP MATERIALIZED VIEW IF EXISTS `shared_mv` ON `db1`.`orders`",
        "USE `db2`",
        "DROP MATERIALIZED VIEW IF EXISTS `shared_mv` ON `db2`.`events`",
    ]

    # 2. Recreating a view name as an async MTMV pops any stale sync MV entry so subsequent
    #    `drop_view(materialized=True)` emits `DROP MATERIALIZED VIEW IF EXISTS db1.shared_mv`.
    adapter.create_view(
        "db1.shared_mv",
        parse_one("SELECT id, SUM(val) AS total FROM db1.orders GROUP BY id", read="doris"),
        replace=False,
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
    )
    adapter.create_view(
        "db1.shared_mv",
        parse_one("SELECT id, val FROM db1.orders", read="doris"),
        replace=False,
        materialized=True,
    )
    adapter.cursor.execute.reset_mock()
    adapter.drop_view("db1.shared_mv", materialized=True)
    assert to_sql_calls(adapter) == [
        "DROP MATERIALIZED VIEW IF EXISTS `db1`.`shared_mv`",
    ]


def test_doris_reconnect_catalog_db_restore_union_star_delete_and_extra_or_conditions(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    class OperationalError(Exception):
        pass

    adapter = make_mocked_engine_adapter(
        DorisEngineAdapter,
        default_catalog=None,
        database="analytics",
    )

    # 1. `set_current_catalog("internal")` restores `USE \`analytics\`` when `database` is configured.
    adapter.set_current_catalog("iceberg_cat")
    adapter.set_current_catalog("internal")
    assert to_sql_calls(adapter) == [
        "SWITCH `iceberg_cat`",
        "SWITCH `internal`",
        "USE `analytics`",
    ]

    # 2. When a query fails with an operational error and the reconnect's `SWITCH` also fails,
    #    `_needs_catalog_restore` stays `True` until restoration succeeds on a subsequent call.
    adapter.set_current_catalog("iceberg_cat")
    adapter.cursor.execute.reset_mock()
    adapter.cursor.execute.side_effect = [
        OperationalError(2013, "Lost connection to MySQL server during query"),
        OperationalError(2006, "MySQL server has gone away"),
        None,
        None,
    ]
    with pytest.raises(OperationalError, match="MySQL server has gone away"):
        adapter.execute("SELECT 1")
    assert adapter._needs_catalog_restore is True

    adapter.execute("SELECT 1")
    assert adapter._needs_catalog_restore is False
    assert to_sql_calls(adapter) == [
        "SELECT 1",
        "SWITCH `iceberg_cat`",
        "SWITCH `iceberg_cat`",
        "SELECT 1",
    ]

    # 3. `delete_from` with `UNION ALL` where the second branch uses `SELECT *` expands/projects key columns
    #    and slices extra trailing columns in `_delete_by_literal_keys`.
    adapter.cursor.execute.reset_mock()
    adapter.cursor.execute.side_effect = None
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[(101, "ignored_extra_col"), (102, "ignored_extra_col")],
    )
    adapter.delete_from(
        "db.target",
        where=parse_one(
            "id IN (SELECT id FROM db.s1 UNION ALL SELECT * FROM db.s2)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT `id` FROM `db`.`s1` UNION ALL SELECT * FROM `db`.`s2`",
        "DELETE FROM `db`.`target` WHERE `id` IN (101, 102)",
    ]

    # 4. Extra `OR` condition alongside `IN (SELECT ...)` (`id IN (SELECT id FROM db.s1) AND (status = 'a' OR status IS NULL)`)
    #    on an Iceberg or literal-key delete path is distributed into separate `OR`-free `DELETE` statements so Doris does not
    #    reject the `DELETE` with `WhereClause only supports compound predicate, binary predicate, is_null predicate or in predicate`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[(1,), (2,)],
    )
    adapter.delete_from(
        "iceberg_cat.db.target",
        where=parse_one(
            "id IN (SELECT id FROM db.s1) AND (status = 'a' OR status IS NULL)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT DISTINCT `id` FROM `db`.`s1`",
        "DELETE FROM `iceberg_cat`.`db`.`target` WHERE `id` IN (1, 2) AND `status` = 'a'",
        "DELETE FROM `iceberg_cat`.`db`.`target` WHERE `id` IN (1, 2) AND `status` IS NULL",
    ]


def test_doris_dialect_native_types_and_complex_constructors_and_row_diff(
    make_mocked_engine_adapter: t.Callable,
):
    from sqlmesh.core.dialect import parse as d_parse
    from sqlmesh.core.model import load_sql_based_model

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Doris-native data types parse cleanly in `MODEL (columns (...))` and round-trip in Doris SQL:
    #    `LARGEINT`, `IPV4`, `IPV6`, `JSONB`, `VARIANT`, `BITMAP`, `HLL`, `QUANTILE_STATE`, `AGG_STATE`,
    #    `DATEV2`, `DATETIMEV2(3)`, `DECIMALV3(18, 4)`, `ARRAY`, `MAP`, and `STRUCT`.
    model = load_sql_based_model(
        d_parse(
            """
            MODEL (
                name db.doris_rich_types,
                kind FULL,
                dialect doris,
                columns (
                    id LARGEINT,
                    ip4 IPV4,
                    ip6 IPV6,
                    jb JSONB,
                    var_col VARIANT,
                    bm BITMAP,
                    hll_col HLL,
                    qs QUANTILE_STATE,
                    agg_s AGG_STATE<max_by(INT, INT)>,
                    dt2 DATEV2,
                    ts2 DATETIMEV2(3),
                    dec3 DECIMALV3(18, 4),
                    tags ARRAY<VARCHAR(32)>,
                    attrs MAP<STRING, INT>,
                    profile STRUCT<uid: BIGINT, name: VARCHAR(64)>
                )
            );
            SELECT
                CAST(1 AS LARGEINT) AS id,
                CAST('127.0.0.1' AS IPV4) AS ip4,
                CAST('::1' AS IPV6) AS ip6,
                CAST('{}' AS JSONB) AS jb,
                CAST('{}' AS VARIANT) AS var_col,
                BITMAP_EMPTY() AS bm,
                HLL_EMPTY() AS hll_col,
                TO_QUANTILE_STATE(1, 2048) AS qs,
                 max_by_state(1, 2) AS agg_s,
                CAST('2024-01-01' AS DATEV2) AS dt2,
                CAST('2024-01-01 00:00:00' AS DATETIMEV2(3)) AS ts2,
                CAST(12.34 AS DECIMALV3(18, 4)) AS dec3,
                ARRAY('a', 'b') AS tags,
                MAP('k1', 1, 'k2', 2) AS attrs,
                STRUCT(100, 'alice') AS profile
            """,
            default_dialect="doris",
        )
    )
    cols = model.columns_to_types
    assert cols is not None
    adapter.create_table("db.doris_rich_types", cols)
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`doris_rich_types` ("
        "`id` LARGEINT, `ip4` IPV4, `ip6` IPV6, `jb` JSONB, `var_col` VARIANT, "
        "`bm` BITMAP, `hll_col` HLL, `qs` QUANTILE_STATE, `agg_s` AGG_STATE<max_by(INT, INT)>, "
        "`dt2` DATE, `ts2` DATETIME(3), `dec3` DECIMAL(18, 4), `tags` ARRAY<VARCHAR(32)>, "
        "`attrs` MAP<STRING, INT>, `profile` STRUCT<`uid`: BIGINT, `name`: VARCHAR(64)>)"
    ]

    # 2. Complex literal constructors (`ARRAY`, `MAP`, `STRUCT`, `NAMED_STRUCT`), `LEFT SEMI JOIN`,
    #    `LEFT ANTI JOIN` on unaliased tables, and `DISTINCT ON` transpile cleanly in Doris dialect.
    query_sql = parse_one(
        "SELECT [1, 2], MAP('a', 1, 'b', 2), STRUCT(1, 'x'), NAMED_STRUCT('id', 1, 'name', 'x') "
        "FROM db.t1 LEFT ANTI JOIN db.t2 ON db.t1.id = db.t2.id "
        "LEFT SEMI JOIN db.t3 ON db.t1.id = db.t3.id",
        read="doris",
    ).sql("doris")
    assert (
        query_sql
        == "SELECT ARRAY(1, 2), MAP('a', 1, 'b', 2), STRUCT(1, 'x'), NAMED_STRUCT('id', 1, 'name', 'x') "
        "FROM db.t1 LEFT ANTI JOIN db.t2 ON db.t1.id = db.t2.id "
        "LEFT SEMI JOIN db.t3 ON db.t1.id = db.t3.id"
    )
    distinct_on_sql = parse_one(
        "SELECT DISTINCT ON (id) id, ts FROM db.events ORDER BY id, ts DESC",
        read="doris",
    ).sql("doris")
    assert "ROW_NUMBER() OVER (PARTITION BY id ORDER BY id, ts DESC)" in distinct_on_sql

    # 3. `RowDiffMixin.concat_columns` (`_normalize_value`) casts complex & special Doris types
    #    to `VARCHAR` instead of `DataType.Type.STRUCT` / `ARRAY` / `USERDEFINED`.
    concat_expr = adapter.concat_columns(
        {
            "id": cols["id"],
            "tags": cols["tags"],
            "attrs": cols["attrs"],
            "profile": cols["profile"],
            "bm": cols["bm"],
            "var_col": cols["var_col"],
        }
    )
    rendered_concat = concat_expr.sql("doris")
    assert "AS STRUCT" not in rendered_concat
    assert "AS ARRAY" not in rendered_concat
    assert "AS MAP" not in rendered_concat
    assert "AS BITMAP" not in rendered_concat
    assert "CAST(tags AS VARCHAR)" in rendered_concat
    assert "CAST(`profile` AS VARCHAR)" in rendered_concat
    assert (
        adapter._normalize_nested_value(exp.column("tags", "s")).sql("doris")
        == "CAST(s.tags AS VARCHAR)"
    )


def test_doris_ctas_window_partially_annotated_positional_group_order_and_subquery_union(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Unannotated CTAS with `ROW_NUMBER() OVER (...)` at col 1 and `AVG(score)` at col 0
    #    recognizes `exp.Window` (`ROW_NUMBER` -> `BIGINT`) and promotes `rn` to col 0.
    adapter.ctas(
        "db.win_ctas",
        parse_one(
            "SELECT AVG(score) AS avg_score, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM db.src GROUP BY id",
            read="doris",
        ),
    )

    # 2. Partially annotated CTAS (`score: DOUBLE`, `id: INT`, `extra: UNKNOWN`) uses known column
    #    types to promote `id` before `score` (`DOUBLE`) and remaps positional `GROUP BY 2 ORDER BY 2 DESC` -> `1`.
    adapter.ctas(
        "db.partial_annotated_ctas",
        parse_one(
            "SELECT AVG(score) AS score, id, extra FROM db.src GROUP BY 2, 3 ORDER BY 2 DESC",
            read="doris",
        ),
        target_columns_to_types={
            "score": exp.DataType.build("DOUBLE", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
            "extra": exp.DataType.build("UNKNOWN"),
        },
    )

    # 3. Parenthesized `UNION ALL` branches (`(SELECT ...) UNION ALL (SELECT ...)`) with `auto_partition = true`
    #    unwrap `exp.Subquery` nodes, reorder projections in lockstep, and wrap range partition column in `NON_NULLABLE`.
    adapter.ctas(
        "db.paren_union_auto_ctas",
        parse_one(
            "(SELECT AVG(score) AS score, CAST(id AS INT) AS id, CAST(ds AS DATE) AS ds FROM db.s1 GROUP BY 2, 3) "
            "UNION ALL "
            "(SELECT CAST(score AS DOUBLE) AS score, CAST(id AS INT) AS id, CAST(ds AS DATE) AS ds FROM db.s2)",
            read="doris",
        ),
        partitioned_by=[parse_one("RANGE(ds)", read="doris")],
        table_properties={"auto_partition": exp.Literal.string("true")},
    )

    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`win_ctas` AS SELECT ROW_NUMBER() OVER (ORDER BY `id`) AS `rn`, AVG(`score`) AS `avg_score` FROM `db`.`src` GROUP BY `id`",
        "CREATE TABLE IF NOT EXISTS `db`.`partial_annotated_ctas` AS SELECT `id`, AVG(`score`) AS `score`, `extra` FROM `db`.`src` GROUP BY 1, 3 ORDER BY 1 DESC",
        "CREATE TABLE IF NOT EXISTS `db`.`paren_union_auto_ctas` AUTO PARTITION BY RANGE (`ds`) () AS (SELECT CAST(`id` AS INT) AS `id`, NON_NULLABLE(CAST(`ds` AS DATE)) AS `ds`, AVG(`score`) AS `score` FROM `db`.`s1` GROUP BY 1, 2) UNION ALL (SELECT CAST(`id` AS INT) AS `id`, NON_NULLABLE(CAST(`ds` AS DATE)) AS `ds`, CAST(`score` AS DOUBLE) AS `score` FROM `db`.`s2`)",
    ]


def test_doris_replace_by_key_text_preservation_intersect_delete_and_direct_time_delete(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="abcd1234")

    # 1. `merge` / `_replace_by_key` when `unique_key` is a `TEXT`/`STRING` column (`doc_id`) and
    #    another valid OLAP key column (`tenant_id INT`) exists: does NOT coerce `doc_id` to `VARCHAR(255)`
    #    on the temp table (preventing 255-byte truncation of long keys), while promoting `tenant_id` to col 0.
    adapter.merge(
        target_table="db.docs_tbl",
        source_table=parse_one("SELECT doc_id, tenant_id FROM db.staging_docs", read="doris"),
        target_columns_to_types={
            "doc_id": exp.DataType.build("TEXT", dialect="doris"),
            "tenant_id": exp.DataType.build("INT", dialect="doris"),
        },
        unique_key=[exp.to_column("doc_id")],
    )
    merge_sqls = to_sql_calls(adapter)
    assert (
        merge_sqls[0]
        == "CREATE TABLE `db`.`__temp_docs_tbl_abcd1234` AS SELECT CAST(`tenant_id` AS INT) AS `tenant_id`, CAST(`doc_id` AS STRING) AS `doc_id` FROM (SELECT `doc_id`, `tenant_id` FROM `db`.`staging_docs`) AS `_subquery`"
    )

    # 2. `_delete_by_time_transform_keys` with `INTERSECT` or `EXCEPT` does NOT unwrap `DATE_TRUNC(ds, 'day')`
    #    into raw `ds` inside the branches (preserving day-level set intersection/difference semantics),
    #    and unwraps parenthesized `(SELECT ...)` branches in `DELETE ... USING`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter.cursor,
        "fetchall",
        return_value=[("2024-01-15", "2024-01-16")],
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "DATE_TRUNC(ds, 'day') IN ((SELECT DATE_TRUNC(ds, 'day') FROM db.s1) INTERSECT (SELECT DATE_TRUNC(ds, 'day') FROM db.s2))",
            read="doris",
        ),
    )
    adapter.delete_from(
        "db.events",
        where=parse_one(
            "id IN ((SELECT id FROM db.s1) UNION ALL (SELECT id FROM db.s2))",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "SELECT DISTINCT DATE_TRUNC(`_col_0`, 'day') AS `_low_0`, DATE_ADD(DATE_TRUNC(`_col_0`, 'day'), INTERVAL 1 DAY) AS `_high_0` FROM ((SELECT DATE_TRUNC(`ds`, 'DAY') AS `_col_0` FROM `db`.`s1`) INTERSECT (SELECT DATE_TRUNC(`ds`, 'DAY') FROM `db`.`s2`)) AS `_sq`",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-15' AND `ds` < '2024-01-16'",
        "DELETE FROM `db`.`events` AS `_t1` USING ((SELECT `id` FROM `db`.`s1`) UNION ALL (SELECT `id` FROM `db`.`s2`)) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id`",
    ]

    # 3. Direct time-transform predicates (`DATE_TRUNC(ds, 'day') = '2024-01-15'`, `YEAR(ds) = 2024`,
    #    `DATE_TRUNC(ds, 'day') IN ('2024-01-15', '2024-01-16')`) and heterogeneous `OR` predicates
    #    (`a = 1 OR b = 2`) in `delete_from` are rewritten into `OR`-free bare-column predicates.
    adapter.cursor.execute.reset_mock()
    adapter.delete_from("db.events", "DATE_TRUNC(ds, 'day') = '2024-01-15'")
    adapter.delete_from("db.events", "YEAR(ds) = 2024")
    adapter.delete_from("db.events", "DATE_TRUNC(ds, 'day') IN ('2024-01-15', '2024-01-16')")
    adapter.delete_from("db.events", "a = 1 OR b = 2")
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-15' AND `ds` < '2024-01-16'",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-01' AND `ds` < '2025-01-01'",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-15' AND `ds` < '2024-01-16'",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-16' AND `ds` < '2024-01-17'",
        "DELETE FROM `db`.`events` WHERE `a` = 1",
        "DELETE FROM `db`.`events` WHERE `b` = 2",
    ]


def test_doris_physical_properties_kwarg_drop_schema_cascade_and_mv_df_source_columns(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. `physical_properties={'engine': exp.Literal.string('iceberg')}` (as passed by SnapshotEvaluator)
    #    is honored across `replace_query` / `_insert_overwrite_by_condition`.
    mocker.patch.object(
        adapter,
        "get_data_object",
        return_value=DataObject(
            catalog="internal", schema="db", name="iceberg_by_prop", type=DataObjectType.TABLE
        ),
    )
    adapter.replace_query(
        "db.iceberg_by_prop",
        parse_one("SELECT 1 AS id", read="doris"),
        target_columns_to_types={"id": exp.DataType.build("INT", dialect="doris")},
        physical_properties={"engine": exp.Literal.string("iceberg")},
    )
    assert to_sql_calls(adapter) == [
        "INSERT OVERWRITE TABLE `db`.`iceberg_by_prop` (`id`) SELECT 1 AS `id`"
    ]

    # 2. `drop_schema(..., cascade=True)` on an external Iceberg catalog drops member tables first
    #    before issuing `DROP DATABASE`.
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "_get_data_objects",
        return_value=[
            DataObject(
                catalog="iceberg_cat", schema="silver", name="t1", type=DataObjectType.TABLE
            ),
            DataObject(
                catalog="iceberg_cat", schema="silver", name="t2", type=DataObjectType.TABLE
            ),
        ],
    )
    adapter.drop_schema("iceberg_cat.silver", ignore_if_not_exists=True, cascade=True)
    assert to_sql_calls(adapter) == [
        "DROP TABLE IF EXISTS `iceberg_cat`.`silver`.`t1`",
        "DROP TABLE IF EXISTS `iceberg_cat`.`silver`.`t2`",
        "DROP DATABASE IF EXISTS `iceberg_cat`.`silver`",
    ]

    # 3. `_create_materialized_view` with a DataFrame containing extra `source_columns` not in
    #    `target_columns_to_types` slices only `source_columns_to_types` so `_values_to_sql` never raises `ValueError`.
    adapter.cursor.execute.reset_mock()
    df_extra = pd.DataFrame([{"id": 1, "name": "alice", "extra_ignored": 999}])
    adapter.create_view(
        "db.mv_from_df_subset",
        df_extra,
        replace=False,
        materialized=True,
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "name": exp.DataType.build("VARCHAR(32)", dialect="doris"),
        },
        source_columns=["id", "name", "extra_ignored"],
    )
    assert to_sql_calls(adapter) == [
        "CREATE MATERIALIZED VIEW `db`.`mv_from_df_subset` (`id`, `name`) AS SELECT CAST(`id` AS INT) AS `id`, CAST(`name` AS VARCHAR(32)) AS `name` FROM (SELECT 1 AS `id`, 'alice' AS `name`) AS `t`"
    ]

    # 4. `_truncate_table` normalizes >64-char `_schema_tmp` table names before `TRUNCATE TABLE` and fallback `DELETE`.
    adapter.cursor.execute.reset_mock()
    long_tmp = "db." + ("very_long_table_name_" * 3) + "_schema_tmp"
    short_tmp = adapter._shorten_schema_tmp_name(exp.to_table(long_tmp).name)
    adapter._truncate_table(long_tmp)
    assert to_sql_calls(adapter) == [f"TRUNCATE TABLE `db`.`{short_tmp}`"]

    # 5. `_fetch_native_df` with a raw string query containing `IS TRUE` or >64-char `_schema_tmp`
    #    normalizes the SQL string before `pd.read_sql_query`.
    mock_read_sql = mocker.patch(
        "pandas.io.sql.read_sql_query",
        return_value=pd.DataFrame({"id": [1]}),
    )
    adapter.fetchdf(f"SELECT id FROM {long_tmp} WHERE is_active IS TRUE", quote_identifiers=True)
    assert (
        mock_read_sql.call_args[0][0]
        == f"SELECT `id` FROM `db`.`{short_tmp}` WHERE `is_active` = TRUE"
    )


def test_doris_time_transform_inequalities_unaliased_ctas_and_sync_mv_db_restore(
    make_mocked_engine_adapter: t.Callable,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter, database="analytics")

    # 1. Direct time-transform inequalities (`>=`, `<=`, `!=`) in `delete_from` are rewritten into
    #    bare-column predicates (and `!=` is split into two OR-free DELETE statements).
    adapter.delete_from(
        "db.events",
        "DATE_TRUNC(ds, 'day') >= '2024-01-15' AND DATE_TRUNC(ds, 'day') <= '2024-01-20'",
    )
    adapter.delete_from("db.events", "DATE_TRUNC(ds, 'day') != '2024-01-15'")
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-15' AND `ds` < '2024-01-21'",
        "DELETE FROM `db`.`events` WHERE `ds` < '2024-01-15'",
        "DELETE FROM `db`.`events` WHERE `ds` >= '2024-01-16'",
    ]

    # 2. `DELETE ... USING` with an extra `OR` condition (`id IN (SELECT id FROM db.src) AND (status = 'a' OR status IS NULL)`)
    #    expands `extra_conditions` into separate OR-free `DELETE ... USING` statements.
    adapter.cursor.execute.reset_mock()
    adapter.delete_from(
        "db.target",
        where=parse_one(
            "id IN (SELECT id FROM db.src) AND (status = 'a' OR status IS NULL)",
            read="doris",
        ),
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`target` AS `_t1` USING (SELECT `id` FROM `db`.`src`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`status` = 'a'",
        "DELETE FROM `db`.`target` AS `_t1` USING (SELECT `id` FROM `db`.`src`) AS `_t2` WHERE `_t1`.`id` = `_t2`.`id` AND `_t1`.`status` IS NULL",
    ]

    # 3. Unannotated CTAS with unaliased `AVG(score)` or division `a / b` at column 0 promotes `id` to column 0.
    adapter.cursor.execute.reset_mock()
    adapter.ctas(
        "db.unaliased_ctas",
        parse_one("SELECT AVG(score), id FROM db.src GROUP BY 2", read="doris"),
    )
    adapter.ctas(
        "db.div_ctas",
        parse_one("SELECT a / b AS ratio, id FROM db.src", read="doris"),
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`unaliased_ctas` AS SELECT `id`, AVG(`score`) FROM `db`.`src` GROUP BY 1",
        "CREATE TABLE IF NOT EXISTS `db`.`div_ctas` AS SELECT `id`, `a` / `b` AS `ratio` FROM `db`.`src`",
    ]

    # 4. Sync MV creation and drop in another schema (`other_db`) restores the configured session database (`analytics`).
    adapter.cursor.execute.reset_mock()
    adapter.create_view(
        "other_db.rollup_mv",
        parse_one("SELECT id, SUM(val) AS s FROM other_db.fact GROUP BY id", read="doris"),
        replace=False,
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
    )
    adapter.drop_view("other_db.rollup_mv", materialized=True)
    assert to_sql_calls(adapter) == [
        "USE `other_db`",
        "CREATE MATERIALIZED VIEW `rollup_mv` AS SELECT `id` AS `mv_id`, SUM(`val`) AS `s` FROM `other_db`.`fact` GROUP BY `id`",
        "USE `analytics`",
        "USE `other_db`",
        "DROP MATERIALIZED VIEW IF EXISTS `rollup_mv` ON `other_db`.`fact`",
        "USE `analytics`",
    ]


def test_doris_insert_append_scd2_create_table_like_and_alter_ops(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. `insert_append` reorders OLAP columns (moving valid key `id` ahead of `score DOUBLE`)
    #    and resolves table columns when `target_columns_to_types` contains `UNKNOWN`.
    adapter.insert_append(
        "db.metrics",
        parse_one("SELECT score, id FROM db.src", read="doris"),
        target_columns_to_types={
            "score": exp.DataType.build("DOUBLE"),
            "id": exp.DataType.build("INT"),
        },
    )
    mocker.patch.object(
        adapter,
        "columns",
        return_value={
            "id": exp.DataType.build("INT"),
            "score": exp.DataType.build("DOUBLE"),
        },
    )
    adapter.insert_append(
        "db.metrics",
        parse_one("SELECT score, id FROM db.src", read="doris"),
        target_columns_to_types={
            "score": exp.DataType.build("UNKNOWN"),
            "id": exp.DataType.build("UNKNOWN"),
        },
    )
    assert to_sql_calls(adapter) == [
        "INSERT INTO `db`.`metrics` (`id`, `score`) SELECT `id`, `score` FROM (SELECT `score`, `id` FROM `db`.`src`) AS `_subquery`",
        "INSERT INTO `db`.`metrics` (`id`, `score`) SELECT `id`, `score` FROM (SELECT `score`, `id` FROM `db`.`src`) AS `_subquery`",
    ]

    # 2. `_create_table_like` on an Iceberg catalog table falls back to `create_table` with `self.columns(source_table_name)`
    #    because Doris rejects `CREATE TABLE ... LIKE` on external catalog tables.
    adapter.cursor.execute.reset_mock()
    adapter._create_table_like("iceberg_cat.db.target_tbl", "iceberg_cat.db.source_tbl")
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `iceberg_cat`.`db`.`target_tbl` (`id` INT, `score` DOUBLE) ENGINE=iceberg"
    ]

    # 3. `_describe_columns_with_keys` and `get_alter_operations` normalize `STRUCT` (unquoting field names
    #    and stripping `NULL`) and `AGG_STATE<sum(LARGEINT NULL)>`, and suppress key-role-change type modifications.
    mocker.stopall()
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)
    adapter.cursor.fetchall.side_effect = [
        [
            ("id", "INT(11)", "No", "true", None, "NONE"),
            ("k2", "INT(11)", "No", "true", None, "NONE"),
            (
                "info",
                "STRUCT<`uid`:BIGINT(20) NULL, `tag`:VARCHAR(32) NULL>",
                "Yes",
                "false",
                None,
                "NONE",
            ),
            ("st", "AGG_STATE<sum(LARGEINT NULL)>", "No", "false", None, "GENERIC"),
        ],
        [
            ("id", "INT(11)", "No", "true", None, "NONE"),
            ("k2", "BIGINT(20)", "Yes", "false", None, "NONE"),
            ("info", "STRUCT<uid: BIGINT, tag: VARCHAR(32)>", "Yes", "false", None, "NONE"),
            ("st", "AGG_STATE<sum(LARGEINT)>", "No", "false", None, "GENERIC"),
        ],
    ]
    ops = adapter.get_alter_operations("db.curr_tbl", "db.targ_tbl")
    assert ops == []

    # 4. `_build_partitioned_by_exp` normalizes `TRUNCATE(col, 10)` and `BUCKET(col, 8)` into Doris Iceberg syntax,
    #    and `_build_create_table_exp` does not force `NOT NULL` when `auto_partition = true` is paired with `VALUES IN`.
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "iceberg_cat.db.part_ice",
        {"id": exp.DataType.build("INT"), "name": exp.DataType.build("STRING")},
        partitioned_by=[
            parse_one("TRUNCATE(name, 10)", read="doris"),
            parse_one("BUCKET(id, 8)", read="doris"),
        ],
    )
    adapter.create_table(
        "db.auto_list_olap",
        {"region": exp.DataType.build("VARCHAR(32)"), "id": exp.DataType.build("INT")},
        partitioned_by=[exp.to_column("region")],
        table_properties={
            "auto_partition": exp.Literal.string("true"),
            "partitions": exp.Literal.string("PARTITION p_us VALUES IN ('US')"),
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `iceberg_cat`.`db`.`part_ice` (`id` INT, `name` STRING) ENGINE=iceberg PARTITION BY LIST (TRUNCATE(10, `name`), BUCKET(8, `id`)) ()",
        "CREATE TABLE IF NOT EXISTS `db`.`auto_list_olap` (`region` VARCHAR(32), `id` INT) AUTO PARTITION BY LIST (`region`) (PARTITION p_us VALUES IN ('US'))",
    ]


def test_doris_not_pushdown_week_quarter_and_candidate_extraction(
    make_mocked_engine_adapter: t.Callable,
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. De Morgan `NOT (a = 1 AND b = 2)` and `NOT (a = 1 OR b = 2)`, `1 != 1`, aligned & non-aligned `DATE_TRUNC(ds, 'week')` /
    #    `DATE_TRUNC(ts, 'quarter')`, and `DATE_TRUNC(ds, 'day') IS [NOT] NULL` in `delete_from`.
    adapter.delete_from("db.t", "1 != 1 AND a = 1")
    adapter.delete_from("db.t", "NOT (a = 1 OR b = 2)")
    adapter.delete_from("db.t", "NOT (a = 1 AND b = 2)")
    adapter.delete_from(
        "db.t",
        "DATE_TRUNC(ds, 'week') = '2024-01-15' AND DATE_TRUNC(ts, 'quarter') = '2024-04-01' AND DATE_TRUNC(ds, 'day') IS NOT NULL",
    )
    adapter.delete_from("db.t", "DATE_TRUNC(ds, 'week') = '2024-01-17' AND a = 1")
    adapter.delete_from("db.t", "DATE_TRUNC(ds, 'week') IN ('2024-01-16', '2024-01-17')")
    adapter.delete_from("db.t", "DATE_TRUNC(ds, 'week') IN ('2024-01-15', '2024-01-17')")
    adapter.delete_from(
        "db.t",
        "DATE_TRUNC(ds, 'week') >= '2024-01-17' AND DATE_TRUNC(ds, 'week') <= '2024-01-24'",
    )
    adapter.delete_from("db.t", "DATE_TRUNC(ds, 'week') != '2024-01-17' AND a = 1")
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`t` WHERE `a` <> 1 AND `b` <> 2",
        "DELETE FROM `db`.`t` WHERE `a` <> 1",
        "DELETE FROM `db`.`t` WHERE `b` <> 2",
        "DELETE FROM `db`.`t` WHERE `ds` >= '2024-01-15' AND `ds` < '2024-01-22' AND `ts` >= '2024-04-01' AND `ts` < '2024-07-01' AND NOT `ds` IS NULL",
        "DELETE FROM `db`.`t` WHERE `ds` >= '2024-01-15' AND `ds` < '2024-01-22'",
        "DELETE FROM `db`.`t` WHERE `ds` >= '2024-01-22' AND `ds` < '2024-01-29'",
        "DELETE FROM `db`.`t` WHERE NOT `ds` IS NULL AND `a` = 1",
    ]

    # 2. Candidate loops in `_extract_or_key_rows_from_where` and `_extract_subquery_from_where`
    #    find the valid key-row OR / subquery IN conjunct even when preceded by another OR / IN conjunct.
    adapter.cursor.execute.reset_mock()
    adapter.delete_from(
        "db.t",
        "(ds < '2024-01-01' OR ds >= '2024-01-02') AND ((id = 1 AND k = 'a') OR (id = 2 AND k = 'b'))",
    )
    assert to_sql_calls(adapter) == [
        "DELETE FROM `db`.`t` WHERE `id` = 1 AND `k` IN ('a') AND `ds` < '2024-01-01'",
        "DELETE FROM `db`.`t` WHERE `id` = 1 AND `k` IN ('a') AND `ds` >= '2024-01-02'",
        "DELETE FROM `db`.`t` WHERE `id` = 2 AND `k` IN ('b') AND `ds` < '2024-01-01'",
        "DELETE FROM `db`.`t` WHERE `id` = 2 AND `k` IN ('b') AND `ds` >= '2024-01-02'",
    ]

    # 3. Sync MV aliases that collide with base table column names (`SUM(amount) AS amount`) are prefixed with `mv_`,
    #    and `partitioned_by` / `clustered_by` kwargs are ignored for `SYNC` MVs.
    adapter.cursor.execute.reset_mock()
    adapter.create_view(
        "db.sync_agg_mv",
        parse_one("SELECT id, SUM(amount) AS amount FROM db.sales GROUP BY id", read="doris"),
        replace=False,
        materialized=True,
        view_properties={"materialized_type": exp.Literal.string("SYNC")},
        partitioned_by=[exp.to_column("id")],
        clustered_by=[exp.to_column("id")],
    )
    assert to_sql_calls(adapter) == [
        "USE `db`",
        "CREATE MATERIALIZED VIEW `sync_agg_mv` AS SELECT `id` AS `mv_id`, SUM(`amount`) AS `mv_amount` FROM `db`.`sales` GROUP BY `id`",
    ]

    # 4. `_sanitize_key_value` decodes bytes and unwraps NumPy scalars.
    import numpy as np

    assert adapter._sanitize_key_value(b"abc") == "abc"
    assert adapter._sanitize_key_value(np.int64(42)) == 42
    assert type(adapter._sanitize_key_value(np.int64(42))) is int


def test_doris_4_1_native_merge_into_and_timestamptz(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Native MERGE INTO when merge_filter is provided (automatically excludes key columns from UPDATE SET)
    adapter.merge(
        target_table="db.target",
        source_table=parse_one("SELECT id, name, val, ds FROM db.src", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "name": exp.DataType.build("VARCHAR(64)", dialect="doris"),
            "val": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
        unique_key=[exp.to_column("id")],
        merge_filter=parse_one("__MERGE_SOURCE__.ds > __MERGE_TARGET__.ds", read="doris"),
    )
    assert to_sql_calls(adapter) == [
        "MERGE INTO `db`.`target` `__MERGE_TARGET__` USING (SELECT `id`, `name`, `val`, `ds` FROM `db`.`src`) AS `__MERGE_SOURCE__` ON `__MERGE_SOURCE__`.`ds` > `__MERGE_TARGET__`.`ds` AND `__MERGE_TARGET__`.`id` = `__MERGE_SOURCE__`.`id` WHEN MATCHED THEN UPDATE SET `__MERGE_TARGET__`.`name` = `__MERGE_SOURCE__`.`name`, `__MERGE_TARGET__`.`val` = `__MERGE_SOURCE__`.`val`, `__MERGE_TARGET__`.`ds` = `__MERGE_SOURCE__`.`ds` WHEN NOT MATCHED THEN INSERT (`id`, `name`, `val`, `ds`) VALUES (`__MERGE_SOURCE__`.`id`, `__MERGE_SOURCE__`.`name`, `__MERGE_SOURCE__`.`val`, `__MERGE_SOURCE__`.`ds`)"
    ]

    # 2. Native MERGE INTO when when_matched is provided (strips any key column assignment in UPDATE SET)
    adapter.cursor.execute.reset_mock()
    when_matched = parse_one(
        "WHEN MATCHED THEN UPDATE SET __MERGE_TARGET__.id = __MERGE_SOURCE__.id, __MERGE_TARGET__.val = __MERGE_SOURCE__.val",
        into=exp.Whens,
    )
    adapter.merge(
        target_table="db.target",
        source_table=parse_one("SELECT id, name, val, ds FROM db.src", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "name": exp.DataType.build("VARCHAR(64)", dialect="doris"),
            "val": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
        unique_key=[exp.to_column("id")],
        when_matched=when_matched,
    )
    assert to_sql_calls(adapter) == [
        "MERGE INTO `db`.`target` `__MERGE_TARGET__` USING (SELECT `id`, `name`, `val`, `ds` FROM `db`.`src`) AS `__MERGE_SOURCE__` ON `__MERGE_TARGET__`.`id` = `__MERGE_SOURCE__`.`id` WHEN MATCHED THEN UPDATE SET `__MERGE_TARGET__`.`val` = `__MERGE_SOURCE__`.`val` WHEN NOT MATCHED THEN INSERT (`id`, `name`, `val`, `ds`) VALUES (`__MERGE_SOURCE__`.`id`, `__MERGE_SOURCE__`.`name`, `__MERGE_SOURCE__`.`val`, `__MERGE_SOURCE__`.`ds`)"
    ]

    # 3. AtTimeZone rewrites to CONVERT_TZ(..., @@time_zone, '+00:00')
    norm_expr = adapter.normalize_value(
        exp.to_column("ts"),
        exp.DataType.build("TIMESTAMPTZ"),
        decimal_precision=3,
        timestamp_precision=6,
    )
    assert (
        adapter._to_sql(norm_expr)
        == "CAST(DATE_FORMAT(CONVERT_TZ(`ts`, @@time_zone, '+00:00'), '%Y-%m-%d %T.%f') AS VARCHAR)"
    )

    # 4. TIMESTAMPTZ renders as TIMESTAMPTZ (not MySQL's TIMESTAMP), defaults to precision 0, and supports
    #    same-type precision widening (TIMESTAMPTZ -> TIMESTAMPTZ(0) -> TIMESTAMPTZ(6), DATETIME(2) -> DATETIME(6)),
    #    while cross-type TIMESTAMPTZ <-> DATETIME is treated as destructive.
    assert exp.DataType.build("TIMESTAMPTZ", dialect="doris").sql("doris") == "TIMESTAMPTZ"
    assert exp.DataType.build("TIMESTAMPTZ(6)", dialect="doris").sql("doris") == "TIMESTAMPTZ(6)"
    from sqlmesh.core.schema_diff import TableAlterChangeColumnTypeOperation

    ops_default = adapter.schema_differ.compare_columns(
        "t",
        {"ts": exp.DataType.build("TIMESTAMPTZ", dialect="doris")},
        {"ts": exp.DataType.build("TIMESTAMPTZ(0)", dialect="doris")},
    )
    assert (
        len(ops_default) == 1
        and isinstance(ops_default[0], TableAlterChangeColumnTypeOperation)
        and ops_default[0].is_part_of_destructive_change is False
    )
    ops = adapter.schema_differ.compare_columns(
        "t",
        {"ts": exp.DataType.build("TIMESTAMPTZ(2)", dialect="doris")},
        {"ts": exp.DataType.build("TIMESTAMPTZ(6)", dialect="doris")},
    )
    assert (
        len(ops) == 1
        and isinstance(ops[0], TableAlterChangeColumnTypeOperation)
        and ops[0].is_part_of_destructive_change is False
    )
    ops_dt = adapter.schema_differ.compare_columns(
        "t",
        {"dt": exp.DataType.build("DATETIME(2)", dialect="doris")},
        {"dt": exp.DataType.build("DATETIME(6)", dialect="doris")},
    )
    assert (
        len(ops_dt) == 1
        and isinstance(ops_dt[0], TableAlterChangeColumnTypeOperation)
        and ops_dt[0].is_part_of_destructive_change is False
    )
    ops_cross = adapter.schema_differ.compare_columns(
        "t",
        {"ts": exp.DataType.build("TIMESTAMPTZ(6)", dialect="doris")},
        {"ts": exp.DataType.build("DATETIME(6)", dialect="doris")},
        ignore_destructive=True,
    )
    assert ops_cross == []

    # 5. alter_table retries when Doris raises `state(SCHEMA_CHANGE) is not NORMAL`
    call_count = 0

    def _flaky_execute(sql: str, **kwargs: t.Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError(
                "errCode = 2, detailMessage = Table[t]'s state(SCHEMA_CHANGE) is not NORMAL. Do not allow doing ALTER ops"
            )

    adapter.cursor.execute.reset_mock()
    adapter.cursor.execute.side_effect = _flaky_execute
    adapter.alter_table(
        [
            t.cast(
                exp.Alter, parse_one("ALTER TABLE `db`.`t` MODIFY COLUMN `a` BIGINT", read="doris")
            ),
            t.cast(
                exp.Alter,
                parse_one("ALTER TABLE `db`.`t` MODIFY COLUMN `b` DATETIME(6)", read="doris"),
            ),
        ]
    )
    assert call_count == 3
    adapter.cursor.execute.side_effect = None


def test_lance_table_reading_and_write_guard(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    from sqlmesh.core.dialect import find_tables

    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Reading from a Doris Lance catalog table extracts upstream table dependency cleanly
    lance_select = parse_one(
        "SELECT id, L2_DISTANCE(vec, [1.0, 2.0, 3.0]) AS dist FROM lance_cat.ml_db.embeddings ORDER BY dist LIMIT 5",
        read="doris",
    )
    assert (
        lance_select.sql("doris")
        == "SELECT id, L2_DISTANCE(vec, ARRAY(1.0, 2.0, 3.0)) AS dist FROM lance_cat.ml_db.embeddings ORDER BY dist LIMIT 5"
    )
    assert find_tables(lance_select, default_catalog=None, dialect="doris") == {
        '"lance_cat"."ml_db"."embeddings"'
    }

    # 2. Reading from a Doris Lance table-valued function (TVF)
    lance_tvf = parse_one(
        "SELECT id, vec FROM s3('uri' = 's3://bucket/data.lance', 'format' = 'lance')",
        read="doris",
    )
    assert (
        lance_tvf.sql("doris")
        == "SELECT id, vec FROM S3('uri' = 's3://bucket/data.lance', 'format' = 'lance')"
    )

    # 3. Attempting to materialize / write a Lance table raises a clear SQLMeshError
    with pytest.raises(SQLMeshError, match="Doris Lance tables are read-only"):
        adapter.create_table(
            "lance_cat.ml_db.out_tbl",
            target_columns_to_types={"id": exp.DataType.build("INT", dialect="doris")},
            table_format="lance",
        )

    with pytest.raises(SQLMeshError, match="Doris Lance tables are read-only"):
        adapter.ctas(
            "lance_cat.ml_db.out_tbl",
            parse_one("SELECT 1 AS id", read="doris"),
            table_properties={"engine": exp.Literal.string("lance")},
        )

    with pytest.raises(SQLMeshError, match="Doris Hudi tables are read-only"):
        adapter.create_table(
            "hudi_cat.ml_db.out_tbl",
            target_columns_to_types={"id": exp.DataType.build("INT", dialect="doris")},
            table_format="hudi",
        )

    # 4. External Paimon catalog table creation emits ENGINE=paimon, 'file.format', and 'primary-key'
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "paimon_cat.silver.orders",
        target_columns_to_types={
            "payload": exp.DataType.build("STRING", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
            "ds": exp.DataType.build("DATE", dialect="doris"),
        },
        table_format="paimon",
        storage_format="parquet",
        partitioned_by=[exp.to_column("ds")],
        unique_key=[exp.to_column("id")],
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `paimon_cat`.`silver`.`orders` (`payload` STRING, `id` INT, `ds` DATE) ENGINE=paimon PARTITION BY LIST (`ds`) () PROPERTIES ('file.format'='parquet', 'primary-key'='id,ds')"
    ]

    # 5. External Hive catalog table creation emits ENGINE=hive, 'file_format', and skips OLAP key reordering
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "hive_cat.raw_db.events",
        target_columns_to_types={
            "payload": exp.DataType.build("STRING", dialect="doris"),
            "id": exp.DataType.build("INT", dialect="doris"),
        },
        table_format="hive",
        storage_format="orc",
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `hive_cat`.`raw_db`.`events` (`payload` STRING, `id` INT) ENGINE=hive PROPERTIES ('file_format'='orc')"
    ]


def test_iceberg_reading_time_travel_branch_and_compaction_ddl(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
):
    from sqlmesh.core.dialect import find_tables

    _ = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Iceberg time travel (FOR TIME AS OF, FOR VERSION AS OF) and branch/tag (@branch, @tag)
    for sql, expected in [
        (
            "SELECT * FROM ice_cat.db.tbl FOR TIME AS OF '2026-01-01 00:00:00' AS t",
            "SELECT * FROM ice_cat.db.tbl FOR TIME AS OF '2026-01-01 00:00:00' AS t",
        ),
        (
            "SELECT * FROM ice_cat.db.tbl FOR VERSION AS OF 123456789 AS t",
            "SELECT * FROM ice_cat.db.tbl FOR VERSION AS OF 123456789 AS t",
        ),
        (
            "SELECT * FROM ice_cat.db.tbl@branch(audit_branch) AS t",
            "SELECT * FROM ice_cat.db.tbl @branch(audit_branch) AS t",
        ),
        (
            "SELECT * FROM ice_cat.db.tbl @tag(v1_release) AS t",
            "SELECT * FROM ice_cat.db.tbl @tag(v1_release) AS t",
        ),
    ]:
        parsed = parse_one(sql, read="doris")
        assert parsed.sql("doris") == expected
        # Upstream table dependency extraction preserves `ice_cat.db.tbl` without `@branch(...)`
        tables = list(parsed.find_all(exp.Table))
        assert [(tbl.catalog, tbl.db, tbl.name) for tbl in tables] == [("ice_cat", "db", "tbl")]
        assert find_tables(parsed, default_catalog=None, dialect="doris") == {
            '"ice_cat"."db"."tbl"'
        }

    # 2. Iceberg compaction (rewrite_data_files), snapshot expiration, and branch/tag DDL parse as exp.Alter
    for ddl in [
        "ALTER TABLE ice_cat.db.tbl EXECUTE rewrite_data_files('target-file-size-bytes' = '134217728')",
        "ALTER TABLE ice_cat.db.tbl EXECUTE expire_snapshots('older_than' = '2026-01-01 00:00:00')",
        "ALTER TABLE ice_cat.db.tbl EXECUTE remove_orphan_files('older_than' = '2026-01-01 00:00:00')",
        "ALTER TABLE ice_cat.db.tbl CREATE BRANCH audit_branch",
        "ALTER TABLE ice_cat.db.tbl CREATE TAG v1_tag",
        "ALTER TABLE ice_cat.db.tbl DROP BRANCH IF EXISTS audit_branch",
    ]:
        parsed_ddl = parse_one(ddl, read="doris")
        assert isinstance(parsed_ddl, exp.Alter)
        assert parsed_ddl.this.sql("doris") == "ice_cat.db.tbl"
        assert parsed_ddl.sql("doris") == ddl

    # 3. Full SQLMesh model loading & @IF post-statement rendering with @this_model resolution
    from sqlmesh.core.dialect import parse
    from sqlmesh.core.macros import RuntimeStage
    from sqlmesh.core.model import load_sql_based_model

    model_sql = """
    MODEL (
      name ice_cat.silver.curated_events,
      kind FULL,
      dialect doris,
      table_format iceberg
    );
    SELECT
      e.id,
      e.msg
    FROM ice_cat.bronze.events FOR VERSION AS OF 123456 AS e
    JOIN ice_cat.bronze.tags@tag(v1) AS t ON e.id = t.id
    WHERE e.msg MATCH_PHRASE 'hello world' AND SEARCH('msg:hello');

    @IF(
      @runtime_stage = 'evaluating',
      ALTER TABLE @this_model EXECUTE rewrite_data_files('target-file-size-bytes' = '134217728')
    );
    @IF(
      @runtime_stage = 'creating',
      ALTER TABLE @this_model ADD INDEX idx_msg (msg) USING INVERTED PROPERTIES ('parser' = 'english')
    );
    @IF(
      @runtime_stage = 'evaluating',
      BUILD INDEX idx_msg ON @this_model PARTITION (p1)
    );
    """
    model = load_sql_based_model(
        parse(model_sql, default_dialect="doris"), default_catalog="internal", dialect="doris"
    )
    assert model.depends_on == {'"ice_cat"."bronze"."events"', '"ice_cat"."bronze"."tags"'}
    assert [
        ps.sql("doris") for ps in model.render_post_statements(runtime_stage=RuntimeStage.CREATING)
    ] == [
        "ALTER TABLE `ice_cat`.`silver`.`curated_events` ADD INDEX `idx_msg` (`msg`) USING INVERTED PROPERTIES ('parser'='english')"
    ]
    assert [
        ps.sql("doris")
        for ps in model.render_post_statements(runtime_stage=RuntimeStage.EVALUATING)
    ] == [
        "ALTER TABLE `ice_cat`.`silver`.`curated_events` EXECUTE rewrite_data_files('target-file-size-bytes' = '134217728')",
        "BUILD INDEX `idx_msg` ON `ice_cat`.`silver`.`curated_events` PARTITION (`p1`)",
    ]


def test_doris_4_1_search_indexes_compaction_and_sequence_col(
    make_mocked_engine_adapter: t.Callable[..., DorisEngineAdapter],
    mocker: MockerFixture,
):
    adapter = make_mocked_engine_adapter(DorisEngineAdapter)

    # 1. Full-text search (SEARCH, MATCH_ANY, MATCH_PHRASE, etc.) and vector distance functions
    ft_query = parse_one(
        "SELECT id, L1_DISTANCE(v1, v2) AS l1, INNER_PRODUCT(v1, v2) AS ip "
        "FROM db.docs WHERE title MATCH_PHRASE 'apache doris' AND SEARCH('title:doris AND body:sqlmesh')",
        read="doris",
    )
    assert (
        ft_query.sql("doris")
        == "SELECT id, L1_DISTANCE(v1, v2) AS l1, INNER_PRODUCT(v1, v2) AS ip "
        "FROM db.docs WHERE title MATCH_PHRASE 'apache doris' AND SEARCH('title:doris AND body:sqlmesh')"
    )

    # 2. Index DDL: create_index with USING INVERTED / ANN, PROPERTIES, COMMENT, and BUILD INDEX
    adapter.create_index(
        "db.docs",
        "idx_title_ft",
        [exp.to_column("title")],
        index_type="INVERTED",
        index_properties={"parser": "english", "support_phrase": "true"},
        comment="fulltext index",
    )
    adapter.create_index(
        "db.docs",
        "idx_vec_ann",
        [exp.to_column("embedding")],
        index_type="ANN",
        index_properties={"index_type": "hnsw", "metric_type": "l2_distance", "dim": "128"},
    )
    assert to_sql_calls(adapter) == [
        "CREATE INDEX IF NOT EXISTS `idx_title_ft` ON `db`.`docs`(`title`) USING INVERTED PROPERTIES ('parser'='english', 'support_phrase'='true') COMMENT 'fulltext index'",
        "CREATE INDEX IF NOT EXISTS `idx_vec_ann` ON `db`.`docs`(`embedding`) USING ANN PROPERTIES ('index_type'='hnsw', 'metric_type'='l2_distance', 'dim'='128')",
    ]

    build_idx = parse_one("BUILD INDEX idx_title_ft ON db.docs PARTITIONS (p1, p2)", read="doris")
    assert isinstance(build_idx, exp.Alter)
    assert build_idx.this.sql("doris") == "db.docs"
    assert build_idx.sql("doris") == "BUILD INDEX idx_title_ft ON db.docs PARTITIONS (p1, p2)"

    build_idx_singular = parse_one(
        "BUILD INDEX idx_title_ft ON db.docs PARTITION (p1, p2)", read="doris"
    )
    assert isinstance(build_idx_singular, exp.Alter)
    assert build_idx_singular.this.sql("doris") == "db.docs"
    assert (
        build_idx_singular.sql("doris") == "BUILD INDEX idx_title_ft ON db.docs PARTITION (p1, p2)"
    )

    alter_add_idx = parse_one(
        "ALTER TABLE db.docs ADD INDEX idx_title (title) USING INVERTED PROPERTIES ('parser' = 'english') COMMENT 'ft'",
        read="doris",
    )
    assert isinstance(alter_add_idx, exp.Alter)
    assert (
        alter_add_idx.sql("doris")
        == "ALTER TABLE db.docs ADD INDEX idx_title (title) USING INVERTED PROPERTIES ('parser'='english') COMMENT 'ft'"
    )
    alter_drop_idx = parse_one("ALTER TABLE db.docs DROP INDEX idx_title", read="doris")
    assert isinstance(alter_drop_idx, exp.Alter)
    assert alter_drop_idx.sql("doris") == "ALTER TABLE db.docs DROP INDEX idx_title"

    # 3. OLAP compaction_policy validation and function_column.sequence_col support
    adapter.cursor.execute.reset_mock()
    adapter.create_table(
        "db.ts_metrics",
        target_columns_to_types={
            "id": exp.DataType.build("BIGINT", dialect="doris"),
            "updated_at": exp.DataType.build("DATETIME", dialect="doris"),
            "val": exp.DataType.build("DOUBLE", dialect="doris"),
        },
        table_properties={
            "unique_key": exp.to_column("id"),
            "function_column.sequence_col": exp.Literal.string("updated_at"),
            "compaction_policy": exp.Literal.string("time_series"),
        },
    )
    assert to_sql_calls(adapter) == [
        "CREATE TABLE IF NOT EXISTS `db`.`ts_metrics` (`id` BIGINT, `updated_at` DATETIME, `val` DOUBLE) UNIQUE KEY (`id`) DISTRIBUTED BY HASH (`id`) BUCKETS 10 PROPERTIES ('function_column.sequence_col'='updated_at', 'compaction_policy'='time_series')"
    ]

    with pytest.raises(SQLMeshError, match="Invalid Doris compaction_policy"):
        adapter.create_table(
            "db.bad_compaction",
            target_columns_to_types={"id": exp.DataType.build("INT", dialect="doris")},
            table_properties={"compaction_policy": exp.Literal.string("invalid_policy")},
        )

    # 4. _replace_by_key skips pre-DELETE when function_column.sequence_col is configured so sequence comparison works
    adapter.cursor.execute.reset_mock()
    adapter._replace_by_key(
        target_table="db.ts_metrics",
        source_table=parse_one("SELECT id, updated_at, val FROM db.src", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("BIGINT", dialect="doris"),
            "updated_at": exp.DataType.build("DATETIME", dialect="doris"),
            "val": exp.DataType.build("DOUBLE", dialect="doris"),
        },
        key=[exp.to_column("id")],
        is_unique_key=True,
        table_properties={"function_column.sequence_col": exp.Literal.string("updated_at")},
    )
    sql_calls = to_sql_calls(adapter)
    assert not any(call.startswith("DELETE FROM") for call in sql_calls)
    assert any(
        "PROPERTIES ('function_column.sequence_col'='updated_at')" in call for call in sql_calls
    )

    # 5. Self-referencing replace_query with aliased, default-catalog-qualified table reference preserves alias
    adapter._default_catalog = "internal"
    adapter.cursor.execute.reset_mock()
    mocker.patch("sqlmesh.core.engine_adapter.doris.random_id", return_value="abcd1234")
    mocker.patch.object(
        adapter,
        "get_data_object",
        return_value=DataObject(
            catalog="internal", schema="db", name="tbl", type=DataObjectType.TABLE
        ),
    )
    adapter.replace_query(
        "db.tbl",
        parse_one("SELECT t.id, t.val FROM internal.db.tbl AS t", read="doris"),
        target_columns_to_types={
            "id": exp.DataType.build("INT", dialect="doris"),
            "val": exp.DataType.build("INT", dialect="doris"),
        },
    )
    self_ref_calls = to_sql_calls(adapter)
    assert any(
        "CREATE TABLE IF NOT EXISTS `internal`.`db`.`__temp_tbl_abcd1234`" in c
        for c in self_ref_calls
    )
    assert any("FROM `internal`.`db`.`__temp_tbl_abcd1234` AS `t`" in c for c in self_ref_calls)
