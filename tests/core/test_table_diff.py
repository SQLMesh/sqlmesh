import pytest
from pytest_mock.plugin import MockerFixture
import pandas as pd  # noqa: TID253
from sqlglot import exp
from sqlmesh.core import dialect as d
import typing as t
from io import StringIO
from rich.console import Console
from sqlmesh.core.console import TerminalConsole
from sqlmesh.core.context import Context
from sqlmesh.core.config import AutoCategorizationMode, CategorizerConfig, DuckDBConnectionConfig
from sqlmesh.core.model import SqlModel, load_sql_based_model
from sqlmesh.core.model.common import ParsableSql
from sqlmesh.core.table_diff import TableDiff, SchemaDiff
import numpy as np  # noqa: TID253
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.rich import strip_ansi_codes

pytestmark = pytest.mark.slow


def create_test_console() -> t.Tuple[StringIO, TerminalConsole]:
    """Creates a console and buffer for validating console output."""
    console_output = StringIO()
    console = Console(file=console_output, force_terminal=True)
    terminal_console = TerminalConsole(console=console)
    return console_output, terminal_console


def capture_console_output(method_name: str, **kwargs) -> str:
    """Factory function to invoke and capture output a TerminalConsole method.

    Args:
        method_name: Name of the TerminalConsole method to call
        **kwargs: Arguments to pass to the method

    Returns:
        The captured output as a string
    """
    console_output, terminal_console = create_test_console()
    try:
        method = getattr(terminal_console, method_name)
        method(**kwargs)
        return console_output.getvalue()
    finally:
        console_output.close()


def test_data_diff(sushi_context_fixed_date, capsys, caplog):
    model = sushi_context_fixed_date.models['"memory"."sushi"."customer_revenue_by_day"']

    sushi_context_fixed_date.upsert_model(
        model,
        query_=ParsableSql(
            sql=model.query.select(exp.cast("'1'", "VARCHAR").as_("modified_col"), "1 AS y").sql(
                model.dialect
            )
        ),
    )

    sushi_context_fixed_date.plan(
        "source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )
    model = sushi_context_fixed_date.models['"memory"."sushi"."customer_revenue_by_day"']

    for column in model.query.find_all(exp.Column):
        if column.name == "total":
            column.replace(
                exp.func(
                    "IF",
                    exp.column("total", "ot") < 4,
                    exp.column("total", "ot") * 0.1,
                    exp.column("total", "ot"),
                )
            )

    for alias in model.query.find_all(exp.Alias):
        if alias.alias == "modified_col":
            alias.this.set("to", exp.DataType.build("INT"))
        if alias.alias == "y":
            alias.pop()

    model.query.select("1 AS z", copy=False).limit(10000, copy=False)
    # add row to table with UNION
    modified_model = model.dict()
    modified_model["query"] = (
        exp.select("*")
        .from_(model.query.subquery())
        .union("select -1, 9999.00, 0, CAST('2023-01-31' AS DATE), 1, 1")
    )
    modified_sqlmodel = SqlModel(**modified_model)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel)

    sushi_context_fixed_date.auto_categorize_changes = CategorizerConfig(
        sql=AutoCategorizationMode.FULL
    )
    sushi_context_fixed_date.plan(
        "target_dev",
        create_from="source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    diff = sushi_context_fixed_date.table_diff(
        source="source_dev",
        target="target_dev",
        on=exp.condition("s.customer_id = t.customer_id AND s.event_date = t.event_date"),
        select_models={"sushi.customer_revenue_by_day"},
    )[0]

    # verify queries were actually logged to the log file, this helps immensely with debugging
    console_output = capsys.readouterr()
    assert "__sqlmesh_join_key" not in console_output  # they should not go to the console
    assert "__sqlmesh_join_key" in caplog.text

    schema_diff = diff.schema_diff()
    assert schema_diff.added == [("z", exp.DataType.build("int"))]
    assert schema_diff.modified == {
        "modified_col": (exp.DataType.build("text"), exp.DataType.build("int"))
    }
    assert schema_diff.removed == [("y", exp.DataType.build("int"))]

    row_diff = diff.row_diff()
    assert row_diff.source_count == 17
    assert row_diff.target_count == 18
    assert row_diff.join_count == 17
    assert row_diff.full_match_count == 17
    assert row_diff.full_match_pct == 97.14
    assert row_diff.partial_match_count == 0.0
    assert row_diff.partial_match_pct == 0.0
    assert row_diff.s_only_count == 0
    assert row_diff.t_only_count == 1
    assert row_diff.sample.shape == (1, 12)
    assert row_diff.joined_sample.shape == (0, 2)
    assert row_diff.s_sample.shape == (0, 6)
    assert row_diff.t_sample.shape == (1, 6)


def test_data_diff_decimals_on_float(sushi_context_fixed_date):
    engine_adapter = sushi_context_fixed_date.engine_adapter

    engine_adapter.ctas(
        "table_diff_source",
        pd.DataFrame(
            {
                "key": [1, 2, 3],
                "value": [1.0, 2.0, 3.1233],
            }
        ),
    )

    engine_adapter.ctas(
        "table_diff_target",
        pd.DataFrame(
            {
                "key": [1, 2, 3],
                "value": [1.0, 2.0, 3.1234321],
            }
        ),
    )

    diff = sushi_context_fixed_date.table_diff(
        source="table_diff_source",
        target="table_diff_target",
        on=["key"],
    )[0]
    assert diff.row_diff().full_match_count == 3
    assert diff.row_diff().partial_match_count == 0

    diff = sushi_context_fixed_date.table_diff(
        source="table_diff_source",
        target="table_diff_target",
        on=["key"],
        decimals=4,
    )[0]

    row_diff = diff.row_diff()
    joined_sample_columns = row_diff.joined_sample.columns
    assert row_diff.full_match_count == 2
    assert row_diff.partial_match_count == 1
    assert "s__value" in joined_sample_columns
    assert "t__value" in joined_sample_columns

    table_diff = TableDiff(
        adapter=engine_adapter,
        source="table_diff_source",
        target="table_diff_target",
        source_alias="dev",
        target_alias="prod",
        on=["key"],
        decimals=4,
    )

    aliased_joined_sample = table_diff.row_diff().joined_sample.columns
    assert "DEV__value" in aliased_joined_sample
    assert "PROD__value" in aliased_joined_sample

    output = capture_console_output("show_row_diff", row_diff=table_diff.row_diff())

    # Expected output with box-drawings
    expected_output = r"""
Row Counts:
├──  FULL MATCH: 2 rows (66.67%)
└──  PARTIAL MATCH: 1 rows (33.33%)

COMMON ROWS column comparison stats:
       pct_match
value  66.666667


COMMON ROWS sample data differences:
Column: value
┏━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ key ┃ DEV    ┃ PROD   ┃
┡━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ 3.0 │ 3.1233 │ 3.1234 │
└─────┴────────┴────────┘
"""

    stripped_output = strip_ansi_codes(output)
    stripped_expected = expected_output.strip()
    assert stripped_output == stripped_expected


