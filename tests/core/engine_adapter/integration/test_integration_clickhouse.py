import typing as t
import pytest
from pytest import FixtureRequest
from tests.core.engine_adapter.integration import TestContext
from sqlmesh.core.engine_adapter.clickhouse import ClickhouseEngineAdapter
import pandas as pd  # noqa: TID253
from sqlglot import exp, parse_one
from sqlmesh.core.snapshot import SnapshotChangeCategory

from tests.core.engine_adapter.integration import (
    TestContext,
    generate_pytest_params,
    ENGINES_BY_NAME,
    IntegrationTestEngine,
)


@pytest.fixture(
    params=list(
        generate_pytest_params([ENGINES_BY_NAME["clickhouse"], ENGINES_BY_NAME["clickhouse_cloud"]])
    )
)
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture
def engine_adapter(ctx: TestContext) -> ClickhouseEngineAdapter:
    assert isinstance(ctx.engine_adapter, ClickhouseEngineAdapter)
    return ctx.engine_adapter


def _get_source_queries_and_columns_to_types(
    ctx,
    insert_table: exp.Table,
    target_table: exp.Table,
    columns_to_types: t.Dict[str, exp.DataType] = {
        "id": exp.DataType.build("Int8", dialect="clickhouse"),
        "ds": exp.DataType.build("Date", dialect="clickhouse"),
    },
):
    return ctx.engine_adapter._get_source_queries_and_columns_to_types(
        parse_one(f"SELECT * FROM {insert_table.sql()}"),  # type: ignore
        columns_to_types,
        target_table=target_table.sql(),
    )


def _create_table_and_insert_existing_data(
    ctx: TestContext,
    existing_data: pd.DataFrame = pd.DataFrame(
        [
            {"id": 1, "ds": "2024-01-01"},
            {"id": 2, "ds": "2024-02-01"},
            {"id": 3, "ds": "2024-02-28"},
            {"id": 4, "ds": "2024-03-01"},
        ]
    ),
    columns_to_types: t.Dict[str, exp.DataType] = {
        "id": exp.DataType.build("Int8", "clickhouse"),
        "ds": exp.DataType.build("Date", "clickhouse"),
    },
    table_name: str = "data_existing",
    partitioned_by: t.Optional[t.List[exp.Expr]] = [parse_one("toMonth(ds)", dialect="clickhouse")],
) -> exp.Table:
    existing_data = existing_data
    existing_table_name: exp.Table = ctx.table(table_name)
    ctx.engine_adapter.ctas(
        existing_table_name.sql(),
        ctx.input_data(existing_data, columns_to_types),
        columns_to_types,
        partitioned_by=partitioned_by,
    )
    return existing_table_name


def test_insert_overwrite_by_condition_replace_partitioned(ctx: TestContext):
    existing_table_name = _create_table_and_insert_existing_data(ctx)

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 5, "ds": "2024-02-29"},
            {"id": 6, "ds": "2024-04-01"},
        ]
    )
    insert_table_name = ctx.table("data_insert")

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), insert_table_name.sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )

        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
        )

    ctx.compare_with_current(
        existing_table_name,
        pd.DataFrame(
            [
                {"id": 5, "ds": pd.Timestamp("2024-02-29")},
                {"id": 6, "ds": pd.Timestamp("2024-04-01")},
            ]
        ),
    )


def test_insert_overwrite_by_condition_replace(ctx: TestContext):
    existing_table_name = _create_table_and_insert_existing_data(ctx, partitioned_by=None)

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 5, "ds": "2024-02-29"},
            {"id": 6, "ds": "2024-04-01"},
        ]
    )
    insert_table_name = ctx.table("data_insert")

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), insert_table_name.sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )

        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
        )

    ctx.compare_with_current(
        existing_table_name,
        pd.DataFrame(
            [
                {"id": 5, "ds": pd.Timestamp("2024-02-29")},
                {"id": 6, "ds": pd.Timestamp("2024-04-01")},
            ]
        ),
    )


