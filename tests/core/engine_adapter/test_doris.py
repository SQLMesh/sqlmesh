import typing as t

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

    # Write statements (e.g. INSERT) must NOT be retried after OperationalError(2013) to avoid duplicating rows
    conn_mock.reset_mock()
    close_spy.reset_mock()
    write_calls = 0

    def write_side_effect(*args, **kwargs):
        nonlocal write_calls
        write_calls += 1
        raise OperationalError(2013, "Lost connection to MySQL server during query")

    adapter.cursor.execute.side_effect = write_side_effect
    with pytest.raises(OperationalError):
        adapter._execute("INSERT INTO `tbl` VALUES (1)")

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
    assert any(
        "SELECT DISTINCT `ds` FROM `db`.`__temp_tbl`" in call
        or "USING (SELECT DISTINCT `ds` FROM `db`.`__temp_tbl`)" in call
        for call in sql_calls
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
        "DROP MATERIALIZED VIEW IF EXISTS `db`.`sync_mv` ON `db`.`orders`",
        "CREATE MATERIALIZED VIEW `db`.`sync_mv` AS SELECT `region` AS `mv_region`, SUM(`amount`) AS `total` FROM `db`.`orders` GROUP BY `region`",
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

    adapter.delete_from(
        "target_table",
        where=parse_one(
            "(ds BETWEEN '2024-01-01' AND '2024-01-07' OR status = 'expired') AND id NOT BETWEEN 10 AND 20",
            read="doris",
        ),
    )

    assert to_sql_calls(adapter) == [
        "DELETE FROM `target_table` WHERE ((`ds` >= '2024-01-01' AND `ds` <= '2024-01-07') OR `status` = 'expired') AND NOT `id` BETWEEN 10 AND 20",
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
    assert sync_calls[0] == (
        f"DROP MATERIALIZED VIEW IF EXISTS `doris_db`.`orders_sync_mv` ON `{physical_tbl.db}`.`{physical_tbl.name}`"
    )


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

    # 4. insert_overwrite_by_partition deduplicates columns and strips table qualifiers
    adapter.cursor.execute.reset_mock()
    mocker.patch.object(
        adapter,
        "_get_temp_table",
        return_value=exp.table_("__temp_tbl", db="db"),
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
    assert any(
        "DELETE FROM `db`.`events` AS `_t1` USING (SELECT DISTINCT `ds` FROM `db`.`__temp_tbl`) AS `_t2` WHERE `_t1`.`ds` = `_t2`.`ds`"
        == call
        for call in overwrite_calls
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
    assert "mv_infos('database' = DATABASE())" in second_query_sql


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
        "CREATE VIEW `db`.`unannotated_view` (`id` COMMENT 'Primary user ID', `name`) COMMENT 'Users view' AS SELECT `id`, `name` FROM `db`.`users`",
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
        "CREATE TABLE IF NOT EXISTS `sqlmesh_temp`.`diff` AS WITH `__source` AS (SELECT `s`.`name` AS `name`, `s`.`id` AS `__sqlmesh_join_key` FROM `db`.`src` AS `s`), `__target` AS (SELECT `t`.`name` AS `name`, `t`.`id` AS `__sqlmesh_join_key` FROM `db`.`tgt` AS `t`), `__stats` AS (SELECT CASE WHEN NOT `s`.`__sqlmesh_join_key` IS NULL THEN 1 ELSE 0 END AS `s_exists`, `s`.`name` AS `s__name`, `t`.`name` AS `t__name`, CASE WHEN NOT `t`.`__sqlmesh_join_key` IS NULL THEN 1 ELSE 0 END AS `t_exists` FROM `__source` AS `s` LEFT JOIN `__target` AS `t` ON `s`.`__sqlmesh_join_key` = `t`.`__sqlmesh_join_key`) SELECT *, CASE WHEN `s__name` = `t__name` THEN 1 ELSE 0 END AS `row_full_match` FROM `__stats`",
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