def test_data_diff_decimals_on_numeric():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {
        "key": exp.DataType.build("int"),
        "value": exp.DataType.build("decimal(10,5)"),
    }

    engine_adapter.create_table("src", columns_to_types)
    engine_adapter.create_table("target", columns_to_types)

    src_records = [
        (1, "25.12344"),
        (2, "25.1234"),
        (3, "25.124"),
        (4, "25.14"),
        (5, "25.4"),
    ]

    target_records = [
        (1, "25.12343"),
        (2, "25.1233"),
        (3, "25.123"),
        (4, "25.13"),
        (5, "25.3"),
    ]

    src_df = pd.DataFrame(data=src_records, columns=columns_to_types.keys())
    target_df = pd.DataFrame(data=target_records, columns=columns_to_types.keys())

    engine_adapter.insert_append("src", src_df)
    engine_adapter.insert_append("target", target_df)

    for decimals in range(5, 0, -1):
        table_diff = TableDiff(
            adapter=engine_adapter, source="src", target="target", on=["key"], decimals=decimals
        )
        diff = table_diff.row_diff()

        assert diff.full_match_count == 5 - decimals
        assert diff.partial_match_count + diff.full_match_count == 5


def test_grain_check(sushi_context_fixed_date):
    expressions = d.parse(
        """
        MODEL (name memory.sushi.grain_items, kind full, grain("key_1", KEY_2));
        SELECT
            key_1 as "key_1",
            KEY_2,
            value,
        FROM
            (VALUES
                (1, 1, 1),
                (7, 4, 2),
                (NULL, 3, 3),
                (NULL, NULL, 3),
                (1, 2, 2),
                (4, NULL, 3),
                (2, 3, 2),
            ) AS t (key_1,KEY_2, value)
    """
    )
    model_s = load_sql_based_model(expressions, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s)
    sushi_context_fixed_date.plan(
        "source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    model = sushi_context_fixed_date.models['"MEMORY"."SUSHI"."GRAIN_ITEMS"']

    modified_model = model.dict()
    modified_model["query"] = (
        exp.select("*")
        .from_(model.query.subquery())
        .union(
            'SELECT key_1 as "key_1", KEY_2, value FROM (VALUES (1, 6, 1),(1, 5, 3),(NULL, 2, 3),) AS t (key_1, KEY_2, value)'
        )
    )

    modified_sqlmodel = SqlModel(**modified_model)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel)

    sushi_context_fixed_date.auto_categorize_changes = CategorizerConfig(
        sql=AutoCategorizationMode.FULL
    )
    sushi_context_fixed_date.plan(
        "target_dev",
        create_from="source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    diff = sushi_context_fixed_date.table_diff(
        source="source_dev",
        target="target_dev",
        on=["'key_1'", "key_2"],
        select_models={"memory.sushi*"},
        skip_grain_check=False,
    )[0]

    row_diff = diff.row_diff()
    assert row_diff.source_count == 7
    assert row_diff.target_count == 10
    assert row_diff.full_match_count == 7
    assert row_diff.partial_match_count == 0
    assert row_diff.s_only_count == 0
    assert row_diff.t_only_count == 3
    assert row_diff.full_match_pct == 82.35
    assert row_diff.partial_match_pct == 0
    assert row_diff.s_only_pct == 0
    assert row_diff.t_only_pct == 17.65
    assert row_diff.stats["join_count"] == 7
    assert (
        row_diff.stats["null_grain_count"] == 4
    )  # null grain currently (2025-07-24) means "any key column is null" as opposed to "all key columns are null"
    assert row_diff.stats["distinct_count_s"] == 7
    assert row_diff.stats["distinct_count_t"] == 10
    assert row_diff.stats["s_count"] == row_diff.stats["distinct_count_s"]
    assert row_diff.stats["t_count"] == row_diff.stats["distinct_count_t"]
    assert row_diff.s_sample.shape == (row_diff.s_only_count, 3)
    assert row_diff.t_sample.shape == (row_diff.t_only_count, 3)


