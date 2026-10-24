from __future__ import annotations

import typing as t

import pytest
from pytest import FixtureRequest
from sqlglot import exp

from sqlmesh.core.engine_adapter import DorisEngineAdapter
from tests.core.engine_adapter.integration import (
    ENGINES_BY_NAME,
    IntegrationTestEngine,
    TestContext,
    generate_pytest_params,
)


@pytest.fixture(params=list(generate_pytest_params(ENGINES_BY_NAME["doris"])))
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture
def engine_adapter(ctx: TestContext) -> DorisEngineAdapter:
    assert isinstance(ctx.engine_adapter, DorisEngineAdapter)
    return ctx.engine_adapter


def test_engine_adapter(ctx: TestContext):
    assert isinstance(ctx.engine_adapter, DorisEngineAdapter)
    assert ctx.engine_adapter.fetchone("select 1") == (1,)


def test_doris_iceberg_local_catalog(ctx: TestContext):
    adapter = ctx.engine_adapter
    assert isinstance(adapter, DorisEngineAdapter)

    catalog_name = f"local_iceberg_{ctx.test_id}"
    schema_name = f"iceberg_db_{ctx.test_id}"
    table_fqn = f"{catalog_name}.{schema_name}.events"

    try:
        adapter.execute(
            f"""
            CREATE CATALOG IF NOT EXISTS `{catalog_name}` PROPERTIES (
                'type' = 'iceberg',
                'iceberg.catalog.type' = 'hadoop',
                'warehouse' = 'file:///tmp/doris_iceberg_warehouse/{catalog_name}'
            )
            """,
            quote_identifiers=False,
        )
        adapter.create_schema(f"{catalog_name}.{schema_name}")
        adapter.create_table(
            table_fqn,
            {
                "id": exp.DataType.build("int"),
                "ds": exp.DataType.build("date"),
            },
            partitioned_by=[exp.to_column("ds")],
        )

        cols = adapter.columns(table_fqn)
        assert "id" in cols
        assert "ds" in cols

        data_objects = adapter.get_data_objects(f"{catalog_name}.{schema_name}")
        assert any(obj.name == "events" for obj in data_objects)

        schema_target_fqn = f"{catalog_name}.{schema_name}.events_target"
        adapter.create_table(
            schema_target_fqn,
            {
                "id": exp.DataType.build("int"),
                "ds": exp.DataType.build("date"),
                "region": exp.DataType.build("varchar(32)"),
            },
            partitioned_by=[exp.to_column("ds")],
        )
        alter_ops = adapter.get_alter_operations(table_fqn, schema_target_fqn)
        assert len(alter_ops) == 1
        adapter.alter_table(alter_ops)

        updated_cols = adapter.columns(table_fqn)
        assert "region" in updated_cols
    finally:
        try:
            adapter.drop_schema(
                f"{catalog_name}.{schema_name}", ignore_if_not_exists=True, cascade=True
            )
        except Exception:
            pass
        try:
            adapter.execute(f"DROP CATALOG IF EXISTS `{catalog_name}`", quote_identifiers=False)
        except Exception:
            pass
        adapter.set_current_catalog("internal")


def test_materialized_view_sync(ctx: TestContext):
    import pandas as pd  # noqa: TID253
    from tests.core.engine_adapter.integration import wait_until

    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    source_table = ctx.table("source_table")
    ctx.engine_adapter.ctas(source_table, ctx.input_data(input_data), ctx.columns_to_types)
    view = exp.to_table("test_sync_mv")
    first_column = next(iter(ctx.columns_to_types))
    view_query = exp.select(first_column).from_(source_table)
    ctx.engine_adapter.create_view(
        view,
        view_query,
        materialized=True,
        view_properties={
            "materialized_type": exp.Literal.string("SYNC"),
            "source_table": source_table,
        },
    )

    def drop_view_success() -> bool:
        try:
            ctx.engine_adapter.drop_view(
                view,
                ignore_if_not_exists=False,
                materialized=True,
                view_properties={
                    "materialized_type": exp.Literal.string("SYNC"),
                    "source_table": source_table,
                },
            )
            return True
        except Exception:
            return False

    wait_until(drop_view_success, attempts=30, wait=1)


