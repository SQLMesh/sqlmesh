# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import typing as t

import pytest
from pytest import FixtureRequest
from sqlglot import exp, parse_one

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

        # Test Iceberg table maintenance actions & branch DDL parsed via Doris dialect
        create_branch_expr = parse_one(
            f"ALTER TABLE {table_fqn} CREATE BRANCH IF NOT EXISTS audit_branch",
            read="doris",
        )
        adapter.execute(create_branch_expr, quote_identifiers=False)

        branch_read_expr = parse_one(
            f"SELECT id FROM {table_fqn}@branch(audit_branch)",
            read="doris",
        )
        assert list(adapter.fetchall(branch_read_expr, quote_identifiers=False)) == []

        compact_expr = parse_one(
            f"ALTER TABLE {table_fqn} EXECUTE rewrite_data_files()",
            read="doris",
        )
        adapter.execute(compact_expr, quote_identifiers=False)

        # Also verify writable Apache Paimon local catalog DDL & schema evolution on Doris 4.1+
        paimon_cat = f"local_paimon_{ctx.test_id}"
        paimon_db = f"paimon_db_{ctx.test_id}"
        paimon_tbl = f"{paimon_cat}.{paimon_db}.orders"
        try:
            adapter.execute(
                f"""
                CREATE CATALOG IF NOT EXISTS `{paimon_cat}` PROPERTIES (
                    'type' = 'paimon',
                    'warehouse' = 'file:///tmp/doris_paimon_warehouse/{paimon_cat}'
                )
                """,
                quote_identifiers=False,
            )
            adapter.create_schema(f"{paimon_cat}.{paimon_db}")
            adapter.create_table(
                paimon_tbl,
                {
                    "id": exp.DataType.build("int"),
                    "ds": exp.DataType.build("date"),
                },
                table_format="paimon",
                storage_format="parquet",
                partitioned_by=[exp.to_column("ds")],
                unique_key=[exp.to_column("id")],
            )
            paimon_cols = adapter.columns(paimon_tbl)
            assert "id" in paimon_cols
            assert "ds" in paimon_cols
        finally:
            try:
                adapter.drop_schema(
                    f"{paimon_cat}.{paimon_db}", ignore_if_not_exists=True, cascade=True
                )
            except Exception:
                pass
            try:
                adapter.execute(f"DROP CATALOG IF EXISTS `{paimon_cat}`", quote_identifiers=False)
            except Exception:
                pass
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

    # Verify DATETIME partition overwrite deletes the entire day range even when timestamps differ
    ts_table = ctx.table("test_auto_partition_ts")
    ts_cols = {
        "payload": exp.DataType.build("STRING", dialect="doris"),
        "id": exp.DataType.build("INT", dialect="doris"),
        "ts": exp.DataType.build("DATETIME", dialect="doris"),
    }
    ts_partitioned_by = [parse_one("RANGE(DATE_TRUNC(ts, 'day'))", read="doris")]
    ctx.engine_adapter.create_table(
        ts_table,
        ts_cols,
        partitioned_by=ts_partitioned_by,
        table_properties={"auto_partition": exp.Literal.string("true")},
    )
    ts_initial = pd.DataFrame(
        [
            {"payload": "d1", "id": 1, "ts": "2022-01-01 09:00:00"},
            {"payload": "d2_morning", "id": 2, "ts": "2022-01-02 08:00:00"},
            {"payload": "d2_evening", "id": 3, "ts": "2022-01-02 18:00:00"},
        ]
    )
    ctx.engine_adapter.insert_append(
        ts_table,
        ctx.input_data(ts_initial, ts_cols),
        target_columns_to_types=ts_cols,
    )
    ts_overwrite = pd.DataFrame(
        [
            {"payload": "d2_replaced", "id": 20, "ts": "2022-01-02 14:30:00"},
        ]
    )
    ctx.engine_adapter.insert_overwrite_by_partition(
        ts_table,
        ctx.input_data(ts_overwrite, ts_cols),
        partitioned_by=ts_partitioned_by,
        target_columns_to_types=ts_cols,
    )
    ts_rows = ctx.engine_adapter.fetchall(
        exp.select("id", "payload").from_(ts_table).order_by("id")
    )
    assert list(ts_rows) == [
        (1, "d1"),
        (20, "d2_replaced"),
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


def test_doris_complex_types_and_alter_table_schema_evolution(ctx: TestContext):
    from sqlglot import parse_one

    # 1. Create table with LARGEINT, ARRAY, MAP, STRUCT, and BITMAP, insert rows, and run row-diff normalization
    rich_table = ctx.table("test_doris_complex_types")
    rich_cols = {
        "id": exp.DataType.build("INT128", dialect="doris"),
        "tags": exp.DataType.build("ARRAY<VARCHAR(32)>", dialect="doris"),
        "attrs": exp.DataType.build("MAP<VARCHAR(32), INT>", dialect="doris"),
        "profile": exp.DataType.build(
            "STRUCT<uid: BIGINT, label: VARCHAR(64)>", dialect="doris", udt=True
        ),
        "bm": exp.DataType.build("BITMAP", dialect="doris", udt=True),
    }
    ctx.engine_adapter.create_table(
        rich_table,
        rich_cols,
        table_properties={"duplicate_key": exp.Tuple(expressions=[exp.to_column("id")])},
    )
    ctx.engine_adapter.execute(
        f"INSERT INTO {ctx.engine_adapter._to_sql(rich_table)} (`id`, `tags`, `attrs`, `profile`, `bm`) "
        f"VALUES (100, ARRAY('x', 'y'), MAP('k1', 10, 'k2', 20), STRUCT(7, 'neo'), TO_BITMAP(42))"
    )
    described_cols = ctx.engine_adapter.columns(rich_table)
    assert described_cols["id"] == exp.DataType.build("INT128")
    assert ctx.engine_adapter.get_alter_operations(rich_table, rich_table) == []
    concat_expr = t.cast(DorisEngineAdapter, ctx.engine_adapter).concat_columns(described_cols)
    diff_rows = ctx.engine_adapter.fetchall(exp.select(concat_expr).from_(rich_table))
    assert len(diff_rows) == 1
    assert "100" in str(diff_rows[0][0])
    assert "42" in str(diff_rows[0][0])

    # 2. Live schema evolution (ADD COLUMN + multiple MODIFY COLUMN widenings + suppressed key-column VARCHAR->STRING no-op)
    cur_tbl = ctx.table("test_alter_cur")
    tgt_tbl = ctx.table("test_alter_tgt")
    ctx.engine_adapter.create_table(
        cur_tbl,
        {
            "key_code": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            "notes": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            "cnt": exp.DataType.build("INT", dialect="doris"),
            "dt": exp.DataType.build("DATETIME(0)", dialect="doris"),
            "tz": exp.DataType.build("TIMESTAMPTZ(0)", dialect="doris"),
        },
        table_properties={"duplicate_key": exp.Tuple(expressions=[exp.to_column("key_code")])},
    )
    ctx.engine_adapter.create_table(
        tgt_tbl,
        {
            "key_code": exp.DataType.build("VARCHAR(32)", dialect="doris"),
            "notes": exp.DataType.build("TEXT", dialect="doris"),
            "cnt": exp.DataType.build("BIGINT", dialect="doris"),
            "dt": exp.DataType.build("DATETIME(6)", dialect="doris"),
            "tz": exp.DataType.build("TIMESTAMPTZ(6)", dialect="doris"),
            "extra_cnt": exp.DataType.build("BIGINT", dialect="doris"),
        },
        table_properties={"duplicate_key": exp.Tuple(expressions=[exp.to_column("key_code")])},
    )
    alter_ops = ctx.engine_adapter.get_alter_operations(cur_tbl, tgt_tbl)
    ctx.engine_adapter.alter_table(alter_ops)
    evolved_cols = ctx.engine_adapter.columns(cur_tbl)
    assert "extra_cnt" in evolved_cols
    assert evolved_cols["key_code"] == exp.DataType.build("VARCHAR(32)", dialect="doris")
    assert evolved_cols["notes"] == exp.DataType.build("TEXT", dialect="doris")
    assert evolved_cols["cnt"] == exp.DataType.build("BIGINT", dialect="doris")
    assert evolved_cols["dt"] == exp.DataType.build("DATETIME(6)", dialect="doris")
    assert evolved_cols["tz"] == exp.DataType.build("TIMESTAMPTZ(6)", dialect="doris")

    # 3. Direct time-transform inequality delete on OLAP table
    ev_tbl = ctx.table("test_time_ineq_del")
    ev_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "ds": exp.DataType.build("DATE", dialect="doris"),
    }
    ctx.engine_adapter.create_table(ev_tbl, ev_cols)
    ctx.engine_adapter.execute(
        f"INSERT INTO {ctx.engine_adapter._to_sql(ev_tbl)} (`id`, `ds`) VALUES "
        f"(1, '2024-01-14'), (2, '2024-01-15'), (3, '2024-01-16')"
    )
    ctx.engine_adapter.delete_from(
        ev_tbl,
        parse_one(
            "DATE_TRUNC(ds, 'day') >= '2024-01-15' AND DATE_TRUNC(ds, 'day') <= '2024-01-15'",
            read="doris",
        ),
    )
    remaining_ids = [
        r[0] for r in ctx.engine_adapter.fetchall(exp.select("id").from_(ev_tbl).order_by("id"))
    ]
    assert remaining_ids == [1, 3]

    # 4. De Morgan `NOT (id = 3)` and aligned `DATE_TRUNC(ds, 'week')` in `delete_from`, plus `insert_append` OLAP reordering
    ctx.engine_adapter.delete_from(
        ev_tbl,
        parse_one("NOT (id = 3) AND DATE_TRUNC(ds, 'week') = '2024-01-08'", read="doris"),
    )
    remaining_after_week = [
        r[0] for r in ctx.engine_adapter.fetchall(exp.select("id").from_(ev_tbl).order_by("id"))
    ]
    assert remaining_after_week == [3]

    app_tbl = ctx.table("test_insert_append_reorder")
    app_cols = {
        "score": exp.DataType.build("DOUBLE", dialect="doris"),
        "id": exp.DataType.build("INT", dialect="doris"),
    }
    ctx.engine_adapter.create_table(app_tbl, app_cols)
    ctx.engine_adapter.insert_append(
        app_tbl,
        parse_one("SELECT 9.5 AS score, 7 AS id", read="doris"),
        target_columns_to_types=app_cols,
    )
    app_rows = ctx.engine_adapter.fetchall(exp.select("id", "score").from_(app_tbl))
    assert list(app_rows) == [(7, 9.5)]