def test_generated_sql(sushi_context_fixed_date: Context, mocker: MockerFixture):
    engine_adapter = sushi_context_fixed_date.engine_adapter

    engine_adapter.ctas(
        "table_diff_source",
        pd.DataFrame(
            {
                "key": [1, 2, 3],
                "value": [1.0, 4.2, 4.1],
                "ignored": [1, 2, 3],
            }
        ),
    )

    engine_adapter.ctas(
        "table_diff_target",
        pd.DataFrame(
            {
                "key": [1, 2, 6],
                "value": [1.0, 3.0, -2.2],
                "ignored": [1, 2, 3],
            }
        ),
    )

    query_sql = 'CREATE TABLE IF NOT EXISTS "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh" AS WITH "__source" AS (SELECT "s"."key", "s"."value", "s"."key" AS "__sqlmesh_join_key" FROM "table_diff_source" AS "s"), "__target" AS (SELECT "t"."key", "t"."value", "t"."key" AS "__sqlmesh_join_key" FROM "table_diff_target" AS "t"), "__stats" AS (SELECT "s"."key" AS "s__key", "s"."value" AS "s__value", "s"."__sqlmesh_join_key" AS "s____sqlmesh_join_key", "t"."key" AS "t__key", "t"."value" AS "t__value", "t"."__sqlmesh_join_key" AS "t____sqlmesh_join_key", CASE WHEN NOT "s"."__sqlmesh_join_key" IS NULL THEN 1 ELSE 0 END AS "s_exists", CASE WHEN NOT "t"."__sqlmesh_join_key" IS NULL THEN 1 ELSE 0 END AS "t_exists", CASE WHEN "s"."__sqlmesh_join_key" = "t"."__sqlmesh_join_key" THEN 1 ELSE 0 END AS "row_joined", CASE WHEN "s"."key" IS NULL AND "t"."key" IS NULL THEN 1 ELSE 0 END AS "null_grain", CASE WHEN "s"."key" = "t"."key" THEN 1 WHEN ("s"."key" IS NULL) AND ("t"."key" IS NULL) THEN 1 WHEN ("s"."key" IS NULL) OR ("t"."key" IS NULL) THEN 0 ELSE 0 END AS "key_matches", CASE WHEN CAST(CAST("s"."value" AS DOUBLE) AS DECIMAL(38, 3)) = CAST(CAST("t"."value" AS DOUBLE) AS DECIMAL(38, 3)) THEN 1 WHEN ("s"."value" IS NULL) AND ("t"."value" IS NULL) THEN 1 WHEN ("s"."value" IS NULL) OR ("t"."value" IS NULL) THEN 0 ELSE 0 END AS "value_matches" FROM "__source" AS "s" FULL JOIN "__target" AS "t" ON "s"."__sqlmesh_join_key" = "t"."__sqlmesh_join_key") SELECT *, CASE WHEN "key_matches" = 1 AND "value_matches" = 1 THEN 1 ELSE 0 END AS "row_full_match" FROM "__stats"'
    summary_query_sql = 'SELECT SUM("s_exists") AS "s_count", SUM("t_exists") AS "t_count", SUM("row_joined") AS "join_count", SUM("null_grain") AS "null_grain_count", SUM("row_full_match") AS "full_match_count", SUM("key_matches") AS "key_matches", SUM("value_matches") AS "value_matches", COUNT(DISTINCT ("s____sqlmesh_join_key")) AS "distinct_count_s", COUNT(DISTINCT ("t____sqlmesh_join_key")) AS "distinct_count_t" FROM "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh"'
    compare_sql = 'SELECT ROUND(100 * (CAST(SUM("key_matches") AS DECIMAL) / COUNT("key_matches")), 9) AS "key_matches", ROUND(100 * (CAST(SUM("value_matches") AS DECIMAL) / COUNT("value_matches")), 9) AS "value_matches" FROM "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh" WHERE "row_joined" = 1'
    sample_query_sql = 'WITH "source_only" AS (SELECT \'source_only\' AS "__sqlmesh_sample_type", "s__key", "s__value", "s____sqlmesh_join_key", "t__key", "t__value", "t____sqlmesh_join_key" FROM "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh" WHERE "s_exists" = 1 AND "row_joined" = 0 ORDER BY "s__key" NULLS FIRST LIMIT 20), "target_only" AS (SELECT \'target_only\' AS "__sqlmesh_sample_type", "s__key", "s__value", "s____sqlmesh_join_key", "t__key", "t__value", "t____sqlmesh_join_key" FROM "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh" WHERE "t_exists" = 1 AND "row_joined" = 0 ORDER BY "t__key" NULLS FIRST LIMIT 20), "common_rows" AS (SELECT \'common_rows\' AS "__sqlmesh_sample_type", "s__key", "s__value", "s____sqlmesh_join_key", "t__key", "t__value", "t____sqlmesh_join_key" FROM "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh" WHERE "row_joined" = 1 AND "row_full_match" = 0 ORDER BY "s__key" NULLS FIRST, "t__key" NULLS FIRST LIMIT 20) SELECT "__sqlmesh_sample_type", "s__key", "s__value", "s____sqlmesh_join_key", "t__key", "t__value", "t____sqlmesh_join_key" FROM "source_only" UNION ALL SELECT "__sqlmesh_sample_type", "s__key", "s__value", "s____sqlmesh_join_key", "t__key", "t__value", "t____sqlmesh_join_key" FROM "target_only" UNION ALL SELECT "__sqlmesh_sample_type", "s__key", "s__value", "s____sqlmesh_join_key", "t__key", "t__value", "t____sqlmesh_join_key" FROM "common_rows"'
    drop_sql = 'DROP TABLE IF EXISTS "memory"."sqlmesh_temp_test"."__temp_diff_abcdefgh"'

    # make with_settings() return the current instance of engine_adapter so we can still spy on _execute
    mocker.patch.object(
        engine_adapter, "with_settings", new_callable=lambda: lambda **kwargs: engine_adapter
    )
    assert engine_adapter.with_settings() == engine_adapter

    spy_execute = mocker.spy(engine_adapter, "_execute")
    mocker.patch("sqlmesh.core.engine_adapter.base.random_id", return_value="abcdefgh")

    sushi_context_fixed_date.table_diff(
        source="table_diff_source",
        target="table_diff_target",
        on=["key"],
        skip_columns=["ignored"],
        temp_schema="sqlmesh_temp_test",
    )

    spy_execute.assert_any_call(query_sql, False)
    spy_execute.assert_any_call(summary_query_sql, False)
    spy_execute.assert_any_call(compare_sql, False)
    spy_execute.assert_any_call(sample_query_sql, False)
    spy_execute.assert_any_call(drop_sql, False)

    spy_execute.reset_mock()

    # Also check WHERE clause is propagated correctly
    sushi_context_fixed_date.table_diff(
        source="table_diff_source",
        target="table_diff_target",
        on=["key"],
        skip_columns=["ignored"],
        where="key = 2",
    )

    query_sql_where = 'CREATE TABLE IF NOT EXISTS "memory"."sqlmesh_temp"."__temp_diff_abcdefgh" AS WITH "__source" AS (SELECT "s"."key", "s"."value", "s"."key" AS "__sqlmesh_join_key" FROM "table_diff_source" AS "s" WHERE "s"."key" = 2), "__target" AS (SELECT "t"."key", "t"."value", "t"."key" AS "__sqlmesh_join_key" FROM "table_diff_target" AS "t" WHERE "t"."key" = 2), "__stats" AS (SELECT "s"."key" AS "s__key", "s"."value" AS "s__value", "s"."__sqlmesh_join_key" AS "s____sqlmesh_join_key", "t"."key" AS "t__key", "t"."value" AS "t__value", "t"."__sqlmesh_join_key" AS "t____sqlmesh_join_key", CASE WHEN NOT "s"."__sqlmesh_join_key" IS NULL THEN 1 ELSE 0 END AS "s_exists", CASE WHEN NOT "t"."__sqlmesh_join_key" IS NULL THEN 1 ELSE 0 END AS "t_exists", CASE WHEN "s"."__sqlmesh_join_key" = "t"."__sqlmesh_join_key" THEN 1 ELSE 0 END AS "row_joined", CASE WHEN "s"."key" IS NULL AND "t"."key" IS NULL THEN 1 ELSE 0 END AS "null_grain", CASE WHEN "s"."key" = "t"."key" THEN 1 WHEN ("s"."key" IS NULL) AND ("t"."key" IS NULL) THEN 1 WHEN ("s"."key" IS NULL) OR ("t"."key" IS NULL) THEN 0 ELSE 0 END AS "key_matches", CASE WHEN CAST(CAST("s"."value" AS DOUBLE) AS DECIMAL(38, 3)) = CAST(CAST("t"."value" AS DOUBLE) AS DECIMAL(38, 3)) THEN 1 WHEN ("s"."value" IS NULL) AND ("t"."value" IS NULL) THEN 1 WHEN ("s"."value" IS NULL) OR ("t"."value" IS NULL) THEN 0 ELSE 0 END AS "value_matches" FROM "__source" AS "s" FULL JOIN "__target" AS "t" ON "s"."__sqlmesh_join_key" = "t"."__sqlmesh_join_key") SELECT *, CASE WHEN "key_matches" = 1 AND "value_matches" = 1 THEN 1 ELSE 0 END AS "row_full_match" FROM "__stats"'
    spy_execute.assert_any_call(query_sql_where, False)