def test_materialized_view_async_mtmv(ctx: TestContext):
    import pandas as pd  # noqa: TID253
    from tests.core.engine_adapter.integration import wait_until

    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    source_table = ctx.table("mtmv_source")
    ctx.engine_adapter.ctas(source_table, ctx.input_data(input_data), ctx.columns_to_types)
    view = ctx.table("test_async_mtmv")
    view_query = exp.select(*ctx.columns_to_types).from_(source_table)
    ctx.engine_adapter.create_view(
        view,
        view_query,
        materialized=True,
        view_properties={
            "build": exp.Literal.string("IMMEDIATE"),
            "refresh": exp.Literal.string("COMPLETE"),
            "refresh_trigger": exp.Literal.string("ON MANUAL"),
            "distributed_by": exp.Literal.string("RANDOM BUCKETS AUTO"),
        },
    )
    wait_until(lambda: len(ctx.get_current_data(view)) == len(input_data), attempts=30, wait=1)
    ctx.compare_with_current(view, input_data)
    results = ctx.get_metadata_results()
    assert exp.to_table(view).name in results.materialized_views
    assert exp.to_table(view).name not in results.tables
    ctx.engine_adapter.drop_view(view, materialized=True)
    assert len(ctx.get_metadata_results().materialized_views) == 0


def test_merge_unique_key(ctx: TestContext):
    import pandas as pd  # noqa: TID253

    table = ctx.table("test_merge_uk")
    ctx.engine_adapter.create_table(
        table,
        ctx.columns_to_types,
        table_properties={"unique_key": exp.Literal.string("id")},
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.merge(
        table,
        ctx.input_data(input_data),
        target_columns_to_types=None,
        unique_key=[exp.to_identifier("id")],
    )
    merge_data = pd.DataFrame(
        [
            {"id": 2, "ds": "2022-01-10"},
            {"id": 4, "ds": "2022-01-04"},
        ]
    )
    ctx.engine_adapter.merge(
        table,
        ctx.input_data(merge_data),
        target_columns_to_types=None,
        unique_key=[exp.to_identifier("id")],
    )
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {"id": 1, "ds": "2022-01-01"},
                {"id": 2, "ds": "2022-01-10"},
                {"id": 3, "ds": "2022-01-03"},
                {"id": 4, "ds": "2022-01-04"},
            ]
        ),
    )


