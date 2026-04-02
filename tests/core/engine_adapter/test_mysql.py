# type: ignore
import typing as t
from unittest.mock import call

from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one

from sqlmesh.core.engine_adapter import MySQLEngineAdapter
from tests.core.engine_adapter import to_sql_calls


def test_comments(make_mocked_engine_adapter: t.Callable, mocker: MockerFixture):
    adapter = make_mocked_engine_adapter(MySQLEngineAdapter)

    allowed_table_comment_length = MySQLEngineAdapter.MAX_TABLE_COMMENT_LENGTH
    truncated_table_comment = "a" * allowed_table_comment_length
    long_table_comment = truncated_table_comment + "b"

    allowed_column_comment_length = MySQLEngineAdapter.MAX_COLUMN_COMMENT_LENGTH
    truncated_column_comment = "c" * allowed_column_comment_length
    long_column_comment = truncated_column_comment + "d"

    fetchone_mock = mocker.patch("sqlmesh.core.engine_adapter.mysql.MySQLEngineAdapter.fetchone")
    fetchone_mock.return_value = ["test_table", "CREATE TABLE test_table (a INT)"]

    adapter.create_table(
        "test_table",
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.ctas(
        "test_table",
        parse_one("SELECT a, b FROM source_table"),
        {"a": exp.DataType.build("INT"), "b": exp.DataType.build("INT")},
        table_description=long_table_comment,
        column_descriptions={"a": long_column_comment},
    )

    adapter.create_view(
        "test_view",
        parse_one("SELECT a, b FROM source_table"),
        table_description=long_table_comment,
    )

    adapter._create_table_comment(
        "test_table",
        long_table_comment,
    )

    adapter._create_column_comments(
        "test_table",
        {"a": long_column_comment},
    )

    sql_calls = to_sql_calls(adapter)
    assert sql_calls == [
        f"CREATE TABLE IF NOT EXISTS `test_table` (`a` INT COMMENT '{truncated_column_comment}', `b` INT) COMMENT='{truncated_table_comment}'",
        f"CREATE TABLE IF NOT EXISTS `test_table` COMMENT='{truncated_table_comment}' AS SELECT CAST(`a` AS SIGNED) AS `a`, CAST(`b` AS SIGNED) AS `b` FROM (SELECT `a`, `b` FROM `source_table`) AS `_subquery`",
        f"ALTER TABLE `test_table` MODIFY `a` INT COMMENT '{truncated_column_comment}'",
        "CREATE OR REPLACE VIEW `test_view` AS SELECT `a`, `b` FROM `source_table`",
        f"ALTER TABLE `test_table` COMMENT = '{truncated_table_comment}'",
        f"ALTER TABLE `test_table` MODIFY `a` INT COMMENT '{truncated_column_comment}'",
    ]


def test_pre_ping(mocker: MockerFixture, make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(MySQLEngineAdapter)
    adapter._pre_ping = True

    adapter.execute("SELECT 'test'")

    assert to_sql_calls(adapter) == [
        "SELECT 'test'",
    ]

    adapter._connection_pool.get().ping.assert_called_once_with(reconnect=False)


def test_create_table_like(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(MySQLEngineAdapter)

    adapter.create_table_like("target_table", "source_table")
    adapter.cursor.execute.assert_called_once_with(
        "CREATE TABLE IF NOT EXISTS `target_table` LIKE `source_table`"
    )


def test_replace_by_key_composite_uses_join_delete(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    """Composite key DELETE uses JOIN instead of CONCAT_WS to allow index usage."""
    adapter = make_mocked_engine_adapter(MySQLEngineAdapter)
    temp_table_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter._get_temp_table"
    )
    temp_table_mock.return_value = exp.to_table("temporary")

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, ts, val FROM source")),
        target_columns_to_types={
            "id": exp.DataType(this=exp.DataType.Type.INT),
            "ts": exp.DataType(this=exp.DataType.Type.TIMESTAMP),
            "val": exp.DataType(this=exp.DataType.Type.INT),
        },
        unique_key=[parse_one("id"), parse_one("ts")],
    )

    sql_calls = to_sql_calls(adapter)

    # The DELETE should use a JOIN instead of CONCAT_WS
    assert any("CONCAT_WS" in s for s in sql_calls) is False, (
        "DELETE should not use CONCAT_WS for composite keys"
    )
    assert any("INNER JOIN" in s for s in sql_calls) is True, (
        "DELETE should use INNER JOIN for composite keys"
    )

    # Verify the full sequence of SQL calls
    adapter.cursor.execute.assert_has_calls(
        [
            call(
                "CREATE TABLE `temporary` AS SELECT CAST(`id` AS SIGNED) AS `id`, CAST(`ts` AS DATETIME) AS `ts`, CAST(`val` AS SIGNED) AS `val` FROM (SELECT `id`, `ts`, `val` FROM `source`) AS `_subquery`"
            ),
            call(
                "DELETE `_target` FROM `target` AS `_target` INNER JOIN `temporary` AS `_temp` ON `_target`.`id` = `_temp`.`id` AND `_target`.`ts` = `_temp`.`ts`"
            ),
            call(
                "INSERT INTO `target` (`id`, `ts`, `val`) SELECT `id`, `ts`, `val` FROM (SELECT `id` AS `id`, `ts` AS `ts`, `val` AS `val`, ROW_NUMBER() OVER (PARTITION BY `id`, `ts` ORDER BY `id`, `ts`) AS _row_number FROM `temporary`) AS _t WHERE _row_number = 1"
            ),
            call("DROP TABLE IF EXISTS `temporary`"),
        ]
    )


def test_replace_by_key_single_key_uses_in(
    make_mocked_engine_adapter: t.Callable, mocker: MockerFixture
):
    """Single key DELETE still uses the IN-based approach (indexes work fine for single column)."""
    adapter = make_mocked_engine_adapter(MySQLEngineAdapter)
    temp_table_mock = mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter._get_temp_table"
    )
    temp_table_mock.return_value = exp.to_table("temporary")

    adapter.merge(
        target_table="target",
        source_table=t.cast(exp.Select, parse_one("SELECT id, val FROM source")),
        target_columns_to_types={
            "id": exp.DataType(this=exp.DataType.Type.INT),
            "val": exp.DataType(this=exp.DataType.Type.INT),
        },
        unique_key=[parse_one("id")],
    )

    sql_calls = to_sql_calls(adapter)

    # Single key should use IN-based approach, not JOIN
    assert any("IN" in s and "DELETE" in s for s in sql_calls) is True
    assert any("INNER JOIN" in s for s in sql_calls) is False