def test_tables_and_grain_inferred_from_model(sushi_context_fixed_date: Context):
    (sushi_context_fixed_date.path / "models" / "waiter_revenue_by_day.sql").write_text("""
    MODEL (
        name sushi.waiter_revenue_by_day,
        kind incremental_by_time_range (
            time_column event_date,
            batch_size 10,
        ),
        owner jen,
        cron '@daily',
        audits (
            NUMBER_OF_ROWS(threshold := 0)
        ),
        grain (waiter_id, event_date)
    );

    SELECT
        o.waiter_id::INT + 1 AS waiter_id, /* Waiter id */
        SUM(oi.quantity * i.price)::DOUBLE AS revenue, /* Revenue from orders taken by this waiter */
        o.event_date::DATE AS event_date /* Date */
    FROM sushi.orders AS o
    LEFT JOIN sushi.order_items AS oi
        ON o.id = oi.order_id AND o.event_date = oi.event_date
    LEFT JOIN sushi.items AS i
        ON oi.item_id = i.id AND oi.event_date = i.event_date
    WHERE
        o.event_date BETWEEN @start_date AND @end_date
    GROUP BY
        o.waiter_id,
        o.event_date
""")
    # this creates a dev preview of "sushi.waiter_revenue_by_day"
    sushi_context_fixed_date.refresh()
    sushi_context_fixed_date.auto_categorize_changes = CategorizerConfig(
        sql=AutoCategorizationMode.FULL
    )
    sushi_context_fixed_date.plan(environment="unit_test", auto_apply=True, include_unmodified=True)

    table_diff = sushi_context_fixed_date.table_diff(
        source="unit_test", target="prod", select_models={"sushi.waiter_revenue_by_day"}
    )[0]
    assert isinstance(table_diff, TableDiff)
    assert table_diff.source == "memory.sushi__unit_test.waiter_revenue_by_day"
    assert table_diff.target == "memory.sushi.waiter_revenue_by_day"

    _, _, col_names = table_diff.key_columns
    assert col_names == ["waiter_id", "event_date"]


def test_data_diff_array_dict(sushi_context_fixed_date):
    engine_adapter = sushi_context_fixed_date.engine_adapter

    engine_adapter.ctas(
        "table_diff_source",
        pd.DataFrame(
            {
                "key": [1, 2, 3],
                "value": [np.array([51.2, 4.5678]), np.array([2.31, 12.2]), np.array([5.0])],
                "dict": [{"key1": 10, "key2": 20, "key3": 30}, {"key1": 10}, {}],
            }
        ),
    )

    engine_adapter.ctas(
        "table_diff_target",
        pd.DataFrame(
            {
                "key": [1, 2, 3],
                "value": [
                    np.array([51.2, 4.5679]),
                    np.array([2.31, 12.2, 3.6, 1.9]),
                    np.array([5.0]),
                ],
                "dict": [{"key1": 10, "key2": 13}, {"key1": 10}, {}],
            }
        ),
    )

    table_diff = TableDiff(
        adapter=engine_adapter,
        source="table_diff_source",
        target="table_diff_target",
        source_alias="dev",
        target_alias="prod",
        on=["key"],
        decimals=4,
    )

    diff = table_diff.row_diff()
    aliased_joined_sample = diff.joined_sample.columns

    assert "DEV__value" in aliased_joined_sample
    assert "PROD__value" in aliased_joined_sample
    assert diff.full_match_count == 1
    assert diff.partial_match_count == 2

    output = capture_console_output("show_row_diff", row_diff=diff)

    # Expected output with boxes
    expected_output = r"""
Row Counts:
├──  FULL MATCH: 1 rows (33.33%)
└──  PARTIAL MATCH: 2 rows (66.67%)

COMMON ROWS column comparison stats:
       pct_match
value  33.333333
dict   66.666667


COMMON ROWS sample data differences:
Column: value
┏━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ key ┃ DEV            ┃ PROD                   ┃
┡━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1   │ [51.2, 4.5678] │ [51.2, 4.5679]         │
│ 2   │ [2.31, 12.2]   │ [2.31, 12.2, 3.6, 1.9] │
└─────┴────────────────┴────────────────────────┘
Column: dict
┏━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ key ┃ DEV                         ┃ PROD               ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ 1   │ {key1=10, key2=20, key3=30} │ {key1=10, key2=13} │
└─────┴─────────────────────────────┴────────────────────┘
"""

    stripped_output = strip_ansi_codes(output)
    stripped_expected = expected_output.strip()
    assert stripped_output == stripped_expected


def test_data_diff_array_struct_query():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {"key": exp.DataType.build("int"), "value": exp.DataType.build("int")}

    engine_adapter.create_table("table_diff_source", columns_to_types)
    engine_adapter.create_table("table_diff_target", columns_to_types)

    engine_adapter.execute(
        "insert into table_diff_source (key, value) values (1, 1), (1, 2), (1, 3)"
    )
    engine_adapter.execute(
        "insert into table_diff_target (key, value) values (1, 1), (1, 3), (1, 2)"
    )

    engine_adapter.execute(
        "create view src_view as select key, array_agg(value) as val_arr, map(['k','v'], [10,11]) as val_map from table_diff_source group by 1"
    )
    engine_adapter.execute(
        "create view target_view as select key, array_agg(value) as val_arr, map(['k','v'],[11,10]) as val_map from table_diff_target group by 1"
    )

    table_diff = TableDiff(
        adapter=engine_adapter,
        source="src_view",
        target="target_view",
        source_alias="dev",
        target_alias="prod",
        on=["key"],
    )

    diff = table_diff.row_diff()

    output = capture_console_output("show_row_diff", row_diff=diff)

    assert (
        strip_ansi_codes(output)
        == """Row Counts:
└──  PARTIAL MATCH: 1 rows (100.0%)

COMMON ROWS column comparison stats:
         pct_match
val_arr        0.0
val_map        0.0


COMMON ROWS sample data differences:
Column: val_arr
┏━━━━━┳━━━━━━━━━┳━━━━━━━━━┓
┃ key ┃ DEV     ┃ PROD    ┃
┡━━━━━╇━━━━━━━━━╇━━━━━━━━━┩
│ 1   │ [1 2 3] │ [1 3 2] │
└─────┴─────────┴─────────┘
Column: val_map
┏━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ key ┃ DEV                ┃ PROD               ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ 1   │ {'k': 10, 'v': 11} │ {'k': 11, 'v': 10} │
└─────┴────────────────────┴────────────────────┘
""".strip()
    )