def test_insert_overwrite_by_condition_where_partitioned(ctx: TestContext):
    # `where` time window
    start_date = "2024-02-15"
    end_date = "2024-04-30"

    # data currently in target table
    existing_table_name = _create_table_and_insert_existing_data(ctx)

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 5, "ds": "2024-02-29"},
            {"id": 6, "ds": "2024-04-01"},
        ]
    )
    insert_table_name = ctx.table("data_insert")

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), insert_table_name.sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            exp.Between(
                this=exp.column("ds"),
                low=parse_one(f"'{start_date}'"),
                high=parse_one(f"'{end_date}'"),
            ),
        )

    ctx.compare_with_current(
        existing_table_name,
        pd.DataFrame(
            [
                {"id": 1, "ds": pd.Timestamp("2024-01-01")},  # retained
                {"id": 2, "ds": pd.Timestamp("2024-02-01")},  # retained
                {"id": 5, "ds": pd.Timestamp("2024-02-29")},  # inserted
                {"id": 6, "ds": pd.Timestamp("2024-04-01")},  # inserted
            ]
        ),
    )


def test_insert_overwrite_by_condition_where_compound_partitioned(ctx: TestContext):
    # `where` time window
    start_date = "2024-02-15"
    end_date = "2024-04-30"

    compound_columns_to_types = {
        "id": exp.DataType.build("Int8", ctx.dialect),
        "ds": exp.DataType.build("Date", ctx.dialect),
        "city": exp.DataType.build("String", ctx.dialect),
    }

    # data currently in target table
    existing_table_name = _create_table_and_insert_existing_data(
        ctx,
        existing_data=pd.DataFrame(
            [
                {"id": 1, "ds": "2024-01-01", "city": "1"},
                {"id": 2, "ds": "2024-01-02", "city": "2"},
                {"id": 3, "ds": "2024-02-01", "city": "1"},
                {"id": 4, "ds": "2024-02-02", "city": "2"},
                {"id": 5, "ds": "2024-02-27", "city": "1"},
                {"id": 6, "ds": "2024-02-28", "city": "2"},
                {"id": 7, "ds": "2024-03-01", "city": "1"},
                {"id": 8, "ds": "2024-03-02", "city": "2"},
            ]
        ),
        columns_to_types=compound_columns_to_types,
        partitioned_by=[parse_one("toMonth(ds)", dialect=ctx.dialect), exp.column("city")],
    )

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 9, "ds": "2024-02-26", "city": "1"},
            {"id": 10, "ds": "2024-02-29", "city": "2"},
            {"id": 11, "ds": "2024-04-01", "city": "1"},
            {"id": 12, "ds": "2024-04-02", "city": "2"},
        ]
    )
    insert_table_name = ctx.table("data_insert")

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data, compound_columns_to_types), insert_table_name.sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx,
            insert_table,
            existing_table_name,
            compound_columns_to_types,
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            exp.Between(
                this=exp.column("ds"),
                low=parse_one(f"'{start_date}'"),
                high=parse_one(f"'{end_date}'"),
            ),
        )

    ctx.compare_with_current(
        existing_table_name,
        pd.DataFrame(
            [
                {"id": 1, "ds": pd.Timestamp("2024-01-01"), "city": "1"},
                {"id": 2, "ds": pd.Timestamp("2024-01-02"), "city": "2"},
                {"id": 3, "ds": pd.Timestamp("2024-02-01"), "city": "1"},
                {"id": 4, "ds": pd.Timestamp("2024-02-02"), "city": "2"},
                {"id": 9, "ds": pd.Timestamp("2024-02-26"), "city": "1"},
                {"id": 10, "ds": pd.Timestamp("2024-02-29"), "city": "2"},
                {"id": 11, "ds": pd.Timestamp("2024-04-01"), "city": "1"},
                {"id": 12, "ds": pd.Timestamp("2024-04-02"), "city": "2"},
            ]
        ),
    )