def test_auto_partition_and_insert_overwrite_by_partition(ctx: TestContext):
    import pandas as pd  # noqa: TID253
    from sqlglot import parse_one

    table = ctx.table("test_auto_partition")
    columns_to_types = {
        "payload": exp.DataType.build("STRING", dialect="doris"),
        "id": exp.DataType.build("INT", dialect="doris"),
        "ds": exp.DataType.build("DATE", dialect="doris"),
    }
    partitioned_by = [parse_one("RANGE(DATE_TRUNC(ds, 'day'))", read="doris")]
    ctx.engine_adapter.create_table(
        table,
        columns_to_types,
        partitioned_by=partitioned_by,
        table_properties={"auto_partition": exp.Literal.string("true")},
    )
    initial_data = pd.DataFrame(
        [
            {"payload": "a", "id": 1, "ds": "2022-01-01"},
            {"payload": "b", "id": 2, "ds": "2022-01-02"},
        ]
    )
    ctx.engine_adapter.insert_append(
        table,
        ctx.input_data(initial_data, columns_to_types),
        target_columns_to_types=columns_to_types,
    )
    overwrite_data = pd.DataFrame(
        [
            {"payload": "b_updated", "id": 20, "ds": "2022-01-02"},
            {"payload": "c", "id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_overwrite_by_partition(
        table,
        ctx.input_data(overwrite_data, columns_to_types),
        partitioned_by=partitioned_by,
        target_columns_to_types=columns_to_types,
    )
    rows = ctx.engine_adapter.fetchall(
        exp.select("id", exp.cast(exp.column("ds"), "VARCHAR").as_("ds"), "payload")
        .from_(table)
        .order_by("id")
    )
    assert list(rows) == [
        (1, "2022-01-01", "a"),
        (3, "2022-01-03", "c"),
        (20, "2022-01-02", "b_updated"),
    ]


def test_unique_key_order_by_and_key_reordering(ctx: TestContext):
    import pandas as pd  # noqa: TID253

    table = ctx.table("test_uk_order_by")
    columns_to_types = {
        "payload": exp.DataType.build("VARCHAR(64)", dialect="doris"),
        "id": exp.DataType.build("INT", dialect="doris"),
        "ds": exp.DataType.build("DATE", dialect="doris"),
        "seq": exp.DataType.build("INT", dialect="doris"),
    }
    ctx.engine_adapter.create_table(
        table,
        columns_to_types,
        unique_key=[exp.to_column("id"), exp.to_column("ds")],
        table_properties={
            "order_by": exp.Tuple(expressions=[exp.to_column("seq"), exp.to_column("id")]),
        },
    )
    df = pd.DataFrame(
        [
            {"payload": "first", "id": 1, "ds": "2022-01-01", "seq": 10},
            {"payload": "second", "id": 2, "ds": "2022-01-02", "seq": 5},
        ]
    )
    ctx.engine_adapter.insert_append(
        table,
        ctx.input_data(df, columns_to_types),
        target_columns_to_types=columns_to_types,
    )
    rows = ctx.engine_adapter.fetchall(
        exp.select("id", exp.cast(exp.column("ds"), "VARCHAR").as_("ds"), "seq", "payload")
        .from_(table)
        .order_by("id")
    )
    assert list(rows) == [
        (1, "2022-01-01", 10, "first"),
        (2, "2022-01-02", 5, "second"),
    ]


def test_duplicate_key_delete_with_subquery_and_between(ctx: TestContext):
    import pandas as pd  # noqa: TID253

    target_table = ctx.table("test_dup_delete_target")
    src_table = ctx.table("test_dup_delete_src")
    ctx.engine_adapter.create_table(
        target_table,
        ctx.columns_to_types,
        table_properties={
            "duplicate_key": exp.Tuple(expressions=[exp.to_column("id"), exp.to_column("ds")]),
        },
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
            {"id": 4, "ds": "2022-01-04"},
        ]
    )
    ctx.engine_adapter.insert_append(
        target_table,
        ctx.input_data(input_data),
        target_columns_to_types=ctx.columns_to_types,
    )
    # 1. BETWEEN rewrite in DELETE WHERE
    ctx.engine_adapter.delete_from(target_table, "ds BETWEEN '2022-01-01' AND '2022-01-01'")

    # 2. Subquery DELETE on DUPLICATE KEY table (triggers literal batch fallback since USING requires UNIQUE KEY)
    src_data = pd.DataFrame([{"id": 2, "ds": "2022-01-02"}])
    ctx.engine_adapter.ctas(src_table, ctx.input_data(src_data), ctx.columns_to_types)
    key_exp = exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", exp.column("id"), exp.column("ds"))
    ctx.engine_adapter.delete_from(
        target_table,
        where=key_exp.isin(query=exp.select(key_exp).from_(src_table)),
    )
    ctx.compare_with_current(
        target_table,
        pd.DataFrame(
            [
                {"id": 3, "ds": "2022-01-03"},
                {"id": 4, "ds": "2022-01-04"},
            ]
        ),
    )


def test_insert_overwrite_by_time_partition_and_scd_type_2(ctx: TestContext):
    from datetime import datetime
    import pandas as pd  # noqa: TID253
    from sqlmesh.utils.date import to_time_column

    # 1. insert_overwrite_by_time_partition with CAST(... AS DATE) bounds and partition column after VARCHAR
    time_table = ctx.table("test_time_range_overwrite")
    time_cols = {
        "code": exp.DataType.build("VARCHAR(64)", dialect="doris"),
        "ds": exp.DataType.build("DATE", dialect="doris"),
        "amount": exp.DataType.build("DOUBLE", dialect="doris"),
    }
    ctx.engine_adapter.create_table(
        time_table,
        time_cols,
        partitioned_by=[exp.to_column("ds")],
    )
    initial_df = pd.DataFrame(
        [
            {"code": "a", "ds": "2022-01-01", "amount": 10.0},
            {"code": "b", "ds": "2022-01-02", "amount": 20.0},
            {"code": "c", "ds": "2022-01-03", "amount": 30.0},
        ]
    )
    ctx.engine_adapter.insert_append(
        time_table,
        ctx.input_data(initial_df, time_cols),
        target_columns_to_types=time_cols,
    )
    replacement_df = pd.DataFrame(
        [
            {"code": "b_new", "ds": "2022-01-02", "amount": 25.0},
        ]
    )
    ctx.engine_adapter.insert_overwrite_by_time_partition(
        time_table,
        ctx.input_data(replacement_df, time_cols),
        start="2022-01-02",
        end="2022-01-02",
        time_column="ds",
        time_formatter=lambda v, _: to_time_column(
            v, exp.DataType.build("DATE", dialect="doris"), "doris"
        ),
        target_columns_to_types=time_cols,
    )
    rows = ctx.engine_adapter.fetchall(
        exp.select("code", exp.cast(exp.column("ds"), "VARCHAR").as_("ds"), "amount")
        .from_(time_table)
        .order_by("ds")
    )
    assert list(rows) == [
        ("a", "2022-01-01", 10.0),
        ("b_new", "2022-01-02", 25.0),
        ("c", "2022-01-03", 30.0),
    ]

    # 2. scd_type_2_by_time across initial load and incremental update
    scd_table = ctx.table("test_scd_type_2")
    scd_src_table = ctx.table("test_scd_type_2_src")
    scd_src_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "name": exp.DataType.build("VARCHAR(64)", dialect="doris"),
        "updated_at": exp.DataType.build("DATETIME", dialect="doris"),
    }
    scd_target_cols = {
        **scd_src_cols,
        "valid_from": exp.DataType.build("DATETIME", dialect="doris"),
        "valid_to": exp.DataType.build("DATETIME", dialect="doris"),
    }
    ctx.engine_adapter.create_table(scd_table, scd_target_cols)
    batch1_df = pd.DataFrame(
        [
            {"id": 1, "name": "alice_v1", "updated_at": "2022-01-01 00:00:00"},
            {"id": 2, "name": "bob_v1", "updated_at": "2022-01-01 00:00:00"},
        ]
    )
    ctx.engine_adapter.ctas(scd_src_table, ctx.input_data(batch1_df, scd_src_cols), scd_src_cols)
    ctx.engine_adapter.scd_type_2_by_time(
        target_table=scd_table,
        source_table=exp.select("id", "name", "updated_at").from_(scd_src_table),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        target_columns_to_types=scd_target_cols,
        execution_time=datetime(2022, 1, 2, 0, 0, 0),
        truncate=True,
    )
    batch2_df = pd.DataFrame(
        [
            {"id": 1, "name": "alice_v2", "updated_at": "2022-01-05 00:00:00"},
            {"id": 2, "name": "bob_v1", "updated_at": "2022-01-01 00:00:00"},
        ]
    )
    ctx.engine_adapter.replace_query(
        scd_src_table,
        ctx.input_data(batch2_df, scd_src_cols),
        target_columns_to_types=scd_src_cols,
    )
    ctx.engine_adapter.scd_type_2_by_time(
        target_table=scd_table,
        source_table=exp.select("id", "name", "updated_at").from_(scd_src_table),
        unique_key=[exp.column("id")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        target_columns_to_types=scd_target_cols,
        execution_time=datetime(2022, 1, 6, 0, 0, 0),
        truncate=False,
    )
    scd_rows = ctx.engine_adapter.fetchall(
        exp.select("id", "name", exp.column("valid_to").is_(exp.null()).as_("is_current"))
        .from_(scd_table)
        .order_by("id", "valid_from")
    )
    assert list(scd_rows) == [
        (1, "alice_v1", 0),
        (1, "alice_v2", 1),
        (2, "bob_v1", 1),
    ]