def test_data_diff_nullable_booleans():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {"key": exp.DataType.build("int"), "value": exp.DataType.build("boolean")}

    engine_adapter.create_table("table_diff_source", columns_to_types)
    engine_adapter.create_table("table_diff_target", columns_to_types)

    engine_adapter.execute(
        "insert into table_diff_source (key, value) values (1, true), (2, false), (3, null)"
    )
    engine_adapter.execute(
        "insert into table_diff_target (key, value) values (1, false), (2, null), (3, true)"
    )

    table_diff = TableDiff(
        adapter=engine_adapter,
        source="table_diff_source",
        target="table_diff_target",
        source_alias="dev",
        target_alias="prod",
        on=["key"],
    )

    diff = table_diff.row_diff()

    output = capture_console_output("show_row_diff", row_diff=diff)

    expected_output = """
Row Counts:
└──  PARTIAL MATCH: 3 rows (100.0%)

COMMON ROWS column comparison stats:
       pct_match
value        0.0


COMMON ROWS sample data differences:
Column: value
┏━━━━━┳━━━━━━━┳━━━━━━━┓
┃ key ┃ DEV   ┃ PROD  ┃
┡━━━━━╇━━━━━━━╇━━━━━━━┩
│ 1   │ True  │ False │
│ 2   │ False │ <NA>  │
│ 3   │ <NA>  │ True  │
└─────┴───────┴───────┘
"""

    assert strip_ansi_codes(output) == expected_output.strip()