def test_insert_overwrite_by_condition_by_key(ctx: TestContext):
    # key parameters
    key = [exp.column("id")]
    key_exp = key[0]

    # data currently in target table
    existing_table_name = _create_table_and_insert_existing_data(ctx, partitioned_by=None)

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 4, "ds": "2024-05-01"},  # will overwrite existing record
            {"id": 4, "ds": "2024-05-02"},  # only inserted if unique_key = False
            {"id": 5, "ds": "2024-02-29"},
            {"id": 6, "ds": "2024-04-01"},
        ]
    )

    # unique_key = True
    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), ctx.table("data_insert").sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            dynamic_key=key,
            dynamic_key_exp=key_exp,
            dynamic_key_unique=True,
        )

        ctx.compare_with_current(
            existing_table_name,
            pd.DataFrame(
                [
                    {"id": 1, "ds": pd.Timestamp("2024-01-01")},
                    {"id": 2, "ds": pd.Timestamp("2024-02-01")},
                    {"id": 3, "ds": pd.Timestamp("2024-02-28")},
                    {"id": 4, "ds": pd.Timestamp("2024-05-01")},
                    {"id": 5, "ds": pd.Timestamp("2024-02-29")},
                    {"id": 6, "ds": pd.Timestamp("2024-04-01")},
                ]
            ),
        )
    ctx.engine_adapter.drop_table(existing_table_name.sql())

    # unique_key = False
    existing_table_name = _create_table_and_insert_existing_data(
        ctx, table_name="data_existing_no_unique", partitioned_by=None
    )

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), ctx.table("data_insert_no_unique").sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            dynamic_key=key,
            dynamic_key_exp=key_exp,
            dynamic_key_unique=False,
        )

        ctx.compare_with_current(
            existing_table_name,
            pd.DataFrame(
                [
                    {"id": 1, "ds": pd.Timestamp("2024-01-01")},
                    {"id": 2, "ds": pd.Timestamp("2024-02-01")},
                    {"id": 3, "ds": pd.Timestamp("2024-02-28")},
                    {"id": 4, "ds": pd.Timestamp("2024-05-01")},
                    {"id": 4, "ds": pd.Timestamp("2024-05-02")},
                    {"id": 5, "ds": pd.Timestamp("2024-02-29")},
                    {"id": 6, "ds": pd.Timestamp("2024-04-01")},
                ]
            ),
        )


def test_insert_overwrite_by_condition_by_key_partitioned(ctx: TestContext):
    # key parameters
    key = [exp.column("id")]
    key_exp = key[0]

    # data currently in target table
    existing_table_name = _create_table_and_insert_existing_data(ctx)

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 4, "ds": "2024-05-01"},  # will overwrite existing record
            {"id": 4, "ds": "2024-05-02"},  # only inserted if unique_key = False
            {"id": 5, "ds": "2024-02-29"},
            {"id": 6, "ds": "2024-04-01"},
        ]
    )

    # unique_key = True
    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), ctx.table("data_insert").sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            dynamic_key=key,
            dynamic_key_exp=key_exp,
            dynamic_key_unique=True,
        )

        ctx.compare_with_current(
            existing_table_name,
            pd.DataFrame(
                [
                    {"id": 1, "ds": pd.Timestamp("2024-01-01")},
                    {"id": 2, "ds": pd.Timestamp("2024-02-01")},
                    {"id": 3, "ds": pd.Timestamp("2024-02-28")},
                    {"id": 4, "ds": pd.Timestamp("2024-05-01")},
                    {"id": 5, "ds": pd.Timestamp("2024-02-29")},
                    {"id": 6, "ds": pd.Timestamp("2024-04-01")},
                ]
            ),
        )
    ctx.engine_adapter.drop_table(existing_table_name.sql())

    # unique_key = False
    existing_table_name = _create_table_and_insert_existing_data(
        ctx, table_name="data_existing_no_unique"
    )

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), ctx.table("data_insert_no_unique").sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            dynamic_key=key,
            dynamic_key_exp=key_exp,
            dynamic_key_unique=False,
        )

        ctx.compare_with_current(
            existing_table_name,
            pd.DataFrame(
                [
                    {"id": 1, "ds": pd.Timestamp("2024-01-01")},
                    {"id": 2, "ds": pd.Timestamp("2024-02-01")},
                    {"id": 3, "ds": pd.Timestamp("2024-02-28")},
                    {"id": 4, "ds": pd.Timestamp("2024-05-01")},
                    {
                        "id": 4,
                        "ds": pd.Timestamp("2024-05-02"),
                    },  # second ID=4 row because unique_key=False
                    {"id": 5, "ds": pd.Timestamp("2024-02-29")},
                    {"id": 6, "ds": pd.Timestamp("2024-04-01")},
                ]
            ),
        )