def test_doris_4_1_native_merge_segment_v3_and_ann_index(ctx: TestContext):
    # 1. Native MERGE INTO with merge_filter and when_matched on a Merge-on-Write UNIQUE KEY table
    merge_tbl = ctx.table("test_doris_41_merge")
    merge_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "name": exp.DataType.build("VARCHAR(64)", dialect="doris"),
        "val": exp.DataType.build("INT", dialect="doris"),
    }
    ctx.engine_adapter.create_table(
        merge_tbl,
        merge_cols,
        unique_key=[exp.to_column("id")],
    )
    ctx.engine_adapter.execute(
        f"INSERT INTO {ctx.engine_adapter._to_sql(merge_tbl)} (`id`, `name`, `val`) VALUES (1, 'orig_a', 10), (2, 'orig_b', 20)"
    )
    src_query = parse_one(
        "SELECT 1 AS id, 'new_a' AS name, 15 AS val UNION ALL "
        "SELECT 2 AS id, 'new_b' AS name, 5 AS val UNION ALL "
        "SELECT 3 AS id, 'new_c' AS name, 30 AS val",
        read="doris",
    )
    ctx.engine_adapter.merge(
        target_table=merge_tbl,
        source_table=src_query,
        target_columns_to_types=merge_cols,
        unique_key=[exp.to_column("id")],
        merge_filter=parse_one("__MERGE_SOURCE__.id > 0", read="doris"),
        when_matched=parse_one(
            "WHEN MATCHED AND __MERGE_SOURCE__.val > __MERGE_TARGET__.val THEN UPDATE SET __MERGE_TARGET__.val = __MERGE_SOURCE__.val",
            into=exp.Whens,
        ),
    )
    merged_rows = ctx.engine_adapter.fetchall(
        exp.select("id", "name", "val").from_(merge_tbl).order_by("id")
    )
    # Row 1 updated val (15 > 10) while keeping name='orig_a'; Row 2 unchanged (5 < 20); Row 3 inserted
    assert list(merged_rows) == [
        (1, "orig_a", 15),
        (2, "orig_b", 20),
        (3, "new_c", 30),
    ]

    # 2. Doris 4.1 Segment V3 storage_format = 'V3' via physical_properties
    v3_tbl = ctx.table("test_doris_41_segment_v3")
    v3_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "payload": exp.DataType.build("VARCHAR(128)", dialect="doris"),
    }
    ctx.engine_adapter.create_table(
        v3_tbl,
        v3_cols,
        table_properties={"storage_format": exp.Literal.string("V3")},
    )
    v3_row = ctx.engine_adapter.fetchone(f"SHOW CREATE TABLE {ctx.engine_adapter._to_sql(v3_tbl)}")
    assert v3_row is not None
    create_v3_ddl = v3_row[1]
    assert '"storage_format" = "V3"' in create_v3_ddl

    # 3. Doris 4.1 USING ANN IVF vector index creation
    ann_tbl = ctx.table("test_doris_41_ann")
    ann_tbl_sql = ctx.engine_adapter._to_sql(ann_tbl)
    ctx.engine_adapter.execute(
        f"""
        CREATE TABLE {ann_tbl_sql} (
            `id` INT NOT NULL,
            `vec` ARRAY<FLOAT> NOT NULL,
            INDEX idx_vec (`vec`) USING ANN PROPERTIES (
                'index_type' = 'ivf',
                'metric_type' = 'l2_distance',
                'dim' = '4',
                'nlist' = '2'
            )
        ) DUPLICATE KEY(`id`)
        DISTRIBUTED BY HASH(`id`) BUCKETS 1
        PROPERTIES ('replication_num' = '1')
        """
    )
    ann_row = ctx.engine_adapter.fetchone(f"SHOW CREATE TABLE {ann_tbl_sql}")
    assert ann_row is not None
    create_ann_ddl = ann_row[1]
    assert "USING ANN" in create_ann_ddl
    assert '"index_type" = "ivf"' in create_ann_ddl

    # 4. Vector distance functions (L2_DISTANCE, COSINE_DISTANCE, INNER_PRODUCT, L1_DISTANCE)
    ctx.engine_adapter.execute(
        f"INSERT INTO {ann_tbl_sql} (`id`, `vec`) VALUES (1, [1.0, 0.0, 0.0, 0.0]), (2, [0.0, 1.0, 0.0, 0.0])"
    )
    vec_query = parse_one(
        f"SELECT id, L2_DISTANCE(vec, [1.0, 0.0, 0.0, 0.0]) AS l2, "
        f"COSINE_DISTANCE(vec, [1.0, 0.0, 0.0, 0.0]) AS cos_d, "
        f"INNER_PRODUCT(vec, [1.0, 0.0, 0.0, 0.0]) AS ip, "
        f"L1_DISTANCE(vec, [1.0, 0.0, 0.0, 0.0]) AS l1 "
        f"FROM {ann_tbl_sql} ORDER BY l2 ASC LIMIT 1",
        read="doris",
    )
    vec_row = ctx.engine_adapter.fetchone(vec_query)
    assert vec_row is not None
    assert vec_row[0] == 1
    assert float(vec_row[1]) == 0.0
    assert float(vec_row[3]) == 1.0

    # 5. Inverted index creation via create_index, BUILD INDEX, MATCH_ANY, and SEARCH()
    doris_adapter = t.cast(DorisEngineAdapter, ctx.engine_adapter)
    fts_tbl = ctx.table("test_doris_41_fts")
    fts_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "msg": exp.DataType.build("STRING", dialect="doris"),
    }
    doris_adapter.create_table(
        fts_tbl,
        fts_cols,
        table_properties={"compaction_policy": exp.Literal.string("time_series")},
    )
    doris_adapter.create_index(
        fts_tbl,
        "idx_msg",
        [exp.to_column("msg")],
        index_type="INVERTED",
        index_properties={"parser": "english"},
        comment="fts index",
    )
    doris_adapter.execute(
        f"INSERT INTO {doris_adapter._to_sql(fts_tbl)} (`id`, `msg`) VALUES "
        f"(1, 'apache doris vector search'), (2, 'iceberg table compaction')"
    )
    fts_query = parse_one(
        f"SELECT id FROM {doris_adapter._to_sql(fts_tbl)} WHERE msg MATCH_ANY 'doris vector' ORDER BY id",
        read="doris",
    )
    assert [r[0] for r in doris_adapter.fetchall(fts_query)] == [1]

    # 6. UNIQUE KEY table with function_column.sequence_col and _replace_by_key sequence protection
    seq_tbl = ctx.table("test_doris_41_seq")
    seq_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "val": exp.DataType.build("VARCHAR(32)", dialect="doris"),
        "ver": exp.DataType.build("BIGINT", dialect="doris"),
    }
    doris_adapter.create_table(
        seq_tbl,
        seq_cols,
        unique_key=[exp.to_column("id")],
        table_properties={"function_column.sequence_col": exp.Literal.string("ver")},
    )
    doris_adapter._replace_by_key(
        seq_tbl,
        parse_one("SELECT 1 AS id, 'v10' AS val, 10 AS ver", read="doris"),
        seq_cols,
        [exp.to_column("id")],
        is_unique_key=True,
        table_properties={"function_column.sequence_col": exp.Literal.string("ver")},
    )
    # Stale update with lower sequence number (ver = 5 < 10) must not overwrite 'v10'
    doris_adapter._replace_by_key(
        seq_tbl,
        parse_one("SELECT 1 AS id, 'v5_stale' AS val, 5 AS ver", read="doris"),
        seq_cols,
        [exp.to_column("id")],
        is_unique_key=True,
        table_properties={"function_column.sequence_col": exp.Literal.string("ver")},
    )
    seq_rows = doris_adapter.fetchall(exp.select("id", "val", "ver").from_(seq_tbl))
    assert list(seq_rows) == [(1, "v10", 10)]

    # 7. Self-referencing replace_query with aliased, catalog-qualified `internal.<db>.<tbl> AS t`
    self_ref_tbl = ctx.table("test_doris_self_ref")
    self_ref_cols = {
        "id": exp.DataType.build("INT", dialect="doris"),
        "val": exp.DataType.build("INT", dialect="doris"),
    }
    doris_adapter.create_table(self_ref_tbl, self_ref_cols)
    doris_adapter.execute(
        f"INSERT INTO {doris_adapter._to_sql(self_ref_tbl)} (`id`, `val`) VALUES (1, 10), (2, 20)"
    )
    prev_default_cat = doris_adapter._default_catalog
    try:
        doris_adapter._default_catalog = "internal"
        doris_adapter.replace_query(
            self_ref_tbl,
            parse_one(
                f"SELECT t.id, t.val + 5 AS val FROM `internal`.`{self_ref_tbl.db}`.`{self_ref_tbl.name}` AS t",
                read="doris",
            ),
            target_columns_to_types=self_ref_cols,
        )
    finally:
        doris_adapter._default_catalog = prev_default_cat
    self_ref_rows = doris_adapter.fetchall(
        exp.select("id", "val").from_(self_ref_tbl).order_by("id")
    )
    assert list(self_ref_rows) == [(1, 15), (2, 25)]

    # 8. NGRAM_BF index creation + BUILD INDEX on INVERTED index + TIMESTAMPTZ cross-timezone normalize_value equivalence
    doris_adapter.create_index(
        fts_tbl,
        "idx_msg_ngram",
        [exp.to_column("msg")],
        index_type="NGRAM_BF",
        index_properties={"gram_size": "3", "bf_size": "256"},
    )
    doris_adapter.execute(
        parse_one(
            f"BUILD INDEX idx_msg ON {doris_adapter._to_sql(fts_tbl)}",
            read="doris",
        )
    )
    norm_tz = doris_adapter.normalize_value(
        exp.to_column("ts"),
        exp.DataType.build("TIMESTAMPTZ(6)", dialect="doris"),
        timestamp_precision=6,
    )
    tz_check_rows = doris_adapter.fetchall(
        exp.select(norm_tz.as_("utc_str")).from_(
            parse_one(
                "(SELECT CAST('2024-06-01 12:00:00.123456+08:00' AS TIMESTAMPTZ(6)) AS ts "
                "UNION ALL "
                "SELECT CAST('2024-06-01 04:00:00.123456+00:00' AS TIMESTAMPTZ(6)) AS ts) t",
                read="doris",
            )
        )
    )
    assert len(tz_check_rows) == 2
    assert tz_check_rows[0][0] == tz_check_rows[1][0] == "2024-06-01 04:00:00.123456"