def test_data_diff_multiple_models(sushi_context_fixed_date, capsys, caplog):
    # Create first analytics model
    expressions = d.parse(
        """
        MODEL (name memory.sushi.analytics_1, kind full, grain(key), tags (finance),);
        SELECT
            key,
            value,
        FROM
            (VALUES
                (1, 3),
                (2, 4),
            ) AS t (key, value)
    """
    )
    model_s = load_sql_based_model(expressions, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s)

    # Create second analytics model from analytics_1
    expressions_2 = d.parse(
        """
        MODEL (name memory.sushi.analytics_2, kind full, grain(key), tags (finance),);
        SELECT
            key,
            value as amount,
        FROM
            memory.sushi.analytics_1
    """
    )
    model_s2 = load_sql_based_model(expressions_2, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s2)

    sushi_context_fixed_date.plan(
        "source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    # Modify first model
    model = sushi_context_fixed_date.models['"MEMORY"."SUSHI"."ANALYTICS_1"']
    modified_model = model.dict()
    modified_model["query"] = (
        exp.select("*")
        .from_(model.query.subquery())
        .union("SELECT key, value FROM (VALUES (1, 6),(2,3),) AS t (key, value)")
    )
    modified_sqlmodel = SqlModel(**modified_model)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel)

    # Modify second model
    model2 = sushi_context_fixed_date.models['"MEMORY"."SUSHI"."ANALYTICS_2"']
    modified_model2 = model2.dict()
    modified_model2["query"] = (
        exp.select("*")
        .from_(model2.query.subquery())
        .union("SELECT key, amount FROM (VALUES (5, 150.2),(6,250.2),) AS t (key, amount)")
    )
    modified_sqlmodel2 = SqlModel(**modified_model2)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel2)

    sushi_context_fixed_date.auto_categorize_changes = CategorizerConfig(
        sql=AutoCategorizationMode.FULL
    )
    sushi_context_fixed_date.plan(
        "target_dev",
        create_from="source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    # Get diffs for both models
    selector = {"tag:finance & memory.sushi.analytics*"}
    diffs = sushi_context_fixed_date.table_diff(
        source="source_dev",
        target="target_dev",
        on=["key"],
        select_models=selector,
        skip_grain_check=False,
    )

    assert len(diffs) == 2

    # Check analytics_1 diff
    diff1 = next(d for d in diffs if "ANALYTICS_1" in d.source)
    row_diff1 = diff1.row_diff()
    assert row_diff1.full_match_count == 2
    assert row_diff1.full_match_pct == 50.0
    assert row_diff1.s_only_count == 0
    assert row_diff1.t_only_count == 0
    assert row_diff1.stats["join_count"] == 4
    assert row_diff1.stats["null_grain_count"] == 0
    assert row_diff1.stats["s_count"] == 4
    assert row_diff1.stats["distinct_count_s"] == 2
    assert row_diff1.stats["t_count"] == 4
    assert row_diff1.stats["distinct_count_t"] == 2
    assert row_diff1.s_sample.shape == (0, 2)
    assert row_diff1.t_sample.shape == (0, 2)

    # Check analytics_2 diff
    diff2 = next(d for d in diffs if "ANALYTICS_2" in d.source)
    row_diff2 = diff2.row_diff()
    assert row_diff2.full_match_count == 2
    assert row_diff2.full_match_pct == 40.0
    assert row_diff2.s_only_count == 0
    assert row_diff2.t_only_count == 2
    assert row_diff2.stats["join_count"] == 4
    assert row_diff2.stats["null_grain_count"] == 0
    assert row_diff2.stats["s_count"] == 4
    assert row_diff2.stats["distinct_count_s"] == 2
    assert row_diff2.stats["t_count"] == 6
    assert row_diff2.stats["distinct_count_t"] == 4
    assert row_diff2.s_sample.shape == (0, 2)
    assert row_diff2.t_sample.shape == (2, 2)

    # This selector shouldn't return any diffs since both models have this tag
    selector = {"^tag:finance"}
    diffs = sushi_context_fixed_date.table_diff(
        source="source_dev",
        target="target_dev",
        on=["key"],
        select_models=selector,
        skip_grain_check=False,
    )
    assert len(diffs) == 0


def test_data_diff_forward_only(sushi_context_fixed_date, capsys, caplog):
    expressions = d.parse(
        """
        MODEL (name memory.sushi.full_1, kind full, grain(key),);
        SELECT
            key,
            value,
        FROM
            (VALUES
                (1, 3),
                (2, 4),
            ) AS t (key, value)
    """
    )
    model_s = load_sql_based_model(expressions, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s)

    # Create second analytics model sourcing from first
    expressions_2 = d.parse(
        """
        MODEL (name memory.sushi.full_2, kind full, grain(key),);
        SELECT
            key,
            value as amount,
        FROM
            memory.sushi.full_1
    """
    )
    model_s2 = load_sql_based_model(expressions_2, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s2)

    sushi_context_fixed_date.plan(
        "target_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    model = sushi_context_fixed_date.models['"MEMORY"."SUSHI"."FULL_1"']
    modified_model = model.dict()
    modified_model["query"] = exp.select("*").from_("(VALUES (12, 6),(5,3),) AS t (key, value)")
    modified_sqlmodel = SqlModel(**modified_model)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel)

    sushi_context_fixed_date.auto_categorize_changes = CategorizerConfig(
        sql=AutoCategorizationMode.FULL
    )

    plan_builder = sushi_context_fixed_date.plan_builder(
        "source_dev", skip_tests=True, forward_only=True
    )
    plan = plan_builder.build()

    sushi_context_fixed_date.apply(plan)

    # Get diffs for both models
    selector = {"*full*"}
    diffs = sushi_context_fixed_date.table_diff(
        source="source_dev",
        target="target_dev",
        on=["key"],
        select_models=selector,
        skip_grain_check=False,
    )

    # Both models should be diffed
    assert len(diffs) == 2

    # Check full_1 diff
    diff1 = next(d for d in diffs if "FULL_1" in d.source)
    row_diff1 = diff1.row_diff()
    diff2 = next(d for d in diffs if "FULL_2" in d.source)
    row_diff2 = diff2.row_diff()

    # Both diffs should show the same matches
    for row_diff in [row_diff1, row_diff2]:
        assert row_diff.full_match_count == 0
        assert row_diff.full_match_pct == 0.0
        assert row_diff.s_only_count == 2
        assert row_diff.t_only_count == 2
        assert row_diff.stats["join_count"] == 0
        assert row_diff.stats["null_grain_count"] == 0
        assert row_diff.stats["s_count"] == 2
        assert row_diff.stats["distinct_count_s"] == 2
        assert row_diff.stats["t_count"] == 2
        assert row_diff.stats["distinct_count_t"] == 2
        assert row_diff.s_sample.shape == (2, 2)
        assert row_diff.t_sample.shape == (2, 2)


def test_data_diff_empty_tables():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types_src = {
        "key": exp.DataType.build("int"),
        "value": exp.DataType.build("varchar"),
    }
    columns_to_types_target = {
        "key": exp.DataType.build("int"),
        "value2": exp.DataType.build("varchar"),
    }

    engine_adapter.create_table("table_diff_source", columns_to_types_src)
    engine_adapter.create_table("table_diff_target", columns_to_types_target)

    table_diff = TableDiff(
        adapter=engine_adapter,
        source="table_diff_source",
        target="table_diff_target",
        source_alias="dev",
        target_alias="prod",
        on=["key"],
    )

    # should show the schema diff
    schema_diff = table_diff.schema_diff()
    assert len(schema_diff.added) == 1
    assert schema_diff.added[0][0] == "value2"
    assert len(schema_diff.removed) == 1
    assert schema_diff.removed[0][0] == "value"

    # should not error on the row diff
    row_diff = table_diff.row_diff()
    assert row_diff.empty

    output = capture_console_output("show_row_diff", row_diff=row_diff)
    assert (
        strip_ansi_codes(output) == "Neither the source nor the target table contained any records"
    )


@pytest.mark.slow
def test_data_diff_multiple_models_lacking_grain(sushi_context_fixed_date, capsys, caplog):
    # Create first model with grain
    expressions = d.parse(
        """
        MODEL (name memory.sushi.grain_model, kind full, grain(key),);
        SELECT
            key,
            value,
        FROM
            (VALUES
                (1, 3),
                (2, 4),
            ) AS t (key, value)
    """
    )
    model_s = load_sql_based_model(expressions, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s)

    # Create second model without grain
    expressions_2 = d.parse(
        """
        MODEL (name memory.sushi.no_grain_model, kind full,);
        SELECT
            key,
            value as amount,
        FROM
            memory.sushi.grain_model
    """
    )
    model_s2 = load_sql_based_model(expressions_2, dialect="snowflake")
    sushi_context_fixed_date.upsert_model(model_s2)

    sushi_context_fixed_date.plan(
        "source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    # Modify first model
    model = sushi_context_fixed_date.models['"MEMORY"."SUSHI"."GRAIN_MODEL"']
    modified_model = model.dict()
    modified_model["query"] = (
        exp.select("*")
        .from_(model.query.subquery())
        .union("SELECT key, value FROM (VALUES (1, 6),(2,3),) AS t (key, value)")
    )
    modified_sqlmodel = SqlModel(**modified_model)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel)

    # Modify second model
    model2 = sushi_context_fixed_date.models['"MEMORY"."SUSHI"."NO_GRAIN_MODEL"']
    modified_model2 = model2.dict()
    modified_model2["query"] = (
        exp.select("*")
        .from_(model2.query.subquery())
        .union("SELECT key, amount FROM (VALUES (5, 150.2),(6,250.2),) AS t (key, amount)")
    )
    modified_sqlmodel2 = SqlModel(**modified_model2)
    sushi_context_fixed_date.upsert_model(modified_sqlmodel2)

    sushi_context_fixed_date.auto_categorize_changes = CategorizerConfig(
        sql=AutoCategorizationMode.FULL
    )
    sushi_context_fixed_date.plan(
        "target_dev",
        create_from="source_dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        start="2023-01-31",
        end="2023-01-31",
    )

    # By default erroring out when even one model lacks a grain
    with pytest.raises(
        SQLMeshError,
        match=r"SQLMesh doesn't know how to join the tables for the following models:*",
    ):
        sushi_context_fixed_date.table_diff(
            source="source_dev",
            target="target_dev",
            select_models={"*"},
            skip_grain_check=False,
        )

    # With warn_grain_check flag the diff will go ahead by warning
    diffs = sushi_context_fixed_date.table_diff(
        source="source_dev",
        target="target_dev",
        select_models={"*"},
        skip_grain_check=False,
        warn_grain_check=True,
    )

    # Check that the diff was performed only for the model with a grain
    assert len(diffs) == 1
    diff1 = diffs[0]

    # Check the table diff corresponds to the grain model
    row_diff1 = diff1.row_diff()
    assert row_diff1.full_match_count == 2.0
    assert row_diff1.full_match_pct == 50.0
    assert row_diff1.s_only_count == 0.0
    assert row_diff1.t_only_count == 0.0
    assert row_diff1.stats["join_count"] == 4.0
    assert row_diff1.stats["null_grain_count"] == 0.0
    assert row_diff1.stats["s_count"] == 4.0
    assert row_diff1.stats["distinct_count_s"] == 2.0
    assert row_diff1.stats["t_count"] == 4.0
    assert row_diff1.stats["distinct_count_t"] == 2.0
    assert row_diff1.s_sample.shape == (0, 2)
    assert row_diff1.t_sample.shape == (0, 2)
    assert row_diff1.joined_sample.shape == (2, 3)
    assert row_diff1.sample.shape == (2, 4)


def test_schema_diff_ignore_case():
    # no changes
    table_a = {"COL_A": exp.DataType.build("varchar"), "cOl_b": exp.DataType.build("int")}
    table_b = {"col_a": exp.DataType.build("varchar"), "COL_b": exp.DataType.build("int")}

    diff = SchemaDiff(
        source="table_a",
        source_schema=table_a,
        target="table_b",
        target_schema=table_b,
        ignore_case=True,
    )

    assert not diff.has_changes

    # added in target
    table_a = {"COL_A": exp.DataType.build("varchar"), "cOl_b": exp.DataType.build("int")}
    table_b = {
        "col_a": exp.DataType.build("varchar"),
        "COL_b": exp.DataType.build("int"),
        "cOL__C": exp.DataType.build("date"),
    }

    diff = SchemaDiff(
        source="table_a",
        source_schema=table_a,
        target="table_b",
        target_schema=table_b,
        ignore_case=True,
    )

    assert diff.has_changes
    assert len(diff.added) == 1
    assert diff.added[0] == (
        "cOL__C",
        exp.DataType.build("date"),
    )  # notice: case preserved on output
    assert not diff.removed
    assert not diff.modified

    # removed from source
    table_a = {
        "cOL_fo0": exp.DataType.build("float"),
        "COL_A": exp.DataType.build("varchar"),
        "cOl_b": exp.DataType.build("int"),
    }
    table_b = {"col_a": exp.DataType.build("varchar"), "COL_b": exp.DataType.build("int")}

    diff = SchemaDiff(
        source="table_a",
        source_schema=table_a,
        target="table_b",
        target_schema=table_b,
        ignore_case=True,
    )

    assert diff.has_changes
    assert not diff.added
    assert len(diff.removed) == 1
    assert diff.removed[0] == (
        "cOL_fo0",
        exp.DataType.build("float"),
    )  # notice: case preserved on output
    assert not diff.modified

    # column type change
    table_a = {"CoL_A": exp.DataType.build("varchar"), "cOl_b": exp.DataType.build("int")}
    table_b = {"col_a": exp.DataType.build("date"), "COL_b": exp.DataType.build("int")}

    diff = SchemaDiff(
        source="table_a",
        source_schema=table_a,
        target="table_b",
        target_schema=table_b,
        ignore_case=True,
    )

    assert diff.has_changes
    assert not diff.added
    assert not diff.removed
    assert diff.modified == {
        "CoL_A": (
            exp.DataType.build("varchar"),
            exp.DataType.build("date"),
        )  # notice: source casing used on output
    }


def test_data_diff_sample_limit():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {"id": exp.DataType.build("int"), "name": exp.DataType.build("varchar")}

    engine_adapter.create_table("src", columns_to_types)
    engine_adapter.create_table("target", columns_to_types)

    common_records = {}
    src_only_records = {}
    target_only_records = {}

    for i in range(0, 10):
        common_records[i] = f"common_{i}"
        src_only_records[i + 20] = f"src_{i}"
        target_only_records[i + 40] = f"target_{i}"

    src_records = {**common_records, **src_only_records}
    target_records = {**common_records, **target_only_records}

    # changes
    src_records[1] = "modified_source_1"
    src_records[3] = "modified_source_3"
    target_records[2] = "modified_target_2"
    target_records[7] = "modified_target_7"

    src_df = pd.DataFrame.from_records([{"id": k, "name": v} for k, v in src_records.items()])
    target_df = pd.DataFrame.from_records([{"id": k, "name": v} for k, v in target_records.items()])

    engine_adapter.insert_append("src", src_df)
    engine_adapter.insert_append("target", target_df)

    table_diff = TableDiff(
        adapter=engine_adapter, source="src", target="target", on=["id"], limit=3
    )

    diff = table_diff.row_diff()

    assert diff.s_only_count == 10
    assert diff.t_only_count == 10
    assert diff.join_count == 10

    # each sample should contain :limit records
    assert len(diff.s_sample) == 3
    assert len(diff.t_sample) == 3
    assert len(diff.joined_sample) == 3


def test_data_diff_non_lowercase_key_columns():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {
        "KEY1": exp.DataType.build("int"),
        "Key2": exp.DataType.build("varchar"),
        "VALUE": exp.DataType.build("varchar"),
    }

    engine_adapter.create_table("src", columns_to_types)
    engine_adapter.create_table("target", columns_to_types)

    src_records = [
        (1, "a", "value"),
        (2, "b", "source"),
        (3, "c", "source only"),
    ]

    target_records = [
        (1, "a", "value"),
        (2, "b", "target"),
        (4, "d", "target only"),
    ]

    src_df = pd.DataFrame(data=src_records, columns=columns_to_types.keys())
    target_df = pd.DataFrame(data=target_records, columns=columns_to_types.keys())

    engine_adapter.insert_append("src", src_df)
    engine_adapter.insert_append("target", target_df)

    # casing of the supplied key should not matter
    for on in (["KEY1", "Key2"], ["key1", "KEY2"]):
        table_diff = TableDiff(adapter=engine_adapter, source="src", target="target", on=on)

        _, _, col_names = table_diff.key_columns
        assert col_names == ["KEY1", "Key2"]

        diff = table_diff.row_diff()

        assert diff.join_count == 2
        assert diff.full_match_count == 1
        assert diff.partial_match_count == 1
        assert diff.s_only_count == 1
        assert diff.t_only_count == 1

    table_diff = TableDiff(adapter=engine_adapter, source="src", target="target", on=["KEY1"])

    _, _, col_names = table_diff.key_columns
    assert col_names == ["KEY1"]

    diff = table_diff.row_diff()

    assert diff.join_count == 2
    assert diff.full_match_count == 1
    assert diff.partial_match_count == 1
    assert diff.s_only_count == 1
    assert diff.t_only_count == 1


def test_data_diff_non_lowercase_skip_columns():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {
        "KEY": exp.DataType.build("int"),
        "VALUE": exp.DataType.build("varchar"),
        "IGNORED": exp.DataType.build("varchar"),
    }

    engine_adapter.create_table("src", columns_to_types)
    engine_adapter.create_table("target", columns_to_types)
    engine_adapter.insert_append(
        "src",
        pd.DataFrame([(1, "same", "source")], columns=columns_to_types),
    )
    engine_adapter.insert_append(
        "target",
        pd.DataFrame([(1, "same", "target")], columns=columns_to_types),
    )

    diff = TableDiff(
        adapter=engine_adapter,
        source="src",
        target="target",
        on=["key"],
        skip_columns=["ignored"],
    ).row_diff()

    assert diff.full_match_count == 1
    assert diff.partial_match_count == 0
    assert diff.column_stats.index.tolist() == ["VALUE"]


def test_skip_columns_across_schemas():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()
    table_diff = TableDiff(
        adapter=engine_adapter,
        source="src",
        target="target",
        on=["key"],
        skip_columns=["foo"],
    )
    data_type = exp.DataType.build("varchar")
    table_diff.__dict__["source_schema"] = {"foo": data_type, "FOO": data_type}
    table_diff.__dict__["target_schema"] = {"FOO": data_type}

    source_skip_columns = {
        table_diff._resolve_column_name(c, table_diff.source_schema)
        for c in table_diff.skip_columns
    }
    target_skip_columns = {
        table_diff._resolve_column_name(c, table_diff.target_schema)
        for c in table_diff.skip_columns
    }

    assert source_skip_columns == {"foo"}
    assert target_skip_columns == {"FOO"}
    assert "FOO" not in source_skip_columns


def test_data_diff_key_columns_with_differing_case_between_source_and_target():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    source_columns_to_types = {
        "KEY1": exp.DataType.build("int"),
        "Key2": exp.DataType.build("varchar"),
        "value": exp.DataType.build("varchar"),
    }
    target_columns_to_types = {
        "key1": exp.DataType.build("int"),
        "KEY2": exp.DataType.build("varchar"),
        "value": exp.DataType.build("varchar"),
    }

    engine_adapter.create_table("src", source_columns_to_types)
    engine_adapter.create_table("target", target_columns_to_types)

    engine_adapter.insert_append(
        "src",
        pd.DataFrame(
            data=[(1, "a", "value"), (2, "b", "source")],
            columns=source_columns_to_types.keys(),
        ),
    )
    engine_adapter.insert_append(
        "target",
        pd.DataFrame(
            data=[(1, "a", "value"), (2, "b", "target")],
            columns=target_columns_to_types.keys(),
        ),
    )

    table_diff = TableDiff(
        adapter=engine_adapter, source="src", target="target", on=["key1", "KEY2"]
    )

    s_index, t_index, col_names = table_diff.key_columns
    assert [c.sql() for c in s_index] == ["s.KEY1", "s.Key2"]
    assert [c.sql() for c in t_index] == ["t.key1", "t.KEY2"]
    assert col_names == ["KEY1", "Key2"]

    diff = table_diff.row_diff()

    assert diff.join_count == 2
    assert diff.full_match_count == 1
    assert diff.partial_match_count == 1
    assert diff.s_only_count == 0
    assert diff.t_only_count == 0

    # the key columns are excluded from the per column match stats
    assert diff.column_stats.index.tolist() == ["value"]


def test_data_diff_non_lowercase_key_columns_in_on_condition():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {
        "KEY1": exp.DataType.build("int"),
        "Key2": exp.DataType.build("varchar"),
        "VALUE": exp.DataType.build("varchar"),
    }

    engine_adapter.create_table("src", columns_to_types)
    engine_adapter.create_table("target", columns_to_types)

    src_df = pd.DataFrame(
        data=[(1, "a", "value"), (2, "b", "source")], columns=columns_to_types.keys()
    )
    target_df = pd.DataFrame(
        data=[(1, "a", "value"), (2, "b", "target")], columns=columns_to_types.keys()
    )

    engine_adapter.insert_append("src", src_df)
    engine_adapter.insert_append("target", target_df)

    table_diff = TableDiff(
        adapter=engine_adapter,
        source="src",
        target="target",
        on=exp.condition('s."KEY1" = t."KEY1" AND s."Key2" = t."Key2"'),
    )

    _, _, col_names = table_diff.key_columns
    assert col_names == ["KEY1", "Key2"]

    diff = table_diff.row_diff()

    assert diff.join_count == 2
    assert diff.full_match_count == 1
    assert diff.partial_match_count == 1
    assert diff.s_only_count == 0
    assert diff.t_only_count == 0


def test_data_diff_nulls_in_some_grain_columns():
    engine_adapter = DuckDBConnectionConfig().create_engine_adapter()

    columns_to_types = {
        "key1": exp.DataType.build("int"),
        "key2": exp.DataType.build("varchar"),
        "key3": exp.DataType.build("int"),
        "value": exp.DataType.build("varchar"),
    }

    engine_adapter.create_table("src", columns_to_types)
    engine_adapter.create_table("target", columns_to_types)

    src_records = [
        (1, None, 1, "value"),  # full match
        (None, None, None, "null value"),  # join, partial match
        (2, None, None, "source only"),  # source only
    ]

    target_records = [
        (1, None, 1, "value"),  # full match
        (None, None, None, "null value modified"),  # join, partial match
        (None, "three", 2, "target only"),  # target only
    ]

    src_df = pd.DataFrame(data=src_records, columns=columns_to_types.keys())
    target_df = pd.DataFrame(data=target_records, columns=columns_to_types.keys())

    engine_adapter.insert_append("src", src_df)
    engine_adapter.insert_append("target", target_df)

    table_diff = TableDiff(
        adapter=engine_adapter, source="src", target="target", on=["key1", "key2", "key3"]
    )

    diff = table_diff.row_diff()

    assert diff.join_count == 2
    assert diff.s_only_count == 1
    assert diff.t_only_count == 1
    assert diff.full_match_count == 1
    assert diff.partial_match_count == 1

    assert diff.s_sample["value"].tolist() == ["source only"]
    assert diff.t_sample["value"].tolist() == ["target only"]
    assert diff.joined_sample[["s__value", "t__value"]].values.flatten().tolist() == [
        "null value",
        "null value modified",
    ]