def test_insert_overwrite_by_condition_inc_by_partition(ctx: TestContext):
    existing_table_name = _create_table_and_insert_existing_data(ctx)

    # new data to insert
    insert_data = pd.DataFrame(
        [
            {"id": 5, "ds": "2024-02-29"},
            {"id": 6, "ds": "2024-04-01"},
        ]
    )
    insert_table_name = ctx.table("data_insert")

    with ctx.engine_adapter.temp_table(
        ctx.input_data(insert_data), insert_table_name.sql()
    ) as insert_table:
        source_queries, columns_to_types = _get_source_queries_and_columns_to_types(
            ctx, insert_table, existing_table_name
        )
        ctx.engine_adapter._insert_overwrite_by_condition(
            existing_table_name.sql(),
            source_queries,
            columns_to_types,
            keep_existing_partition_rows=False,
        )

    ctx.compare_with_current(
        existing_table_name,
        pd.DataFrame(
            [
                {"id": 1, "ds": pd.Timestamp("2024-01-01")},
                {
                    "id": 5,
                    "ds": pd.Timestamp("2024-02-29"),
                },  # all existing Feb records overwritten by this row
                {"id": 4, "ds": pd.Timestamp("2024-03-01")},
                {"id": 6, "ds": pd.Timestamp("2024-04-01")},
            ]
        ),
    )


def test_inc_by_time_auto_partition_string(ctx: TestContext):
    # ensure automatic time partitioning works when the time column is not a Date/DateTime type
    existing_table_name = _create_table_and_insert_existing_data(
        ctx,
        columns_to_types={
            "id": exp.DataType.build("Int8", "clickhouse"),
            "ds": exp.DataType.build("String", "clickhouse"),  # String time column
        },
        table_name="data_existing",
        partitioned_by=None,
    )

    sqlmesh_context, model = ctx.upsert_sql_model(
        f"""
        MODEL (
            name test.inc_by_time_no_partition,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ds
            ),
            dialect clickhouse,
            start '2023-01-01'
        );

        SELECT
            id::Int8,
            ds::String
        FROM {existing_table_name.sql()}
        WHERE ds BETWEEN @start_ds AND @end_ds
        """
    )

    plan = sqlmesh_context.plan(no_prompts=True, auto_apply=True)

    physical_location = ctx.engine_adapter.get_data_objects(
        plan.environment.snapshots[0].physical_schema
    )[0]

    partitions = ctx.engine_adapter.fetchall(
        exp.select("_partition_id")
        .distinct()
        .from_(f"{physical_location.schema_name}.{physical_location.name}")
    )

    # The automatic time partitioning creates one partition per week. The 4 input data points
    # are located in three distinct weeks, which should have one partition each.
    assert len(partitions) == 3


def test_diff_requires_dialect(ctx: TestContext):
    sql = """
        MODEL (
          name test_schema.some_view,
          kind VIEW,
          dialect clickhouse
        );

        SELECT
          maxIf('2020-01-01'::Date, 1={rhs})::Nullable(Date) as col
    """

    sqlmesh_context, model = ctx.upsert_sql_model(sql.format(rhs="1"))
    sqlmesh_context.plan(no_prompts=True, auto_apply=True)

    _, model = ctx.upsert_sql_model(sql.format(rhs="2"))
    sqlmesh_context.upsert_model(model)

    plan = sqlmesh_context.plan(no_prompts=True, auto_apply=True, no_diff=True)

    new_snapshot = plan.context_diff.modified_snapshots['"test_schema"."some_view"'][0]
    assert new_snapshot.change_category == SnapshotChangeCategory.BREAKING
