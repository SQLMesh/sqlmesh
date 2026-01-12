# type: ignore
from __future__ import annotations

import pathlib
import re
import sys
import typing as t
import shutil
from datetime import datetime, timedelta, date
from unittest import mock
from unittest.mock import patch
import logging


import time_machine

import numpy as np  # noqa: TID253
import pandas as pd  # noqa: TID253
import pytest
import pytz
import time_machine
from sqlglot import exp, parse_one
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_columns import quote_identifiers

from sqlmesh import Config, Context
from sqlmesh.cli.project_init import init_example_project
from sqlmesh.core.config.common import VirtualEnvironmentMode
from sqlmesh.core.config.connection import ConnectionConfig
import sqlmesh.core.dialect as d
from sqlmesh.core.environment import EnvironmentSuffixTarget
from sqlmesh.core.dialect import select_from_values
from sqlmesh.core.model import Model, load_sql_based_model
from sqlmesh.core.engine_adapter.shared import DataObject, DataObjectType
from sqlmesh.core.engine_adapter.mixins import RowDiffMixin, LogicalMergeMixin
from sqlmesh.core.model.definition import create_sql_model
from sqlmesh.core.plan import Plan
from sqlmesh.core.state_sync.db import EngineAdapterStateSync
from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory
from sqlmesh.utils.date import now, to_date, to_time_column
from sqlmesh.core.table_diff import TableDiff
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.pydantic import PydanticModel
from tests.conftest import SushiDataValidator
from tests.core.engine_adapter.integration import (
    TestContext,
    MetadataResults,
    TEST_SCHEMA,
    wait_until,
)

DATA_TYPE = exp.DataType.Type
VARCHAR_100 = exp.DataType.build("varchar(100)")


class PlanResults(PydanticModel):
    plan: Plan
    ctx: TestContext
    schema_metadata: MetadataResults
    internal_schema_metadata: MetadataResults

    @classmethod
    def create(cls, plan: Plan, ctx: TestContext, schema_name: str):
        schema_metadata = ctx.get_metadata_results(schema_name)
        internal_schema_metadata = ctx.get_metadata_results(f"sqlmesh__{schema_name}")
        return PlanResults(
            plan=plan,
            ctx=ctx,
            schema_metadata=schema_metadata,
            internal_schema_metadata=internal_schema_metadata,
        )

    def snapshot_for(self, model: Model) -> Snapshot:
        return next((s for s in list(self.plan.snapshots.values()) if s.name == model.fqn))

    def modified_snapshot_for(self, model: Model) -> Snapshot:
        return next((s for s in list(self.plan.modified_snapshots.values()) if s.name == model.fqn))

    def table_name_for(
        self, snapshot_or_model: Snapshot | Model, is_deployable: bool = True
    ) -> str:
        snapshot = (
            snapshot_or_model
            if isinstance(snapshot_or_model, Snapshot)
            else self.snapshot_for(snapshot_or_model)
        )
        table_name = snapshot.table_name(is_deployable)
        return exp.to_table(table_name).this.sql(dialect=self.ctx.dialect)

    def dev_table_name_for(self, snapshot: Snapshot) -> str:
        return self.table_name_for(snapshot, is_deployable=False)


def test_connection(ctx: TestContext):
    cursor_from_connection = ctx.engine_adapter.connection.cursor()
    cursor_from_connection.execute("SELECT 1")
    assert cursor_from_connection.fetchone()[0] == 1


def test_catalog_operations(ctx: TestContext):
    if (
        ctx.engine_adapter.catalog_support.is_unsupported
        or ctx.engine_adapter.catalog_support.is_single_catalog_only
    ):
        pytest.skip(
            f"Engine adapter {ctx.engine_adapter.dialect} doesn't support catalog operations"
        )

    # use a unique name so that integration tests on cloud databases can run in parallel
    catalog_name = "testing" if not ctx.is_remote else ctx.add_test_suffix("testing")

    ctx.create_catalog(catalog_name)

    current_catalog = ctx.engine_adapter.get_current_catalog().lower()
    ctx.engine_adapter.set_current_catalog(catalog_name)
    assert ctx.engine_adapter.get_current_catalog().lower() == catalog_name
    ctx.engine_adapter.set_current_catalog(current_catalog)
    assert ctx.engine_adapter.get_current_catalog().lower() == current_catalog

    # cleanup cloud databases since they persist between runs
    if ctx.is_remote:
        ctx.drop_catalog(catalog_name)


def test_drop_schema_catalog(ctx: TestContext, caplog):
    def drop_schema_and_validate(schema_name: str):
        ctx.engine_adapter.drop_schema(schema_name, cascade=True)
        results = ctx.get_metadata_results(schema_name)
        assert (
            len(results.tables)
            == len(results.views)
            == len(results.materialized_views)
            == len(results.non_temp_tables)
            == 0
        )

    def create_objects_and_validate(schema_name: str):
        ctx.engine_adapter.create_schema(schema_name)
        ctx.engine_adapter.create_view(f"{schema_name}.test_view", parse_one("SELECT 1 as col"))
        ctx.engine_adapter.create_table(
            f"{schema_name}.test_table", {"col": exp.DataType.build("int")}
        )
        ctx.engine_adapter.create_table(
            f"{schema_name}.replace_table", {"col": exp.DataType.build("int")}
        )
        ctx.engine_adapter.replace_query(
            f"{schema_name}.replace_table",
            parse_one("SELECT 1 as col"),
            {"col": exp.DataType.build("int")},
        )
        results = ctx.get_metadata_results(schema_name)
        assert len(results.tables) == 2
        assert len(results.views) == 1
        assert len(results.materialized_views) == 0
        assert len(results.non_temp_tables) == 2

    if ctx.engine_adapter.catalog_support.is_unsupported:
        pytest.skip(
            f"Engine adapter {ctx.engine_adapter.dialect} doesn't support catalog operations"
        )
    if ctx.dialect == "spark":
        pytest.skip(
            "Currently local spark is configured to have iceberg be the testing catalog and drop cascade doesn't work on iceberg. Skipping until we have time to fix."
        )

    catalog_name = "testing" if not ctx.is_remote else ctx.add_test_suffix("testing")
    if ctx.dialect == "bigquery":
        catalog_name = ctx.engine_adapter.get_current_catalog()

    catalog_name = normalize_identifiers(catalog_name, dialect=ctx.dialect).sql(dialect=ctx.dialect)

    ctx.create_catalog(catalog_name)

    schema = ctx.schema("drop_schema_catalog_test", catalog_name)
    if ctx.engine_adapter.catalog_support.is_single_catalog_only:
        with pytest.raises(
            SQLMeshError, match="requires that all catalog operations be against a single catalog"
        ):
            drop_schema_and_validate(schema)
            create_objects_and_validate(schema)
        return
    drop_schema_and_validate(schema)
    create_objects_and_validate(schema)

    if ctx.is_remote:
        ctx.drop_catalog(catalog_name)


def test_temp_table(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    table = ctx.table("example")

    with ctx.engine_adapter.temp_table(
        ctx.input_data(input_data), table.sql(), table_format=ctx.default_table_format
    ) as table_name:
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.tables) == 1
        assert len(results.non_temp_tables) == 0
        assert len(results.materialized_views) == 0
        ctx.compare_with_current(table_name, input_data)

    results = ctx.get_metadata_results()
    assert len(results.views) == len(results.tables) == len(results.non_temp_tables) == 0


def test_create_table(ctx: TestContext):
    table = ctx.table("test_table")
    ctx.engine_adapter.create_table(
        table,
        {"id": exp.DataType.build("int")},
        table_description="test table description",
        column_descriptions={"id": "test id column description"},
        table_format=ctx.default_table_format,
    )
    results = ctx.get_metadata_results()
    assert len(results.tables) == 1
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert results.tables[0] == table.name

    if ctx.engine_adapter.COMMENT_CREATION_TABLE.is_supported:
        table_description = ctx.get_table_comment(table.db, "test_table")
        column_comments = ctx.get_column_comments(table.db, "test_table")
        assert table_description == "test table description"
        assert column_comments == {"id": "test id column description"}


def test_ctas(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    table = ctx.table("test_table")

    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.ctas(
        table,
        ctx.input_data(input_data),
        table_description="test table description",
        column_descriptions={"id": "test id column description"},
        table_format=ctx.default_table_format,
    )

    results = ctx.get_metadata_results(schema=table.db)
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    if ctx.engine_adapter.COMMENT_CREATION_TABLE.is_supported:
        table_description = ctx.get_table_comment(table.db, table.name)
        column_comments = ctx.get_column_comments(table.db, table.name)

        assert table_description == "test table description"
        assert column_comments == {"id": "test id column description"}

    # ensure we don't hit clickhouse INSERT with LIMIT 0 bug on CTAS
    if ctx.dialect == "clickhouse":
        ctx.engine_adapter.ctas(table, exp.select("1").limit(0))


def test_ctas_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    table = ctx.table("test_table")

    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")

    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01", "ignored_source": "ignored_value"},
            {"id": 2, "ds": "2022-01-02", "ignored_source": "ignored_value"},
            {"id": 3, "ds": "2022-01-03", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.ctas(
        table,
        ctx.input_data(input_data),
        table_description="test table description",
        column_descriptions={"id": "test id column description"},
        table_format=ctx.default_table_format,
        target_columns_to_types=columns_to_types,
        source_columns=["id", "ds", "ignored_source"],
    )

    expected_data = input_data.copy()
    expected_data["ignored_column"] = pd.Series()
    expected_data = expected_data.drop(columns=["ignored_source"])

    results = ctx.get_metadata_results(schema=table.db)
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, expected_data)

    if ctx.engine_adapter.COMMENT_CREATION_TABLE.is_supported:
        table_description = ctx.get_table_comment(table.db, table.name)
        column_comments = ctx.get_column_comments(table.db, table.name)

        assert table_description == "test table description"
        assert column_comments == {"id": "test id column description"}

    # ensure we don't hit clickhouse INSERT with LIMIT 0 bug on CTAS
    if ctx.dialect == "clickhouse":
        ctx.engine_adapter.ctas(table, exp.select("1").limit(0))


def test_create_view(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    view = ctx.table("test_view")
    ctx.engine_adapter.create_view(
        view,
        ctx.input_data(input_data),
        table_description="test view description",
        column_descriptions={"id": "test id column description"},
    )
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 1
    assert len(results.materialized_views) == 0
    assert results.views[0] == view.name
    ctx.compare_with_current(view, input_data)

    if ctx.engine_adapter.COMMENT_CREATION_VIEW.is_supported:
        table_description = ctx.get_table_comment(view.db, "test_view", table_kind="VIEW")
        column_comments = ctx.get_column_comments(view.db, "test_view", table_kind="VIEW")

        # Query:
        #   In the query test, columns_to_types are not available when the view is created. Since we
        #   can only register column comments in the CREATE VIEW schema expression with columns_to_types
        #   available, the column comments must be registered via post-creation commands. Some engines,
        #   such as Spark and Snowflake, do not support view column comments via post-creation commands.
        assert table_description == "test view description"
        assert column_comments == (
            {}
            if (
                ctx.test_type == "query"
                and not ctx.engine_adapter.COMMENT_CREATION_VIEW.supports_column_comment_commands
            )
            else {"id": "test id column description"}
        )


def test_create_view_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df

    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")

    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01", "ignored_source": "ignored_value"},
            {"id": 2, "ds": "2022-01-02", "ignored_source": "ignored_value"},
            {"id": 3, "ds": "2022-01-03", "ignored_source": "ignored_value"},
        ]
    )
    view = ctx.table("test_view")
    ctx.engine_adapter.create_view(
        view,
        ctx.input_data(input_data),
        table_description="test view description",
        column_descriptions={"id": "test id column description"},
        source_columns=["id", "ds", "ignored_source"],
        target_columns_to_types=columns_to_types,
    )

    expected_data = input_data.copy()
    expected_data["ignored_column"] = pd.Series()
    expected_data = expected_data.drop(columns=["ignored_source"])

    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 1
    assert len(results.materialized_views) == 0
    assert results.views[0] == view.name
    ctx.compare_with_current(view, expected_data)

    if ctx.engine_adapter.COMMENT_CREATION_VIEW.is_supported:
        table_description = ctx.get_table_comment(view.db, "test_view", table_kind="VIEW")
        column_comments = ctx.get_column_comments(view.db, "test_view", table_kind="VIEW")

        assert table_description == "test view description"
        assert column_comments == {"id": "test id column description"}


def test_materialized_view(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    if not ctx.engine_adapter.SUPPORTS_MATERIALIZED_VIEWS:
        pytest.skip(f"Engine adapter {ctx.engine_adapter} doesn't support materialized views")
    if ctx.engine_adapter.dialect == "databricks":
        pytest.skip(
            "Databricks requires DBSQL Serverless or Pro warehouse to test materialized views which we do not have setup"
        )
    if ctx.engine_adapter.dialect == "snowflake":
        pytest.skip("Snowflake requires enterprise edition which we do not have setup")
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    source_table = ctx.table("source_table")
    ctx.engine_adapter.ctas(source_table, ctx.input_data(input_data), ctx.columns_to_types)
    view = ctx.table("test_view")
    view_query = exp.select(*ctx.columns_to_types).from_(source_table)
    ctx.engine_adapter.create_view(view, view_query, materialized=True)
    results = ctx.get_metadata_results()
    # Redshift considers the underlying dataset supporting materialized views as a table therefore we get 2
    # tables in the result
    if ctx.engine_adapter.dialect == "redshift":
        assert len(results.tables) == 2
    else:
        assert len(results.tables) == 1
    assert len(results.views) == 0
    assert len(results.materialized_views) == 1
    assert results.materialized_views[0] == view.name
    ctx.compare_with_current(view, input_data)
    # Make sure that dropping a materialized view also works
    ctx.engine_adapter.drop_view(view, materialized=True)
    results = ctx.get_metadata_results()
    assert len(results.materialized_views) == 0


def test_drop_schema(ctx: TestContext):
    ctx.columns_to_types = {"one": "int"}
    schema = ctx.schema(TEST_SCHEMA)
    ctx.engine_adapter.drop_schema(schema, cascade=True)
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 0

    ctx.engine_adapter.create_schema(schema)
    view = ctx.table("test_view")
    view_query = exp.Select().select(exp.Literal.number(1).as_("one"))
    ctx.engine_adapter.create_view(view, view_query, ctx.columns_to_types)
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 1

    ctx.engine_adapter.drop_schema(schema, cascade=True)
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 0


def test_nan_roundtrip(ctx_df: TestContext):
    ctx = ctx_df
    ctx.engine_adapter.DEFAULT_BATCH_SIZE = sys.maxsize
    table = ctx.table("test_table")
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": np.nan, "ds": np.nan},
        ]
    )
    ctx.engine_adapter.create_table(table, ctx.columns_to_types)
    ctx.engine_adapter.replace_query(
        table,
        ctx.input_data(input_data),
        target_columns_to_types=ctx.columns_to_types,
    )
    results = ctx.get_metadata_results()
    assert not results.views
    assert not results.materialized_views
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)


def test_replace_query(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    ctx.engine_adapter.DEFAULT_BATCH_SIZE = sys.maxsize
    table = ctx.table("test_table")
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.create_table(
        table, ctx.columns_to_types, table_format=ctx.default_table_format
    )
    ctx.engine_adapter.replace_query(
        table,
        ctx.input_data(input_data),
        # Spark based engines do a create table -> insert overwrite instead of replace. If columns to types aren't
        # provided then it checks the table itself for types. This is fine within SQLMesh since we always know the tables
        # exist prior to evaluation but when running these tests that isn't the case. As a result we just pass in
        # columns_to_types for these two engines so we can still test inference on the other ones
        target_columns_to_types=ctx.columns_to_types
        if ctx.dialect in ["spark", "databricks"]
        else None,
        table_format=ctx.default_table_format,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    # Replace that we only need to run once
    if type == "df":
        replace_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
                {"id": 6, "ds": "2022-01-06"},
            ]
        )
        ctx.engine_adapter.replace_query(
            table,
            ctx.input_data(replace_data),
            target_columns_to_types=(
                ctx.columns_to_types if ctx.dialect in ["spark", "databricks"] else None
            ),
            table_format=ctx.default_table_format,
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, replace_data)


def test_replace_query_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    ctx.engine_adapter.DEFAULT_BATCH_SIZE = sys.maxsize
    table = ctx.table("test_table")

    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")

    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01", "ignored_source": "ignored_value"},
            {"id": 2, "ds": "2022-01-02", "ignored_source": "ignored_value"},
            {"id": 3, "ds": "2022-01-03", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.create_table(table, columns_to_types, table_format=ctx.default_table_format)
    ctx.engine_adapter.replace_query(
        table,
        ctx.input_data(input_data),
        table_format=ctx.default_table_format,
        source_columns=["id", "ds", "ignored_source"],
        target_columns_to_types=columns_to_types,
    )
    expected_data = input_data.copy()
    expected_data["ignored_column"] = pd.Series()
    expected_data = expected_data.drop(columns=["ignored_source"])

    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, expected_data)

    # Replace that we only need to run once
    if type == "df":
        replace_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
                {"id": 6, "ds": "2022-01-06"},
            ]
        )
        ctx.engine_adapter.replace_query(
            table,
            ctx.input_data(replace_data),
            table_format=ctx.default_table_format,
            source_columns=["id", "ds"],
            target_columns_to_types=columns_to_types,
        )
        expected_data = replace_data.copy()
        expected_data["ignored_column"] = pd.Series()

        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, expected_data)


def test_replace_query_batched(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    ctx.engine_adapter.DEFAULT_BATCH_SIZE = 1
    table = ctx.table("test_table")
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.create_table(
        table, ctx.columns_to_types, table_format=ctx.default_table_format
    )
    ctx.engine_adapter.replace_query(
        table,
        ctx.input_data(input_data),
        # Spark based engines do a create table -> insert overwrite instead of replace. If columns to types aren't
        # provided then it checks the table itself for types. This is fine within SQLMesh since we always know the tables
        # exist prior to evaluation but when running these tests that isn't the case. As a result we just pass in
        # columns_to_types for these two engines so we can still test inference on the other ones
        target_columns_to_types=ctx.columns_to_types
        if ctx.dialect in ["spark", "databricks"]
        else None,
        table_format=ctx.default_table_format,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    # Replace that we only need to run once
    if ctx.test_type == "df":
        replace_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
                {"id": 6, "ds": "2022-01-06"},
            ]
        )
        ctx.engine_adapter.replace_query(
            table,
            ctx.input_data(replace_data),
            target_columns_to_types=(
                ctx.columns_to_types if ctx.dialect in ["spark", "databricks"] else None
            ),
            table_format=ctx.default_table_format,
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, replace_data)


def test_insert_append(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    table = ctx.table("test_table")
    ctx.engine_adapter.create_table(
        table, ctx.columns_to_types, table_format=ctx.default_table_format
    )
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_append(table, ctx.input_data(input_data))
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    # Replace that we only need to run once
    if ctx.test_type == "df":
        append_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
                {"id": 6, "ds": "2022-01-06"},
            ]
        )
        ctx.engine_adapter.insert_append(table, ctx.input_data(append_data))
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) in [1, 2, 3]
        assert len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, pd.concat([input_data, append_data]))


def test_insert_append_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    table = ctx.table("test_table")
    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")
    ctx.engine_adapter.create_table(table, columns_to_types, table_format=ctx.default_table_format)
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01", "ignored_source": "ignored_value"},
            {"id": 2, "ds": "2022-01-02", "ignored_source": "ignored_value"},
            {"id": 3, "ds": "2022-01-03", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.insert_append(
        table,
        ctx.input_data(input_data),
        source_columns=["id", "ds", "ignored_source"],
        target_columns_to_types=columns_to_types,
    )
    expected_data = input_data.copy()
    expected_data["ignored_column"] = pd.Series()
    expected_data = expected_data.drop(columns=["ignored_source"])

    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, expected_data)

    # Replace that we only need to run once
    if ctx.test_type == "df":
        append_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04", "ignored_source": "ignored_value"},
                {"id": 5, "ds": "2022-01-05", "ignored_source": "ignored_value"},
                {"id": 6, "ds": "2022-01-06", "ignored_source": "ignored_value"},
            ]
        )
        ctx.engine_adapter.insert_append(
            table,
            ctx.input_data(append_data),
            source_columns=["id", "ds", "ignored_source"],
            target_columns_to_types=columns_to_types,
        )
        append_expected_data = append_data.copy()
        append_expected_data["ignored_column"] = pd.Series()
        append_expected_data = append_expected_data.drop(columns=["ignored_source"])

        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) in [1, 2, 3]
        assert len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, pd.concat([expected_data, append_expected_data]))


def test_insert_overwrite_by_time_partition(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    ds_type = "string"
    if ctx.dialect == "bigquery":
        ds_type = "datetime"
    if ctx.dialect == "tsql":
        ds_type = "varchar(max)"
    if ctx.dialect == "starrocks":
        ds_type = "datetime"

    ctx.columns_to_types = {"id": "int", "ds": ds_type}
    table = ctx.table("test_table")
    if ctx.dialect == "bigquery":
        partitioned_by = ["DATE(ds)"]
    else:
        partitioned_by = ctx.partitioned_by  # type: ignore
    ctx.engine_adapter.create_table(
        table,
        ctx.columns_to_types,
        partitioned_by=partitioned_by,
        partition_interval_unit="DAY",
        table_format=ctx.default_table_format,
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, ctx.time_column: "2022-01-01"},
            {"id": 2, ctx.time_column: "2022-01-02"},
            {"id": 3, ctx.time_column: "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_overwrite_by_time_partition(
        table,
        ctx.input_data(input_data),
        start="2022-01-02",
        end="2022-01-03",
        time_formatter=ctx.time_formatter,
        time_column=ctx.time_column,
        target_columns_to_types=ctx.columns_to_types,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name

    if ctx.dialect == "trino":
        # trino has some lag between partitions being registered and data showing up
        wait_until(lambda: len(ctx.get_current_data(table)) > 0)

    ctx.compare_with_current(table, input_data.iloc[1:])

    if ctx.test_type == "df":
        overwrite_data = pd.DataFrame(
            [
                {"id": 10, ctx.time_column: "2022-01-03"},
                {"id": 4, ctx.time_column: "2022-01-04"},
                {"id": 5, ctx.time_column: "2022-01-05"},
            ]
        )
        ctx.engine_adapter.insert_overwrite_by_time_partition(
            table,
            ctx.input_data(overwrite_data),
            start="2022-01-03",
            end="2022-01-05",
            time_formatter=ctx.time_formatter,
            time_column=ctx.time_column,
            target_columns_to_types=ctx.columns_to_types,
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name

        if ctx.dialect == "trino":
            wait_until(lambda: len(ctx.get_current_data(table)) > 2)

        ctx.compare_with_current(
            table,
            pd.DataFrame(
                [
                    {"id": 2, ctx.time_column: "2022-01-02"},
                    {"id": 10, ctx.time_column: "2022-01-03"},
                    {"id": 4, ctx.time_column: "2022-01-04"},
                    {"id": 5, ctx.time_column: "2022-01-05"},
                ]
            ),
        )


def test_insert_overwrite_by_time_partition_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    ds_type = "string"
    if ctx.dialect == "bigquery":
        ds_type = "datetime"
    if ctx.dialect == "tsql":
        ds_type = "varchar(max)"
    if ctx.dialect == "starrocks":
        ds_type = "datetime"

    ctx.columns_to_types = {"id": "int", "ds": ds_type}
    columns_to_types = {
        "id": exp.DataType.build("int"),
        "ignored_column": exp.DataType.build("int"),
        "ds": exp.DataType.build(ds_type),
    }
    table = ctx.table("test_table")
    if ctx.dialect == "bigquery":
        partitioned_by = ["DATE(ds)"]
    else:
        partitioned_by = ctx.partitioned_by  # type: ignore
    ctx.engine_adapter.create_table(
        table,
        columns_to_types,
        partitioned_by=partitioned_by,
        partition_interval_unit="DAY",
        table_format=ctx.default_table_format,
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, ctx.time_column: "2022-01-01", "ignored_source": "ignored_value"},
            {"id": 2, ctx.time_column: "2022-01-02", "ignored_source": "ignored_value"},
            {"id": 3, ctx.time_column: "2022-01-03", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.insert_overwrite_by_time_partition(
        table,
        ctx.input_data(input_data),
        start="2022-01-02",
        end="2022-01-03",
        time_formatter=ctx.time_formatter,
        time_column=ctx.time_column,
        target_columns_to_types=columns_to_types,
        source_columns=["id", "ds", "ignored_source"],
    )

    expected_data = input_data.copy()
    expected_data = expected_data.drop(columns=["ignored_source"])
    expected_data.insert(len(expected_data.columns) - 1, "ignored_column", pd.Series())

    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name

    if ctx.dialect == "trino":
        # trino has some lag between partitions being registered and data showing up
        wait_until(lambda: len(ctx.get_current_data(table)) > 0)

    ctx.compare_with_current(table, expected_data.iloc[1:])

    if ctx.test_type == "df":
        overwrite_data = pd.DataFrame(
            [
                {"id": 10, ctx.time_column: "2022-01-03", "ignored_source": "ignored_value"},
                {"id": 4, ctx.time_column: "2022-01-04", "ignored_source": "ignored_value"},
                {"id": 5, ctx.time_column: "2022-01-05", "ignored_source": "ignored_value"},
            ]
        )
        ctx.engine_adapter.insert_overwrite_by_time_partition(
            table,
            ctx.input_data(overwrite_data),
            start="2022-01-03",
            end="2022-01-05",
            time_formatter=ctx.time_formatter,
            time_column=ctx.time_column,
            target_columns_to_types=columns_to_types,
            source_columns=["id", "ds", "ignored_source"],
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name

        if ctx.dialect == "trino":
            wait_until(lambda: len(ctx.get_current_data(table)) > 2)

        ctx.compare_with_current(
            table,
            pd.DataFrame(
                [
                    {"id": 2, "ignored_column": None, ctx.time_column: "2022-01-02"},
                    {"id": 10, "ignored_column": None, ctx.time_column: "2022-01-03"},
                    {"id": 4, "ignored_column": None, ctx.time_column: "2022-01-04"},
                    {"id": 5, "ignored_column": None, ctx.time_column: "2022-01-05"},
                ]
            ),
        )


def test_merge(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    if not ctx.supports_merge:
        pytest.skip(f"{ctx.dialect} doesn't support merge")

    table = ctx.table("test_table")

    # Athena only supports MERGE on Iceberg tables
    # And it cant fall back to a logical merge on Hive tables because it cant delete records
    table_format = "iceberg" if ctx.dialect == "athena" else None

    ctx.engine_adapter.create_table(table, ctx.columns_to_types, table_format=table_format)
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
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    if ctx.test_type == "df":
        merge_data = pd.DataFrame(
            [
                {"id": 2, "ds": "2022-01-10"},
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
            ]
        )
        ctx.engine_adapter.merge(
            table,
            ctx.input_data(merge_data),
            target_columns_to_types=None,
            unique_key=[exp.to_identifier("id")],
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(
            table,
            pd.DataFrame(
                [
                    {"id": 1, "ds": "2022-01-01"},
                    {"id": 2, "ds": "2022-01-10"},
                    {"id": 3, "ds": "2022-01-03"},
                    {"id": 4, "ds": "2022-01-04"},
                    {"id": 5, "ds": "2022-01-05"},
                ]
            ),
        )


def test_merge_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    if not ctx.supports_merge:
        pytest.skip(f"{ctx.dialect} doesn't support merge")

    table = ctx.table("test_table")

    # Athena only supports MERGE on Iceberg tables
    # And it cant fall back to a logical merge on Hive tables because it cant delete records
    table_format = "iceberg" if ctx.dialect == "athena" else None

    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")

    ctx.engine_adapter.create_table(table, columns_to_types, table_format=table_format)
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01", "ignored_source": "ignored_value"},
            {"id": 2, "ds": "2022-01-02", "ignored_source": "ignored_value"},
            {"id": 3, "ds": "2022-01-03", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.merge(
        table,
        ctx.input_data(input_data),
        unique_key=[exp.to_identifier("id")],
        target_columns_to_types=columns_to_types,
        source_columns=["id", "ds", "ignored_source"],
    )

    expected_data = input_data.copy()
    expected_data["ignored_column"] = pd.Series()
    expected_data = expected_data.drop(columns=["ignored_source"])

    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, expected_data)

    if ctx.test_type == "df":
        merge_data = pd.DataFrame(
            [
                {"id": 2, "ds": "2022-01-10", "ignored_source": "ignored_value"},
                {"id": 4, "ds": "2022-01-04", "ignored_source": "ignored_value"},
                {"id": 5, "ds": "2022-01-05", "ignored_source": "ignored_value"},
            ]
        )
        ctx.engine_adapter.merge(
            table,
            ctx.input_data(merge_data),
            unique_key=[exp.to_identifier("id")],
            target_columns_to_types=columns_to_types,
            source_columns=["id", "ds", "ignored_source"],
        )

        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(
            table,
            pd.DataFrame(
                [
                    {"id": 1, "ds": "2022-01-01", "ignored_column": None},
                    {"id": 2, "ds": "2022-01-10", "ignored_column": None},
                    {"id": 3, "ds": "2022-01-03", "ignored_column": None},
                    {"id": 4, "ds": "2022-01-04", "ignored_column": None},
                    {"id": 5, "ds": "2022-01-05", "ignored_column": None},
                ]
            ),
        )


def test_scd_type_2_by_time(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    # Athena only supports the operations required for SCD models on Iceberg tables
    if ctx.mark == "athena_hive":
        pytest.skip("SCD Type 2 is only supported on Athena / Iceberg")

    time_type = exp.DataType.build("timestamp")

    ctx.columns_to_types = {
        "id": "int",
        "name": "string",
        "updated_at": time_type,
        "valid_from": time_type,
        "valid_to": time_type,
    }
    table = ctx.table("test_table")
    input_schema = {
        k: v for k, v in ctx.columns_to_types.items() if k not in ("valid_from", "valid_to")
    }

    ctx.engine_adapter.create_table(
        table, ctx.columns_to_types, table_format=ctx.default_table_format
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, "name": "a", "updated_at": "2022-01-01 00:00:00"},
            {"id": 2, "name": "b", "updated_at": "2022-01-02 00:00:00"},
            {"id": 3, "name": "c", "updated_at": "2022-01-03 00:00:00"},
        ]
    )
    ctx.engine_adapter.scd_type_2_by_time(
        table,
        ctx.input_data(input_data, input_schema),
        unique_key=[parse_one("COALESCE(id, -1)")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        execution_time="2023-01-01 00:00:00",
        updated_at_as_valid_from=False,
        target_columns_to_types=input_schema,
        table_format=ctx.default_table_format,
        truncate=True,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "updated_at": "2022-01-01 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 2,
                    "name": "b",
                    "updated_at": "2022-01-02 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 3,
                    "name": "c",
                    "updated_at": "2022-01-03 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
            ]
        ),
    )

    if ctx.test_type == "query":
        return

    current_data = pd.DataFrame(
        [
            # Change `a` to `x`
            {"id": 1, "name": "x", "updated_at": "2022-01-04 00:00:00"},
            # Delete
            # {"id": 2, "name": "b", "updated_at": "2022-01-02 00:00:00"},
            # No change
            {"id": 3, "name": "c", "updated_at": "2022-01-03 00:00:00"},
            # Add
            {"id": 4, "name": "d", "updated_at": "2022-01-04 00:00:00"},
        ]
    )
    ctx.engine_adapter.scd_type_2_by_time(
        table,
        ctx.input_data(current_data, input_schema),
        unique_key=[exp.to_column("id")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        execution_time="2023-01-05 00:00:00",
        updated_at_as_valid_from=False,
        target_columns_to_types=input_schema,
        table_format=ctx.default_table_format,
        truncate=False,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "updated_at": "2022-01-01 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2022-01-04 00:00:00",
                },
                {
                    "id": 1,
                    "name": "x",
                    "updated_at": "2022-01-04 00:00:00",
                    "valid_from": "2022-01-04 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 2,
                    "name": "b",
                    "updated_at": "2022-01-02 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                },
                {
                    "id": 3,
                    "name": "c",
                    "updated_at": "2022-01-03 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 4,
                    "name": "d",
                    "updated_at": "2022-01-04 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
            ]
        ),
    )


def test_scd_type_2_by_time_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    # Athena only supports the operations required for SCD models on Iceberg tables
    if ctx.mark == "athena_hive":
        pytest.skip("SCD Type 2 is only supported on Athena / Iceberg")

    time_type = exp.DataType.build("timestamp")

    ctx.columns_to_types = {
        "id": "int",
        "name": "string",
        "updated_at": time_type,
        "valid_from": time_type,
        "valid_to": time_type,
    }
    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")

    table = ctx.table("test_table")
    input_schema = {
        k: v for k, v in ctx.columns_to_types.items() if k not in ("valid_from", "valid_to")
    }

    ctx.engine_adapter.create_table(table, columns_to_types, table_format=ctx.default_table_format)
    input_data = pd.DataFrame(
        [
            {
                "id": 1,
                "name": "a",
                "updated_at": "2022-01-01 00:00:00",
                "ignored_source": "ignored_value",
            },
            {
                "id": 2,
                "name": "b",
                "updated_at": "2022-01-02 00:00:00",
                "ignored_source": "ignored_value",
            },
            {
                "id": 3,
                "name": "c",
                "updated_at": "2022-01-03 00:00:00",
                "ignored_source": "ignored_value",
            },
        ]
    )
    ctx.engine_adapter.scd_type_2_by_time(
        table,
        ctx.input_data(input_data, input_schema),
        unique_key=[parse_one("COALESCE(id, -1)")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        execution_time="2023-01-01 00:00:00",
        updated_at_as_valid_from=False,
        table_format=ctx.default_table_format,
        truncate=True,
        start="2022-01-01 00:00:00",
        target_columns_to_types=columns_to_types,
        source_columns=["id", "name", "updated_at", "ignored_source"],
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "updated_at": "2022-01-01 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 2,
                    "name": "b",
                    "updated_at": "2022-01-02 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 3,
                    "name": "c",
                    "updated_at": "2022-01-03 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
            ]
        ),
    )

    if ctx.test_type == "query":
        return

    current_data = pd.DataFrame(
        [
            # Change `a` to `x`
            {
                "id": 1,
                "name": "x",
                "updated_at": "2022-01-04 00:00:00",
                "ignored_source": "ignored_value",
            },
            # Delete
            # {"id": 2, "name": "b", "updated_at": "2022-01-02 00:00:00", "ignored_source": "ignored_value"},
            # No change
            {
                "id": 3,
                "name": "c",
                "updated_at": "2022-01-03 00:00:00",
                "ignored_source": "ignored_value",
            },
            # Add
            {
                "id": 4,
                "name": "d",
                "updated_at": "2022-01-04 00:00:00",
                "ignored_source": "ignored_value",
            },
        ]
    )
    ctx.engine_adapter.scd_type_2_by_time(
        table,
        ctx.input_data(current_data, input_schema),
        unique_key=[exp.to_column("id")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        updated_at_col=exp.column("updated_at", quoted=True),
        execution_time="2023-01-05 00:00:00",
        updated_at_as_valid_from=False,
        table_format=ctx.default_table_format,
        truncate=False,
        start="2022-01-01 00:00:00",
        target_columns_to_types=columns_to_types,
        source_columns=["id", "name", "updated_at", "ignored_source"],
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "updated_at": "2022-01-01 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2022-01-04 00:00:00",
                    "ignored_column": None,
                },
                {
                    "id": 1,
                    "name": "x",
                    "updated_at": "2022-01-04 00:00:00",
                    "valid_from": "2022-01-04 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 2,
                    "name": "b",
                    "updated_at": "2022-01-02 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                    "ignored_column": None,
                },
                {
                    "id": 3,
                    "name": "c",
                    "updated_at": "2022-01-03 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 4,
                    "name": "d",
                    "updated_at": "2022-01-04 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
            ]
        ),
    )


def test_scd_type_2_by_column(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    # Athena only supports the operations required for SCD models on Iceberg tables
    if ctx.mark == "athena_hive":
        pytest.skip("SCD Type 2 is only supported on Athena / Iceberg")

    time_type = exp.DataType.build("timestamp")

    ctx.columns_to_types = {
        "id": "int",
        "name": "string",
        "status": "string",
        "valid_from": time_type,
        "valid_to": time_type,
    }
    table = ctx.table("test_table")
    input_schema = {
        k: v for k, v in ctx.columns_to_types.items() if k not in ("valid_from", "valid_to")
    }

    ctx.engine_adapter.create_table(
        table, ctx.columns_to_types, table_format=ctx.default_table_format
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, "name": "a", "status": "active"},
            {"id": 2, "name": "b", "status": "inactive"},
            {"id": 3, "name": "c", "status": "active"},
            {"id": 4, "name": "d", "status": "active"},
        ]
    )
    ctx.engine_adapter.scd_type_2_by_column(
        table,
        ctx.input_data(input_data, input_schema),
        unique_key=[exp.to_column("id")],
        check_columns=[exp.to_column("name"), exp.to_column("status")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        execution_time="2023-01-01",
        execution_time_as_valid_from=False,
        target_columns_to_types=ctx.columns_to_types,
        truncate=True,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 2,
                    "name": "b",
                    "status": "inactive",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 3,
                    "name": "c",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 4,
                    "name": "d",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
            ]
        ),
    )

    if ctx.test_type == "query":
        return

    current_data = pd.DataFrame(
        [
            # Change `a` to `x`
            {"id": 1, "name": "x", "status": "active"},
            # Delete
            # {"id": 2, "name": "b", status: "inactive"},
            # No change
            {"id": 3, "name": "c", "status": "active"},
            # Change status to inactive
            {"id": 4, "name": "d", "status": "inactive"},
            # Add
            {"id": 5, "name": "e", "status": "inactive"},
        ]
    )
    ctx.engine_adapter.scd_type_2_by_column(
        table,
        ctx.input_data(current_data, input_schema),
        unique_key=[exp.to_column("id")],
        check_columns=[exp.to_column("name"), exp.to_column("status")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        execution_time="2023-01-05 00:00:00",
        execution_time_as_valid_from=False,
        target_columns_to_types=ctx.columns_to_types,
        truncate=False,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                },
                {
                    "id": 1,
                    "name": "x",
                    "status": "active",
                    "valid_from": "2023-01-05 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 2,
                    "name": "b",
                    "status": "inactive",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                },
                {
                    "id": 3,
                    "name": "c",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 4,
                    "name": "d",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                },
                {
                    "id": 4,
                    "name": "d",
                    "status": "inactive",
                    "valid_from": "2023-01-05 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 5,
                    "name": "e",
                    "status": "inactive",
                    "valid_from": "2023-01-05 00:00:00",
                    "valid_to": pd.NaT,
                },
            ]
        ),
    )


def test_scd_type_2_by_column_source_columns(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    # Athena only supports the operations required for SCD models on Iceberg tables
    if ctx.mark == "athena_hive":
        pytest.skip("SCD Type 2 is only supported on Athena / Iceberg")

    time_type = exp.DataType.build("timestamp")

    ctx.columns_to_types = {
        "id": "int",
        "name": "string",
        "status": "string",
        "valid_from": time_type,
        "valid_to": time_type,
    }
    columns_to_types = ctx.columns_to_types.copy()
    columns_to_types["ignored_column"] = exp.DataType.build("int")

    table = ctx.table("test_table")
    input_schema = {
        k: v for k, v in ctx.columns_to_types.items() if k not in ("valid_from", "valid_to")
    }

    ctx.engine_adapter.create_table(table, columns_to_types, table_format=ctx.default_table_format)
    input_data = pd.DataFrame(
        [
            {"id": 1, "name": "a", "status": "active", "ignored_source": "ignored_value"},
            {"id": 2, "name": "b", "status": "inactive", "ignored_source": "ignored_value"},
            {"id": 3, "name": "c", "status": "active", "ignored_source": "ignored_value"},
            {"id": 4, "name": "d", "status": "active", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.scd_type_2_by_column(
        table,
        ctx.input_data(input_data, input_schema),
        unique_key=[exp.to_column("id")],
        check_columns=[exp.to_column("name"), exp.to_column("status")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        execution_time="2023-01-01",
        execution_time_as_valid_from=False,
        truncate=True,
        start="2023-01-01",
        target_columns_to_types=columns_to_types,
        source_columns=["id", "name", "status", "ignored_source"],
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 2,
                    "name": "b",
                    "status": "inactive",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 3,
                    "name": "c",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 4,
                    "name": "d",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
            ]
        ),
    )

    if ctx.test_type == "query":
        return

    current_data = pd.DataFrame(
        [
            # Change `a` to `x`
            {"id": 1, "name": "x", "status": "active", "ignored_source": "ignored_value"},
            # Delete
            # {"id": 2, "name": "b", status: "inactive", "ignored_source": "ignored_value"},
            # No change
            {"id": 3, "name": "c", "status": "active", "ignored_source": "ignored_value"},
            # Change status to inactive
            {"id": 4, "name": "d", "status": "inactive", "ignored_source": "ignored_value"},
            # Add
            {"id": 5, "name": "e", "status": "inactive", "ignored_source": "ignored_value"},
        ]
    )
    ctx.engine_adapter.scd_type_2_by_column(
        table,
        ctx.input_data(current_data, input_schema),
        unique_key=[exp.to_column("id")],
        check_columns=[exp.to_column("name"), exp.to_column("status")],
        valid_from_col=exp.column("valid_from", quoted=True),
        valid_to_col=exp.column("valid_to", quoted=True),
        execution_time="2023-01-05 00:00:00",
        execution_time_as_valid_from=False,
        truncate=False,
        start="2023-01-01",
        target_columns_to_types=columns_to_types,
        source_columns=["id", "name", "status", "ignored_source"],
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                    "ignored_column": None,
                },
                {
                    "id": 1,
                    "name": "x",
                    "status": "active",
                    "valid_from": "2023-01-05 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 2,
                    "name": "b",
                    "status": "inactive",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                    "ignored_column": None,
                },
                {
                    "id": 3,
                    "name": "c",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 4,
                    "name": "d",
                    "status": "active",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                    "ignored_column": None,
                },
                {
                    "id": 4,
                    "name": "d",
                    "status": "inactive",
                    "valid_from": "2023-01-05 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
                {
                    "id": 5,
                    "name": "e",
                    "status": "inactive",
                    "valid_from": "2023-01-05 00:00:00",
                    "valid_to": pd.NaT,
                    "ignored_column": None,
                },
            ]
        ),
    )


def test_get_data_objects(ctx_query_and_df: TestContext):
    ctx = ctx_query_and_df
    table = ctx.table("test_table")
    view = ctx.table("test_view")
    ctx.engine_adapter.create_table(
        table,
        {"id": exp.DataType.build("int")},
        table_description="test table description",
        column_descriptions={"id": "test id column description"},
        table_format=ctx.default_table_format,
    )
    ctx.engine_adapter.create_view(
        view,
        ctx.input_data(pd.DataFrame([{"id": 1, "ds": "2022-01-01"}])),
        table_description="test view description",
        column_descriptions={"id": "test id column description"},
    )

    schema = ctx.schema(TEST_SCHEMA)

    assert sorted(ctx.engine_adapter.get_data_objects(schema), key=lambda o: o.name) == [
        DataObject(
            name=table.name,
            schema=table.db,
            catalog=table.catalog or None,
            type=DataObjectType.TABLE,
        ),
        DataObject(
            name=view.name,
            schema=view.db,
            catalog=view.catalog or None,
            type=DataObjectType.VIEW,
        ),
    ]

    assert sorted(
        ctx.engine_adapter.get_data_objects(schema, {table.name, view.name}),
        key=lambda o: o.name,
    ) == [
        DataObject(
            name=table.name,
            schema=table.db,
            catalog=table.catalog or None,
            type=DataObjectType.TABLE,
        ),
        DataObject(
            name=view.name,
            schema=view.db,
            catalog=view.catalog or None,
            type=DataObjectType.VIEW,
        ),
    ]

    assert ctx.engine_adapter.get_data_objects(schema, {table.name}) == [
        DataObject(
            name=table.name,
            schema=table.db,
            catalog=table.catalog or None,
            type=DataObjectType.TABLE,
        ),
    ]

    assert ctx.engine_adapter.get_data_objects(schema, {view.name}) == [
        DataObject(
            name=view.name,
            schema=view.db,
            catalog=view.catalog or None,
            type=DataObjectType.VIEW,
        ),
    ]

    assert ctx.engine_adapter.get_data_objects(schema, {}) == []
    assert ctx.engine_adapter.get_data_objects("missing_schema") == []


def test_truncate_table(ctx: TestContext):
    table = ctx.table("test_table")

    ctx.engine_adapter.create_table(
        table, ctx.columns_to_types, table_format=ctx.default_table_format
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_append(table, ctx.input_data(input_data))
    ctx.compare_with_current(table, input_data)
    ctx.engine_adapter._truncate_table(table)
    assert ctx.engine_adapter.fetchone(exp.select("count(*)").from_(table))[0] == 0


def test_transaction(ctx: TestContext):
    if ctx.engine_adapter.SUPPORTS_TRANSACTIONS is False:
        pytest.skip(f"Engine adapter {ctx.engine_adapter.dialect} doesn't support transactions")

    table = ctx.table("test_table")
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    with ctx.engine_adapter.transaction():
        ctx.engine_adapter.create_table(table, ctx.columns_to_types)
        ctx.engine_adapter.insert_append(
            table, ctx.input_data(input_data, ctx.columns_to_types), ctx.columns_to_types
        )
    ctx.compare_with_current(table, input_data)
    with ctx.engine_adapter.transaction():
        ctx.engine_adapter._truncate_table(table)
        ctx.engine_adapter._connection_pool.rollback()
    ctx.compare_with_current(table, input_data)


@pytest.mark.parametrize(
    "virtual_environment_mode", [VirtualEnvironmentMode.FULL, VirtualEnvironmentMode.DEV_ONLY]
)
def test_sushi(
    ctx: TestContext, tmp_path: pathlib.Path, virtual_environment_mode: VirtualEnvironmentMode
):
    if ctx.mark == "athena_hive":
        pytest.skip(
            "Sushi end-to-end tests only need to run once for Athena because sushi needs a hybrid of both Hive and Iceberg"
        )

    sushi_test_schema = ctx.add_test_suffix("sushi")
    sushi_state_schema = ctx.add_test_suffix("sushi_state")
    raw_test_schema = ctx.add_test_suffix("raw")

    # Copy sushi example to tmpdir
    shutil.copytree(pathlib.Path("./examples/sushi"), tmp_path, dirs_exist_ok=True)

    # Rewrite schema references to test schema references
    # Note that we deliberately do it at the filesystem level instead of messing with the Context to ensure
    # that we are testing an actual Context rather than a doctored one
    extensions = ["*.sql", "*.yaml", "*.py"]
    replacements = {
        "sushi.": f"{sushi_test_schema}.",
        'sushi".': f'{sushi_test_schema}".',
        " raw.": f" {raw_test_schema}.",
        "NOT EXISTS raw;": f" NOT EXISTS {raw_test_schema};",
    }
    for ext in extensions:
        for f in tmp_path.rglob(ext):
            if f.is_file():
                contents = f.read_text()
                for search, replace in replacements.items():
                    contents = contents.replace(search, replace)
                f.write_text(contents)

    before_all = [
        f"CREATE SCHEMA IF NOT EXISTS {raw_test_schema}",
        f"DROP VIEW IF EXISTS {raw_test_schema}.demographics",
        f"CREATE VIEW {raw_test_schema}.demographics AS (SELECT 1 AS customer_id, '00000' AS zip)",
    ]

    def _mutate_config(gateway: str, config: Config) -> None:
        config.gateways[gateway].state_schema = sushi_state_schema
        config.before_all = [
            quote_identifiers(
                parse_one(e, dialect=config.model_defaults.dialect),
                dialect=config.model_defaults.dialect,
            ).sql(dialect=config.model_defaults.dialect)
            for e in before_all
        ]
        config.virtual_environment_mode = virtual_environment_mode

    context = ctx.create_context(_mutate_config, path=tmp_path, ephemeral_state_connection=False)

    end = now()
    start = to_date(end - timedelta(days=7))
    yesterday = to_date(end - timedelta(days=1))

    # Databricks requires the table property `delta.columnMapping.mode = 'name'` for
    # spaces in column names. Other engines error if it is set in the model definition,
    # so we set it here.
    if ctx.dialect == "databricks":
        cust_rev_by_day_key = [key for key in context._models if "customer_revenue_by_day" in key][
            0
        ]

        cust_rev_by_day_model_tbl_props = context._models[cust_rev_by_day_key].copy(
            update={
                "physical_properties": {
                    "delta.columnMapping.mode": exp.Literal(this="name", is_string=True)
                }
            }
        )

        context._models.update({cust_rev_by_day_key: cust_rev_by_day_model_tbl_props})

    # Clickhouse requires columns used as keys to be non-Nullable, but all transpiled columns are nullable by default
    if ctx.dialect == "clickhouse":
        models_to_modify = {
            '"items"',
            '"orders"',
            '"order_items"',
            '"customer_revenue_by_day"',
            '"customer_revenue_lifetime"',
            '"waiter_revenue_by_day"',
            '"waiter_as_customer_by_day"',
        }
        for model in models_to_modify:
            model_key = [key for key in context._models if model in key][0]
            model_columns = context._models[model_key].columns_to_types
            updated_model_columns = {
                k: exp.DataType.build(v.sql(), dialect="clickhouse", nullable=False)
                for k, v in model_columns.items()
            }

            model_ch_cols_to_types = context._models[model_key].copy(
                update={
                    "columns_to_types": updated_model_columns,
                    "columns_to_types_": updated_model_columns,
                    "columns_to_types_or_raise": updated_model_columns,
                }
            )
            context._models.update({model_key: model_ch_cols_to_types})

        # create raw schema and view
        if ctx.gateway == "inttest_clickhouse_cluster":
            context.engine_adapter.execute(
                f"CREATE DATABASE IF NOT EXISTS {raw_test_schema} ON CLUSTER cluster1;"
            )
            context.engine_adapter.execute(
                f"DROP VIEW IF EXISTS {raw_test_schema}.demographics ON CLUSTER cluster1;"
            )
            context.engine_adapter.execute(
                f"CREATE VIEW {raw_test_schema}.demographics ON CLUSTER cluster1 AS SELECT 1 AS customer_id, '00000' AS zip;"
            )

    # DuckDB parses TIMESTAMP into Type.TIMESTAMPNTZ which generates into TIMESTAMP_NTZ for
    # Spark, but this type is not supported in Spark's DDL statements so we make it a TIMESTAMP
    if ctx.dialect == "spark":
        for model_key, model in context._models.items():
            model_columns = model.columns_to_types

            updated_model_columns = {}
            for k, v in model_columns.items():
                updated_model_columns[k] = v
                if v.this == exp.DataType.Type.TIMESTAMPNTZ:
                    v.set("this", exp.DataType.Type.TIMESTAMP)

            update_fields = {
                "columns_to_types": updated_model_columns,
                "columns_to_types_": updated_model_columns,
                "columns_to_types_or_raise": updated_model_columns,
            }

            # We get rid of the sushi.marketing post statement here because it asserts that
            # updated_at is a 'timestamp', which is parsed using duckdb in assert_has_columns
            # and the assertion fails because we now have TIMESTAMPs and not TIMESTAMPNTZs in
            # the columns_to_types mapping
            if '"marketing"' in model_key:
                update_fields["post_statements_"] = []

            context._models.update(
                {model_key: context._models[model_key].copy(update=update_fields)}
            )

    if ctx.dialect == "athena":
        for model_name in {"customer_revenue_lifetime"}:
            model_key = next(k for k in context._models if model_name in k)
            model = context._models[model_key].copy(
                update={"table_format": ctx.default_table_format}
            )
            context._models.update({model_key: model})

    plan: Plan = context.plan(
        environment="test_prod",
        start=start,
        end=end,
        skip_tests=True,
        no_prompts=True,
        auto_apply=True,
    )

    data_validator = SushiDataValidator.from_context(context, sushi_schema_name=sushi_test_schema)
    data_validator.validate(
        f"{sushi_test_schema}.customer_revenue_lifetime",
        start,
        yesterday,
        env_name="test_prod",
        dialect=ctx.dialect,
        environment_naming_info=plan.environment_naming_info,
    )

    # Ensure table and column comments were correctly registered with engine
    if ctx.engine_adapter.COMMENT_CREATION_TABLE.is_supported:
        comments = {
            "customer_revenue_by_day": {
                "table": "Table of revenue from customers by day.",
                "column": {
                    "customer_id": "Customer id",
                    "revenue": "Revenue from orders made by this customer",
                    "event_date": "Date",
                },
            },
            "customer_revenue_lifetime": {
                "table": """Table of lifetime customer revenue.
    Date is available to get lifetime value up to a certain date.
    Use latest date to get current lifetime value.""",
                "column": {
                    "customer_id": "Customer id",
                    "revenue": "Lifetime revenue from this customer",
                    "event_date": "End date of the lifetime calculation",
                },
            },
            "customers": {
                "table": "Sushi customer data",
                "column": {"customer_id": "customer_id uniquely identifies customers"},
            },
            "orders": {
                "table": "Table of sushi orders.",
            },
            "raw_marketing": {
                "table": "Table of marketing status.",
                "column": {"customer_id": "Unique identifier of the customer"},
            },
            "top_waiters": {
                "table": "View of top waiters.",
            },
            "waiter_names": {
                "table": "List of waiter names",
            },
            "waiter_revenue_by_day": {
                "table": "Table of revenue generated by waiters by day.",
                "column": {
                    "waiter_id": "Waiter id",
                    "revenue": "Revenue from orders taken by this waiter",
                    "event_date": "Date",
                },
            },
        }

        def validate_comments(
            schema_name: str,
            expected_comments_dict: t.Dict[str, t.Any] = comments,
            is_physical_layer: bool = True,
            prod_schema_name: str = "sushi",
        ) -> None:
            layer_objects = context.engine_adapter.get_data_objects(schema_name)
            layer_models = {
                x.name.split("__")[1] if is_physical_layer else x.name: {
                    "table_name": x.name,
                    "is_view": x.type == DataObjectType.VIEW,
                }
                for x in layer_objects
                if not x.name.endswith("__dev")
            }

            for model_name, comment in comments.items():
                if not model_name in layer_models:
                    continue
                layer_table_name = layer_models[model_name]["table_name"]
                table_kind = "VIEW" if layer_models[model_name]["is_view"] else "BASE TABLE"

                # is this model in a physical layer or PROD environment?
                is_physical_or_prod = is_physical_layer or (
                    not is_physical_layer and schema_name == prod_schema_name
                )
                # is this model a VIEW and the engine doesn't support VIEW comments?
                is_view_and_comments_unsupported = (
                    layer_models[model_name]["is_view"]
                    and ctx.engine_adapter.COMMENT_CREATION_VIEW.is_unsupported
                )
                if is_physical_or_prod and not is_view_and_comments_unsupported:
                    expected_tbl_comment = comments.get(model_name).get("table", None)
                    if expected_tbl_comment:
                        actual_tbl_comment = ctx.get_table_comment(
                            schema_name,
                            layer_table_name,
                            table_kind=table_kind,
                            snowflake_capitalize_ids=False,
                        )
                        assert expected_tbl_comment == actual_tbl_comment

                    expected_col_comments = comments.get(model_name).get("column", None)

                    # Trino:
                    #   Trino on Hive COMMENT permissions are separate from standard SQL object permissions.
                    #   Trino has a bug where CREATE SQL permissions are not passed to COMMENT permissions,
                    #   which generates permissions errors when COMMENT commands are issued.
                    #
                    #   The errors are thrown for both table and comments, but apparently the
                    #   table comments are actually registered with the engine. Column comments are not.
                    #
                    # Query:
                    #   In the query test, columns_to_types are not available when views are created. Since we
                    #   can only register column comments in the CREATE VIEW schema expression with columns_to_types
                    #   available, the column comments must be registered via post-creation commands. Some engines,
                    #   such as Spark and Snowflake, do not support view column comments via post-creation commands.
                    if (
                        expected_col_comments
                        and not ctx.dialect == "trino"
                        and not (
                            ctx.test_type == "query"
                            and layer_models[model_name]["is_view"]
                            and not ctx.engine_adapter.COMMENT_CREATION_VIEW.supports_column_comment_commands
                        )
                    ):
                        actual_col_comments = ctx.get_column_comments(
                            schema_name,
                            layer_table_name,
                            table_kind=table_kind,
                            snowflake_capitalize_ids=False,
                        )
                        for column_name, expected_col_comment in expected_col_comments.items():
                            expected_col_comment = expected_col_comments.get(column_name, None)
                            actual_col_comment = actual_col_comments.get(column_name, None)
                            assert expected_col_comment == actual_col_comment

            return None

        def validate_no_comments(
            schema_name: str,
            expected_comments_dict: t.Dict[str, t.Any] = comments,
            is_physical_layer: bool = True,
            table_name_suffix: str = "",
            check_temp_tables: bool = False,
            prod_schema_name: str = "sushi",
        ) -> None:
            layer_objects = context.engine_adapter.get_data_objects(schema_name)
            layer_models = {
                x.name.split("__")[1] if is_physical_layer else x.name: {
                    "table_name": x.name,
                    "is_view": x.type == DataObjectType.VIEW,
                }
                for x in layer_objects
                if x.name.endswith(table_name_suffix)
            }
            if not check_temp_tables:
                layer_models = {k: v for k, v in layer_models.items() if not k.endswith("__dev")}

            for model_name, comment in comments.items():
                layer_table_name = layer_models[model_name]["table_name"]
                table_kind = "VIEW" if layer_models[model_name]["is_view"] else "BASE TABLE"

                actual_tbl_comment = ctx.get_table_comment(
                    schema_name,
                    layer_table_name,
                    table_kind=table_kind,
                    snowflake_capitalize_ids=False,
                )
                # MySQL doesn't support view comments and always returns "VIEW" as the table comment
                if ctx.dialect == "mysql" and layer_models[model_name]["is_view"]:
                    assert actual_tbl_comment == "VIEW"
                else:
                    assert actual_tbl_comment is None or actual_tbl_comment == ""

                # MySQL and Spark pass through the column comments from the underlying table to the view
                # so always have view comments present
                if not (
                    ctx.dialect in ("mysql", "spark", "databricks")
                    and layer_models[model_name]["is_view"]
                ):
                    expected_col_comments = comments.get(model_name).get("column", None)
                    if expected_col_comments:
                        actual_col_comments = ctx.get_column_comments(
                            schema_name,
                            layer_table_name,
                            table_kind=table_kind,
                            snowflake_capitalize_ids=False,
                        )
                        for column_name in expected_col_comments:
                            actual_col_comment = actual_col_comments.get(column_name, None)
                            assert actual_col_comment is None or actual_col_comment == ""

            return None

        validate_comments(f"sqlmesh__{sushi_test_schema}", prod_schema_name=sushi_test_schema)

        # confirm view layer comments are not registered in non-PROD environment
        env_name = "test_prod"
        if plan.environment_naming_info and plan.environment_naming_info.normalize_name:
            env_name = normalize_identifiers(env_name, dialect=ctx.dialect).name
        validate_no_comments(
            f"{sushi_test_schema}__{env_name}",
            is_physical_layer=False,
            prod_schema_name=sushi_test_schema,
        )

    # Ensure that the plan has been applied successfully.
    no_change_plan: Plan = context.plan_builder(
        environment="test_dev",
        start=start,
        end=end,
        skip_tests=True,
        include_unmodified=True,
    ).build()
    assert not no_change_plan.requires_backfill
    assert no_change_plan.context_diff.is_new_environment

    # make and validate unmodified dev environment
    context.apply(no_change_plan)

    data_validator.validate(
        f"{sushi_test_schema}.customer_revenue_lifetime",
        start,
        yesterday,
        env_name="test_dev",
        dialect=ctx.dialect,
        environment_naming_info=no_change_plan.environment_naming_info,
    )

    # confirm view layer comments are registered in PROD
    if ctx.engine_adapter.COMMENT_CREATION_VIEW.is_supported:
        context.plan(skip_tests=True, no_prompts=True, auto_apply=True)
        validate_comments(sushi_test_schema, is_physical_layer=False)

    # Register schemas for cleanup
    for schema in [
        f"{sushi_test_schema}__test_prod",
        f"{sushi_test_schema}__test_dev",
        sushi_test_schema,
        f"sqlmesh__{sushi_test_schema}",
        sushi_state_schema,
        raw_test_schema,
    ]:
        ctx._schemas.append(schema)


def test_init_project(ctx: TestContext, tmp_path: pathlib.Path):
    schema_name = ctx.add_test_suffix(TEST_SCHEMA)
    state_schema = ctx.add_test_suffix("sqlmesh_state")

    object_names = {
        "view_schema": [schema_name],
        "physical_schema": [f"sqlmesh__{schema_name}"],
        "dev_schema": [f"{schema_name}__test_dev"],
        "views": ["full_model", "incremental_model", "seed_model"],
    }

    # normalize object names for snowflake
    if ctx.dialect == "snowflake":

        def _normalize_snowflake(name: str, prefix_regex: str = "(sqlmesh__)(.*)"):
            match = re.search(prefix_regex, name)
            if match:
                return f"{match.group(1)}{match.group(2).upper()}"
            return name.upper()

        object_names = {
            k: [_normalize_snowflake(name) for name in v] for k, v in object_names.items()
        }

    init_example_project(tmp_path, ctx.engine_type, schema_name=schema_name)

    def _mutate_config(gateway: str, config: Config):
        # ensure default dialect comes from init_example_project and not ~/.sqlmesh/config.yaml
        if config.model_defaults.dialect != ctx.dialect:
            config.model_defaults = config.model_defaults.copy(update={"dialect": ctx.dialect})

        # Ensure the state schema is unique to this test (since we deliberately use the warehouse as the state connection)
        config.gateways[gateway].state_schema = state_schema

    context = ctx.create_context(_mutate_config, path=tmp_path, ephemeral_state_connection=False)

    if ctx.default_table_format:
        # if the default table format is explicitly set, ensure its being used
        replacement_models = {}
        for model_key, model in context._models.items():
            if not model.table_format:
                replacement_models[model_key] = model.copy(
                    update={"table_format": ctx.default_table_format}
                )
        context._models.update(replacement_models)

    # capture row counts for each evaluated snapshot
    actual_execution_stats = {}

    def capture_execution_stats(
        snapshot,
        interval,
        batch_idx,
        duration_ms,
        num_audits_passed,
        num_audits_failed,
        audit_only=False,
        execution_stats=None,
        auto_restatement_triggers=None,
    ):
        if execution_stats is not None:
            actual_execution_stats[snapshot.model.name.replace(f"{schema_name}.", "")] = (
                execution_stats
            )

    # apply prod plan
    with patch.object(
        context.console, "update_snapshot_evaluation_progress", capture_execution_stats
    ):
        context.plan(auto_apply=True, no_prompts=True)

    prod_schema_results = ctx.get_metadata_results(object_names["view_schema"][0])
    assert sorted(prod_schema_results.views) == object_names["views"]
    assert len(prod_schema_results.materialized_views) == 0
    assert len(prod_schema_results.tables) == len(prod_schema_results.non_temp_tables) == 0

    physical_layer_results = ctx.get_metadata_results(object_names["physical_schema"][0])
    assert len(physical_layer_results.views) == 0
    assert len(physical_layer_results.materialized_views) == 0
    assert len(physical_layer_results.tables) == len(physical_layer_results.non_temp_tables) == 3

    if ctx.engine_adapter.SUPPORTS_QUERY_EXECUTION_TRACKING:
        assert actual_execution_stats["incremental_model"].total_rows_processed == 7
        # snowflake and redshift don't track rows for CTAS
        assert actual_execution_stats["full_model"].total_rows_processed == (
            None if ctx.mark.startswith("snowflake") or ctx.mark.startswith("redshift") else 3
        )
        assert actual_execution_stats["seed_model"].total_rows_processed == (
            None if ctx.mark.startswith("snowflake") else 7
        )

        if ctx.mark.startswith("bigquery"):
            assert actual_execution_stats["incremental_model"].total_bytes_processed
            assert actual_execution_stats["full_model"].total_bytes_processed

    # run that loads 0 rows in incremental model
    # - some cloud DBs error because time travel messes up token expiration
    if not ctx.is_remote:
        actual_execution_stats = {}
        with patch.object(
            context.console, "update_snapshot_evaluation_progress", capture_execution_stats
        ):
            with time_machine.travel(date.today() + timedelta(days=1)):
                context.run()

        if ctx.engine_adapter.SUPPORTS_QUERY_EXECUTION_TRACKING:
            assert actual_execution_stats["incremental_model"].total_rows_processed == 0
            assert actual_execution_stats["full_model"].total_rows_processed == 3

    # make and validate unmodified dev environment
    no_change_plan: Plan = context.plan_builder(
        environment="test_dev",
        skip_tests=True,
        include_unmodified=True,
    ).build()
    assert not no_change_plan.requires_backfill
    assert no_change_plan.context_diff.is_new_environment

    context.apply(no_change_plan)

    environment = no_change_plan.environment
    first_snapshot = no_change_plan.environment.snapshots[0]
    schema_name = first_snapshot.qualified_view_name.schema_for_environment(
        environment, dialect=ctx.dialect
    )
    dev_schema_results = ctx.get_metadata_results(schema_name)
    assert sorted(dev_schema_results.views) == object_names["views"]
    assert len(dev_schema_results.materialized_views) == 0
    assert len(dev_schema_results.tables) == len(dev_schema_results.non_temp_tables) == 0

    # register the schemas to be cleaned up
    for schema in [
        state_schema,
        *object_names["view_schema"],
        *object_names["physical_schema"],
        *object_names["dev_schema"],
    ]:
        ctx._schemas.append(schema)


def test_dialects(ctx: TestContext):
    from sqlglot import Dialect, parse_one

    dialect = Dialect[ctx.dialect]

    if dialect.NORMALIZATION_STRATEGY == "CASE_INSENSITIVE":
        a = '"a"'
        b = '"b"'
        c = '"c"'
        d = '"d"'
    elif dialect.NORMALIZATION_STRATEGY == "LOWERCASE":
        a = '"a"'
        b = '"B"'
        c = '"c"'
        d = '"d"'
    # https://dev.mysql.com/doc/refman/8.0/en/identifier-case-sensitivity.html
    # if these tests fail for mysql it means you're running on os x or windows
    elif dialect.NORMALIZATION_STRATEGY == "CASE_SENSITIVE":
        a = '"a"'
        b = '"B"'
        c = '"c"'
        d = '"D"'
    else:
        a = '"a"'
        b = '"B"'
        c = '"C"'
        d = '"D"'

    q = parse_one(
        f"""
        WITH
          "a" AS (SELECT 1 w),
          "B" AS (SELECT 1 x),
          c AS (SELECT 1 y),
          D AS (SELECT 1 z)

          SELECT *
          FROM {a}
          CROSS JOIN {b}
          CROSS JOIN {c}
          CROSS JOIN {d}
    """
    )
    df = ctx.engine_adapter.fetchdf(q)
    expected_columns = ["W", "X", "Y", "Z"] if ctx.dialect == "snowflake" else ["w", "x", "y", "z"]
    pd.testing.assert_frame_equal(
        df, pd.DataFrame([[1, 1, 1, 1]], columns=expected_columns), check_dtype=False
    )


@pytest.mark.parametrize(
    "time_column, time_column_type, time_column_format, result",
    [
        (
            exp.null(),
            exp.DataType.build("TIMESTAMP", nullable=True),
            None,
            {
                "default": None,
                "bigquery": pd.NaT,
                "clickhouse": pd.NaT,
                "databricks": pd.NaT,
                "duckdb": pd.NaT,
                "motherduck": pd.NaT,
                "snowflake": pd.NaT,
                "spark": pd.NaT,
            },
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("DATE"),
            None,
            {
                "default": datetime(2020, 1, 1).date(),
                "clickhouse": pd.Timestamp("2020-01-01"),
                "duckdb": pd.Timestamp("2020-01-01"),
            },
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TIMESTAMPTZ"),
            None,
            {
                "default": pd.Timestamp("2020-01-01 00:00:00+00:00"),
                "clickhouse": pd.Timestamp("2020-01-01 00:00:00"),
                "fabric": pd.Timestamp("2020-01-01 00:00:00"),
                "mysql": pd.Timestamp("2020-01-01 00:00:00"),
                "spark": pd.Timestamp("2020-01-01 00:00:00"),
                "databricks": pd.Timestamp("2020-01-01 00:00:00"),
                "starrocks": pd.Timestamp("2020-01-01 00:00:00"),
            },
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TIMESTAMP"),
            None,
            {"default": pd.Timestamp("2020-01-01 00:00:00")},
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TEXT"),
            "%Y-%m-%dT%H:%M:%S%z",
            {
                "default": "2020-01-01T00:00:00+0000",
            },
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("INT"),
            "%Y%m%d",
            {
                "default": 20200101,
            },
        ),
    ],
)
def test_to_time_column(
    ctx: TestContext, time_column, time_column_type, time_column_format, result
):
    # TODO: can this be cleaned up after recent sqlglot updates?
    if ctx.dialect == "clickhouse" and time_column_type.is_type(exp.DataType.Type.TIMESTAMPTZ):
        # Clickhouse does not have natively timezone-aware types and does not accept timestrings
        #   with UTC offset "+XX:XX". Therefore, we remove the timezone offset and set a timezone-
        #   specific data type to validate what is returned.

        time_column = re.match(r"^(.*?)\+", time_column).group(1)
        time_column_type = exp.DataType.build("TIMESTAMP('UTC')", dialect="clickhouse")

    time_column = to_time_column(time_column, time_column_type, ctx.dialect, time_column_format)
    df = ctx.engine_adapter.fetchdf(exp.select(time_column).as_("the_col"))
    expected = result.get(ctx.dialect, result.get("default"))
    col_name = "THE_COL" if ctx.dialect == "snowflake" else "the_col"
    if expected is pd.NaT or expected is None:
        assert df[col_name][0] is expected
    else:
        assert df[col_name][0] == expected


def test_batch_size_on_incremental_by_unique_key_model(ctx: TestContext):
    if not ctx.supports_merge:
        pytest.skip(f"{ctx.dialect} on {ctx.gateway} doesnt support merge")

    def _mutate_config(current_gateway_name: str, config: Config):
        # make stepping through in the debugger easier
        connection = config.gateways[current_gateway_name].connection
        connection.concurrent_tasks = 1

    context = ctx.create_context(_mutate_config)
    assert context.default_dialect == "duckdb"

    schema = ctx.schema(TEST_SCHEMA)
    seed_columns_to_types = {
        "item_id": exp.DataType.build("integer"),
        "event_date": exp.DataType.build("date"),
    }
    seed_query = ctx.input_data(
        pd.DataFrame(
            [
                [2, "2020-01-01"],
                [1, "2020-01-01"],
                [3, "2020-01-03"],
                [1, "2020-01-04"],
                [1, "2020-01-05"],
                [1, "2020-01-06"],
                [1, "2020-01-07"],
            ],
            columns=["item_id", "event_date"],
        ),
        columns_to_types=seed_columns_to_types,
    )
    context.upsert_model(
        create_sql_model(
            name=f"{schema}.seed_model",
            query=seed_query,
            kind="FULL",
            columns=seed_columns_to_types,
        )
    )

    table_format = ""
    if ctx.dialect == "athena":
        # INCREMENTAL_BY_UNIQUE_KEY uses MERGE which is only supported in Athena on Iceberg tables
        table_format = "table_format iceberg,"

    context.upsert_model(
        load_sql_based_model(
            d.parse(
                f"""MODEL (
                    name {schema}.test_model,
                    kind INCREMENTAL_BY_UNIQUE_KEY (
                        unique_key item_id,
                        batch_size 1
                    ),
                    {table_format}
                    start '2020-01-01',
                    end '2020-01-07',
                    cron '@daily'
                );

                select * from {schema}.seed_model
                where event_date between @start_date and @end_date""",
            )
        )
    )

    try:
        context.plan(auto_apply=True, no_prompts=True)

        test_model = context.get_model(f"{schema}.test_model")
        normalized_schema_name = test_model.fully_qualified_table.db
        results = ctx.get_metadata_results(normalized_schema_name)
        assert "test_model" in results.views

        actual_df = (
            ctx.get_current_data(test_model.fqn).sort_values(by="event_date").reset_index(drop=True)
        )
        actual_df["event_date"] = actual_df["event_date"].astype(str)
        assert actual_df.count()[0] == 3

        expected_df = pd.DataFrame(
            [[2, "2020-01-01"], [3, "2020-01-03"], [1, "2020-01-07"]],
            columns=actual_df.columns,
        ).sort_values(by="event_date")

        pd.testing.assert_frame_equal(
            actual_df,
            expected_df,
            check_dtype=False,
        )

    finally:
        ctx.cleanup(context)


def test_incremental_by_unique_key_model_when_matched(ctx: TestContext):
    if not ctx.supports_merge:
        pytest.skip(f"{ctx.dialect} on {ctx.gateway} doesnt support merge")

    # DuckDB and some other engines use logical_merge which doesn't support when_matched
    if isinstance(ctx.engine_adapter, LogicalMergeMixin):
        pytest.skip(
            f"{ctx.dialect} on {ctx.gateway} uses logical merge which doesn't support when_matched"
        )

    def _mutate_config(current_gateway_name: str, config: Config):
        connection = config.gateways[current_gateway_name].connection
        connection.concurrent_tasks = 1
        if current_gateway_name == "inttest_redshift":
            connection.enable_merge = True

    context = ctx.create_context(_mutate_config)
    schema = ctx.schema(TEST_SCHEMA)

    # Create seed data with multiple days
    seed_query = ctx.input_data(
        pd.DataFrame(
            [
                [1, "item_a", 100, "2020-01-01"],
                [2, "item_b", 200, "2020-01-01"],
                [1, "item_a_changed", 150, "2020-01-02"],  # Same item_id, different name and value
                [2, "item_b_changed", 250, "2020-01-02"],  # Same item_id, different name and value
                [3, "item_c", 300, "2020-01-02"],  # New item on day 2
            ],
            columns=["item_id", "name", "value", "event_date"],
        ),
        columns_to_types={
            "item_id": exp.DataType.build("integer"),
            "name": exp.DataType.build("text"),
            "value": exp.DataType.build("integer"),
            "event_date": exp.DataType.build("date"),
        },
    )
    context.upsert_model(
        create_sql_model(name=f"{schema}.seed_model", query=seed_query, kind="FULL")
    )

    table_format = ""
    if ctx.dialect == "athena":
        # INCREMENTAL_BY_UNIQUE_KEY uses MERGE which is only supported in Athena on Iceberg tables
        table_format = "table_format iceberg,"

    # Create model with when_matched clause that only updates the value column
    # BUT keeps the existing name column unchanged
    # batch_size=1 is so that we trigger merge on second batch and verify behaviour of when_matched
    context.upsert_model(
        load_sql_based_model(
            d.parse(
                f"""MODEL (
                    name {schema}.test_model_when_matched,
                    kind INCREMENTAL_BY_UNIQUE_KEY (
                        unique_key item_id,
                        batch_size 1,
                        merge_filter source.event_date > target.event_date,
                        when_matched WHEN MATCHED THEN UPDATE SET target.value = source.value, target.event_date = source.event_date
                    ),
                    {table_format}
                    start '2020-01-01',
                    end '2020-01-02',
                    cron '@daily'
                );

                select item_id, name, value, event_date
                from {schema}.seed_model
                where event_date between @start_date and @end_date""",
            )
        )
    )

    try:
        # Initial plan to create the model and run it
        context.plan(auto_apply=True, no_prompts=True)

        test_model = context.get_model(f"{schema}.test_model_when_matched")

        # Verify that the model has the when_matched clause and merge_filter
        assert test_model.kind.when_matched is not None
        assert (
            test_model.kind.when_matched.sql()
            == '(WHEN MATCHED THEN UPDATE SET "__MERGE_TARGET__"."value" = "__MERGE_SOURCE__"."value", "__MERGE_TARGET__"."event_date" = "__MERGE_SOURCE__"."event_date")'
        )
        assert test_model.merge_filter is not None
        assert (
            test_model.merge_filter.sql()
            == '"__MERGE_SOURCE__"."event_date" > "__MERGE_TARGET__"."event_date"'
        )

        actual_df = (
            ctx.get_current_data(test_model.fqn).sort_values(by="item_id").reset_index(drop=True)
        )

        # Expected results after batch processing:
        # - Day 1: Items 1 and 2 are inserted (first insert)
        # - Day 2: Items 1 and 2 are merged (when_matched clause preserves names but updates values/dates)
        #          Item 3 is inserted as new
        expected_df = (
            pd.DataFrame(
                [
                    [1, "item_a", 150, "2020-01-02"],  # name from day 1, value and date from day 2
                    [2, "item_b", 250, "2020-01-02"],  # name from day 1, value and date from day 2
                    [3, "item_c", 300, "2020-01-02"],  # new item from day 2
                ],
                columns=["item_id", "name", "value", "event_date"],
            )
            .sort_values(by="item_id")
            .reset_index(drop=True)
        )

        # Convert date columns to string for comparison
        actual_df["event_date"] = actual_df["event_date"].astype(str)
        expected_df["event_date"] = expected_df["event_date"].astype(str)

        pd.testing.assert_frame_equal(
            actual_df,
            expected_df,
            check_dtype=False,
        )

    finally:
        ctx.cleanup(context)


def test_managed_model_upstream_forward_only(ctx: TestContext):
    """
    This scenario goes as follows:
        - A managed model B is a downstream dependency of an incremental model A
            (as a sidenote: this is an incorrect use of managed models, they should really only reference external models, but we dont prevent it specifically to be more user friendly)
        - User plans a forward-only change against Model A in a virtual environment "dev"
        - This causes a new non-deployable snapshot of Model B in "dev".
        - In these situations, we create a normal table for Model B, not a managed table
        - User modifies model B and applies a plan in "dev"
            - This should also result in a normal table
        - User decides they want to deploy so they run their plan against prod
            - We need to ensure we ignore the normal table for Model B (it was just a dev preview) and create a new managed table for prod
            - Upon apply to prod, Model B should be completely recreated as a managed table
    """

    if not ctx.engine_adapter.SUPPORTS_MANAGED_MODELS:
        pytest.skip("This test only runs for engines that support managed models")

    def _run_plan(sqlmesh_context: Context, environment: str = None) -> PlanResults:
        plan: Plan = sqlmesh_context.plan(auto_apply=True, no_prompts=True, environment=environment)
        return PlanResults.create(plan, ctx, schema)

    context = ctx.create_context()
    schema = ctx.add_test_suffix(TEST_SCHEMA)

    model_a = load_sql_based_model(
        d.parse(  # type: ignore
            f"""
            MODEL (
                name {schema}.upstream_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ts,
                    forward_only True
                ),
            );

            SELECT 1 as id, 'foo' as name, current_timestamp as ts;
            """
        )
    )

    model_b = load_sql_based_model(
        d.parse(  # type: ignore
            f"""
            MODEL (
                name {schema}.managed_model,
                kind MANAGED,
                physical_properties (
                    target_lag = '5 minutes'
                )
            );

            SELECT * from {schema}.upstream_model;
            """
        )
    )

    context.upsert_model(model_a)
    context.upsert_model(model_b)

    plan_1 = _run_plan(context)

    assert plan_1.snapshot_for(model_a).change_category == SnapshotChangeCategory.BREAKING
    assert not plan_1.snapshot_for(model_a).is_forward_only
    assert plan_1.snapshot_for(model_b).change_category == SnapshotChangeCategory.BREAKING
    assert not plan_1.snapshot_for(model_b).is_forward_only

    # so far so good, model_a should exist as a normal table, model b should be a managed table and the prod views should exist
    assert len(plan_1.schema_metadata.views) == 2
    assert plan_1.snapshot_for(model_a).model.view_name in plan_1.schema_metadata.views
    assert plan_1.snapshot_for(model_b).model.view_name in plan_1.schema_metadata.views

    assert len(plan_1.internal_schema_metadata.tables) == 1

    assert plan_1.table_name_for(model_a) in plan_1.internal_schema_metadata.tables
    assert (
        plan_1.table_name_for(model_b) not in plan_1.internal_schema_metadata.tables
    )  # because its a managed table

    assert len(plan_1.internal_schema_metadata.managed_tables) == 1
    assert plan_1.table_name_for(model_b) in plan_1.internal_schema_metadata.managed_tables
    assert (
        plan_1.dev_table_name_for(model_b) not in plan_1.internal_schema_metadata.managed_tables
    )  # the dev table should not be created as managed

    # Let's modify model A with a breaking change and plan it against a dev environment. This should trigger a forward-only plan
    new_model_a = load_sql_based_model(
        d.parse(  # type: ignore
            f"""
            MODEL (
                name {schema}.upstream_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ts,
                    forward_only True
                ),
            );

            SELECT 1 as id, 'foo' as name, 'bar' as extra, current_timestamp as ts;
            """
        )
    )
    context.upsert_model(new_model_a)

    # apply plan to dev environment
    plan_2 = _run_plan(context, "dev")

    assert plan_2.plan.has_changes
    assert len(plan_2.plan.modified_snapshots) == 2
    assert plan_2.snapshot_for(new_model_a).change_category == SnapshotChangeCategory.NON_BREAKING
    assert plan_2.snapshot_for(new_model_a).is_forward_only
    assert plan_2.snapshot_for(model_b).change_category == SnapshotChangeCategory.NON_BREAKING
    assert not plan_2.snapshot_for(model_b).is_forward_only

    # verify that the new snapshots were created correctly
    # the forward-only change to model A should be in a new table separate from the one created in the first plan
    # since model B depends on an upstream model with a forward-only change, it should also get recreated, but as a normal table, not a managed table
    assert plan_2.table_name_for(model_a) == plan_1.table_name_for(
        model_a
    )  # no change in the main table because the dev preview changes go to the dev table
    assert plan_2.dev_table_name_for(model_a) != plan_1.dev_table_name_for(
        model_a
    )  # it creates a new dev table to hold the dev preview
    assert plan_2.dev_table_name_for(model_a) in plan_2.internal_schema_metadata.tables

    assert plan_2.table_name_for(model_b) != plan_1.table_name_for(
        model_b
    )  # model b gets a new table
    assert plan_2.dev_table_name_for(model_b) != plan_1.dev_table_name_for(
        model_b
    )  # model b gets a new dev table as well
    assert (
        plan_2.table_name_for(model_b) not in plan_2.internal_schema_metadata.tables
    )  # the new main table is not actually created, because it was triggered by a forward-only change. downstream models use the dev table
    assert plan_2.table_name_for(model_b) not in plan_2.internal_schema_metadata.managed_tables
    assert (
        plan_2.dev_table_name_for(model_b) in plan_2.internal_schema_metadata.tables
    )  # dev tables are always regular tables for managed models

    # modify model B, still in the dev environment
    new_model_b = load_sql_based_model(
        d.parse(  # type: ignore
            f"""
            MODEL (
                name {schema}.managed_model,
                kind MANAGED,
                physical_properties (
                    target_lag = '5 minutes'
                )
            );

            SELECT *, 'modified' as extra_b from {schema}.upstream_model;
            """
        )
    )
    context.upsert_model(new_model_b)

    plan_3 = _run_plan(context, "dev")

    assert plan_3.plan.has_changes
    assert len(plan_3.plan.modified_snapshots) == 1
    assert (
        plan_3.modified_snapshot_for(model_b).change_category == SnapshotChangeCategory.NON_BREAKING
    )

    # model A should be unchanged
    # the new model B should be a normal table, not a managed table
    assert plan_3.table_name_for(model_a) == plan_2.table_name_for(model_a)
    assert plan_3.dev_table_name_for(model_a) == plan_2.dev_table_name_for(model_a)
    assert plan_3.table_name_for(model_b) != plan_2.table_name_for(model_b)
    assert plan_3.dev_table_name_for(model_b) != plan_2.table_name_for(model_b)

    assert (
        plan_3.table_name_for(model_b) not in plan_3.internal_schema_metadata.tables
    )  # still using the dev table, no main table created
    assert plan_3.dev_table_name_for(model_b) in plan_3.internal_schema_metadata.tables
    assert (
        plan_3.table_name_for(model_b) not in plan_3.internal_schema_metadata.managed_tables
    )  # still not a managed table

    # apply plan to prod
    plan_4 = _run_plan(context)

    assert plan_4.plan.has_changes
    assert plan_4.snapshot_for(model_a).change_category == SnapshotChangeCategory.NON_BREAKING
    assert plan_4.snapshot_for(model_a).is_forward_only
    assert plan_4.snapshot_for(model_b).change_category == SnapshotChangeCategory.NON_BREAKING
    assert not plan_4.snapshot_for(model_b).is_forward_only

    # verify the Model B table is created as a managed table in prod
    assert plan_4.table_name_for(model_b) == plan_3.table_name_for(
        model_b
    )  # the model didnt change; the table should still have the same name
    assert (
        plan_4.table_name_for(model_b) not in plan_4.internal_schema_metadata.tables
    )  # however, it should be a managed table, not a normal table
    assert plan_4.table_name_for(model_b) in plan_4.internal_schema_metadata.managed_tables


@pytest.mark.parametrize(
    "column_type, input_data, expected_results",
    [
        (DATA_TYPE.BOOLEAN, (True, False, None), ("1", "0", None)),
        (
            DATA_TYPE.DATE,
            (datetime(2023, 1, 1).date(), datetime(2024, 12, 15, 5, 30, 0).date(), None),
            ("2023-01-01", "2024-12-15", None),
        ),
        (
            DATA_TYPE.TIMESTAMP,
            (
                datetime(2023, 1, 1),
                datetime(2023, 1, 1, 13, 14, 15),
                datetime(2023, 1, 1, 13, 14, 15, 123456),
                None,
            ),
            (
                "2023-01-01 00:00:00.000000",
                "2023-01-01 13:14:15.000000",
                "2023-01-01 13:14:15.123456",
                None,
            ),
        ),
        (
            DATA_TYPE.DATETIME,
            (
                datetime(2023, 1, 1),
                datetime(2023, 1, 1, 13, 14, 15),
                datetime(2023, 1, 1, 13, 14, 15, 123456),
                None,
            ),
            (
                "2023-01-01 00:00:00.000000",
                "2023-01-01 13:14:15.000000",
                "2023-01-01 13:14:15.123456",
                None,
            ),
        ),
        (
            DATA_TYPE.TIMESTAMPTZ,
            (
                pytz.timezone("America/Los_Angeles").localize(datetime(2023, 1, 1)),
                pytz.timezone("Europe/Athens").localize(datetime(2023, 1, 1, 13, 14, 15)),
                pytz.timezone("Pacific/Auckland").localize(
                    datetime(2023, 1, 1, 13, 14, 15, 123456)
                ),
                None,
            ),
            (
                "2023-01-01 08:00:00.000000",
                "2023-01-01 11:14:15.000000",
                "2023-01-01 00:14:15.123456",
                None,
            ),
        ),
    ],
)
def test_value_normalization(
    ctx: TestContext,
    column_type: exp.DataType.Type,
    input_data: t.Tuple[t.Any, ...],
    expected_results: t.Tuple[str, ...],
) -> None:
    # Skip TIMESTAMPTZ tests for engines that don't support it
    if column_type == exp.DataType.Type.TIMESTAMPTZ:
        if ctx.dialect == "trino" and ctx.engine_adapter.current_catalog_type == "hive":
            pytest.skip("Trino on Hive doesn't support TIMESTAMP WITH TIME ZONE fields")
        if ctx.dialect == "fabric":
            pytest.skip("Fabric doesn't support TIMESTAMP WITH TIME ZONE fields")

    if not isinstance(ctx.engine_adapter, RowDiffMixin):
        pytest.skip(
            "Value normalization tests are only relevant for engines with row diffing implemented"
        )

    full_column_type = exp.DataType.build(column_type)

    # resolve dialect-specific types
    if column_type in (DATA_TYPE.DATETIME, DATA_TYPE.TIMESTAMP, DATA_TYPE.TIMESTAMPTZ):
        if ctx.dialect in ("mysql", "trino"):
            # MySQL needs DATETIME(6) instead of DATETIME or subseconds will be truncated.
            # It also needs TIMESTAMP(6) as the column type for CREATE TABLE or the truncation will occur
            full_column_type = exp.DataType.build(
                column_type,
                expressions=[
                    exp.DataTypeParam(
                        this=exp.Literal.number(ctx.engine_adapter.MAX_TIMESTAMP_PRECISION)
                    )
                ],
            )
    if ctx.dialect == "tsql" and column_type == exp.DataType.Type.DATETIME:
        full_column_type = exp.DataType.build("DATETIME2", dialect="tsql")

    columns_to_types = {
        "_idx": exp.DataType.build(DATA_TYPE.INT),
        "value": full_column_type,
    }

    input_data_with_idx = [(idx, value) for idx, value in enumerate(input_data)]

    test_table = normalize_identifiers(
        exp.to_table(ctx.table("test_value_normalization")), dialect=ctx.dialect
    )
    columns_to_types_normalized = {
        normalize_identifiers(k, dialect=ctx.dialect).sql(dialect=ctx.dialect): v
        for k, v in columns_to_types.items()
    }

    ctx.engine_adapter.create_table(
        table_name=test_table, target_columns_to_types=columns_to_types_normalized
    )
    data_query = next(select_from_values(input_data_with_idx, columns_to_types_normalized))
    ctx.engine_adapter.insert_append(
        table_name=test_table,
        query_or_df=data_query,
        target_columns_to_types=columns_to_types_normalized,
    )

    query = (
        exp.select(
            ctx.engine_adapter.normalize_value(
                normalize_identifiers(exp.to_column("value"), dialect=ctx.dialect),
                columns_to_types["value"],
                decimal_precision=3,
                timestamp_precision=ctx.engine_adapter.MAX_TIMESTAMP_PRECISION,
            ).as_("value")
        )
        .from_(test_table)
        .order_by(normalize_identifiers("_idx", dialect=ctx.dialect))
    )
    result = ctx.engine_adapter.fetchdf(query, quote_identifiers=True)
    assert len(result) == len(expected_results)

    def truncate_timestamp(ts: str, precision: int) -> str:
        if not ts:
            return ts

        digits_to_truncate = 6 - precision
        return ts[:-digits_to_truncate] if digits_to_truncate > 0 else ts

    if full_column_type.is_type(DATA_TYPE.DATETIME, DATA_TYPE.TIMESTAMP, DATA_TYPE.TIMESTAMPTZ):
        # truncate our expected results to the engine precision
        expected_results = tuple(
            truncate_timestamp(e, ctx.engine_adapter.MAX_TIMESTAMP_PRECISION)
            for e in expected_results
        )

    for idx, row in enumerate(result.itertuples(index=False)):
        assert row.value == expected_results[idx]


def test_table_diff_grain_check_single_key(ctx: TestContext):
    if not isinstance(ctx.engine_adapter, RowDiffMixin):
        pytest.skip("table_diff tests are only relevant for engines with row diffing implemented")

    src_table = ctx.table("source")
    target_table = ctx.table("target")

    columns_to_types = {
        "key1": exp.DataType.build("int"),
        "value": exp.DataType.build("varchar"),
    }

    ctx.engine_adapter.create_table(src_table, columns_to_types)
    ctx.engine_adapter.create_table(target_table, columns_to_types)

    src_data = [
        (1, "one"),
        (2, "two"),
        (None, "three"),
        (4, "four"),  # missing in target
    ]

    target_data = [
        (1, "one"),
        (2, "two"),
        (None, "three"),
        (5, "five"),  # missing in src
        (6, "six"),  # missing in src
    ]

    ctx.engine_adapter.replace_query(
        src_table, pd.DataFrame(src_data, columns=columns_to_types.keys()), columns_to_types
    )
    ctx.engine_adapter.replace_query(
        target_table, pd.DataFrame(target_data, columns=columns_to_types.keys()), columns_to_types
    )

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=['"key1"'],
    )

    row_diff = table_diff.row_diff()

    assert row_diff.full_match_count == 2
    assert row_diff.full_match_pct == 57.14
    assert row_diff.s_only_count == 1
    assert row_diff.t_only_count == 2
    assert row_diff.stats["key1_matches"] == 4
    assert row_diff.stats["value_matches"] == 2
    assert row_diff.stats["join_count"] == 2
    assert row_diff.stats["null_grain_count"] == 2
    assert row_diff.stats["s_count"] == 3
    assert row_diff.stats["distinct_count_s"] == 3
    assert row_diff.stats["t_count"] == 4
    assert row_diff.stats["distinct_count_t"] == 4
    assert row_diff.stats["s_only_count"] == 1
    assert row_diff.stats["t_only_count"] == 2
    assert row_diff.s_sample.shape == (1, 2)
    assert row_diff.t_sample.shape == (2, 2)


def test_table_diff_grain_check_multiple_keys(ctx: TestContext):
    if not isinstance(ctx.engine_adapter, RowDiffMixin):
        pytest.skip("table_diff tests are only relevant for engines with row diffing implemented")

    src_table = ctx.table("source")
    target_table = ctx.table("target")

    columns_to_types = {
        "key1": exp.DataType.build("int"),
        "key2": exp.DataType.build("varchar"),
        "value": exp.DataType.build("varchar"),
    }

    ctx.engine_adapter.create_table(src_table, columns_to_types)
    ctx.engine_adapter.create_table(target_table, columns_to_types)

    src_data = [
        (1, 1, 1),
        (7, 4, 2),
        (None, 3, 3),
        (None, None, 3),
        (1, 2, 2),
        (4, None, 3),
        (2, 3, 2),
    ]

    target_data = src_data + [(1, 6, 1), (1, 5, 3), (None, 2, 3)]

    ctx.engine_adapter.insert_append(
        src_table, next(select_from_values(src_data, columns_to_types)), columns_to_types
    )
    ctx.engine_adapter.insert_append(
        target_table, next(select_from_values(target_data, columns_to_types)), columns_to_types
    )

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=['"key1"', '"key2"'],
    )

    row_diff = table_diff.row_diff()

    assert row_diff.full_match_count == 7
    assert row_diff.full_match_pct == 82.35
    assert row_diff.s_only_count == 0
    assert row_diff.t_only_count == 3
    assert row_diff.stats["join_count"] == 7
    assert (
        row_diff.stats["null_grain_count"] == 4
    )  # null grain currently (2025-07-24) means "any key column is null" as opposed to "all key columns are null"
    assert row_diff.stats["distinct_count_s"] == 7
    assert row_diff.stats["s_count"] == row_diff.stats["distinct_count_s"]
    assert row_diff.stats["distinct_count_t"] == 10
    assert row_diff.stats["t_count"] == row_diff.stats["distinct_count_t"]
    assert row_diff.s_sample.shape == (row_diff.s_only_count, 3)
    assert row_diff.t_sample.shape == (row_diff.t_only_count, 3)


def test_table_diff_arbitrary_condition(ctx: TestContext):
    if not isinstance(ctx.engine_adapter, RowDiffMixin):
        pytest.skip("table_diff tests are only relevant for engines with row diffing implemented")

    src_table = ctx.table("source")
    target_table = ctx.table("target")

    columns_to_types_src = {
        "id": exp.DataType.build("int"),
        "value": exp.DataType.build("varchar"),
        "ts": exp.DataType.build("timestamp"),
    }

    columns_to_types_target = {
        "item_id": exp.DataType.build("int"),
        "value": exp.DataType.build("varchar"),
        "ts": exp.DataType.build("timestamp"),
    }

    ctx.engine_adapter.create_table(src_table, columns_to_types_src)
    ctx.engine_adapter.create_table(target_table, columns_to_types_target)

    src_data = [
        (1, "one", datetime(2023, 1, 1, 12, 13, 14)),
        (2, "two", datetime(2023, 10, 1, 8, 13, 14)),
        (3, "three", datetime(2024, 1, 1, 8, 13, 14)),
    ]

    target_data = src_data + [(4, "four", datetime(2024, 2, 1, 8, 13, 14))]

    ctx.engine_adapter.replace_query(
        src_table, pd.DataFrame(src_data, columns=columns_to_types_src.keys()), columns_to_types_src
    )
    ctx.engine_adapter.replace_query(
        target_table,
        pd.DataFrame(target_data, columns=columns_to_types_target.keys()),
        columns_to_types_target,
    )

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=parse_one('"s"."id" = "t"."item_id"', into=exp.Condition),
        where=parse_one("to_char(\"ts\", 'YYYY') = '2024'", dialect="postgres", into=exp.Condition),
    )

    row_diff = table_diff.row_diff()

    assert row_diff.full_match_count == 1
    assert row_diff.full_match_pct == 66.67
    assert row_diff.s_only_count == 0
    assert row_diff.t_only_count == 1
    assert row_diff.stats["value_matches"] == 1
    assert row_diff.stats["ts_matches"] == 1
    assert row_diff.stats["join_count"] == 1
    assert row_diff.stats["null_grain_count"] == 0
    assert row_diff.stats["s_count"] == 1
    assert row_diff.stats["distinct_count_s"] == 1
    assert row_diff.stats["t_count"] == 2
    assert row_diff.stats["distinct_count_t"] == 2
    assert row_diff.stats["s_only_count"] == 0
    assert row_diff.stats["t_only_count"] == 1
    assert row_diff.s_sample.shape == (0, 3)
    assert row_diff.t_sample.shape == (1, 3)


def test_table_diff_identical_dataset(ctx: TestContext):
    if not isinstance(ctx.engine_adapter, RowDiffMixin):
        pytest.skip("table_diff tests are only relevant for engines with row diffing implemented")

    src_table = ctx.table("source")
    target_table = ctx.table("target")

    columns_to_types = {
        "key1": exp.DataType.build("int"),
        "key2": exp.DataType.build("varchar"),
        "value": exp.DataType.build("varchar"),
    }

    ctx.engine_adapter.create_table(src_table, columns_to_types)
    ctx.engine_adapter.create_table(target_table, columns_to_types)

    src_data = [
        (1, 1, 1),
        (7, 4, 2),
        (1, 2, 2),
        (4, 1, 3),
        (2, 3, 2),
    ]

    target_data = src_data

    ctx.engine_adapter.insert_append(
        src_table, next(select_from_values(src_data, columns_to_types)), columns_to_types
    )
    ctx.engine_adapter.insert_append(
        target_table, next(select_from_values(target_data, columns_to_types)), columns_to_types
    )

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=['"key1"', '"key2"'],
    )

    row_diff = table_diff.row_diff()

    assert row_diff.full_match_count == 5
    assert row_diff.full_match_pct == 100
    assert row_diff.s_only_count == 0
    assert row_diff.t_only_count == 0
    assert row_diff.stats["join_count"] == 5
    assert row_diff.stats["null_grain_count"] == 0
    assert row_diff.stats["s_count"] == 5
    assert row_diff.stats["distinct_count_s"] == 5
    assert row_diff.stats["t_count"] == 5
    assert row_diff.stats["distinct_count_t"] == 5
    assert row_diff.stats["s_only_count"] == 0
    assert row_diff.stats["t_only_count"] == 0
    assert row_diff.s_sample.shape == (0, 3)
    assert row_diff.t_sample.shape == (0, 3)


def test_state_migrate_from_scratch(ctx: TestContext):
    test_schema = ctx.add_test_suffix("state")
    ctx._schemas.append(test_schema)  # so it gets cleaned up when the test finishes

    def _use_warehouse_as_state_connection(gateway_name: str, config: Config):
        warehouse_connection = config.gateways[gateway_name].connection
        assert isinstance(warehouse_connection, ConnectionConfig)
        if warehouse_connection.is_forbidden_for_state_sync:
            pytest.skip(
                f"{warehouse_connection.type_} doesnt support being used as a state connection"
            )

        # this triggers the fallback to using the warehouse as a state connection
        config.gateways[gateway_name].state_connection = None
        assert config.get_state_connection(gateway_name) is None

        config.gateways[gateway_name].state_schema = test_schema

    sqlmesh_context = ctx.create_context(
        config_mutator=_use_warehouse_as_state_connection, ephemeral_state_connection=False
    )
    assert sqlmesh_context.config.get_state_schema(ctx.gateway) == test_schema

    state_sync = (
        sqlmesh_context._new_state_sync()
    )  # this prevents migrate() being called which it does if you access the state_sync property
    assert isinstance(state_sync, EngineAdapterStateSync)
    assert state_sync.engine_adapter.dialect == ctx.dialect

    # will throw if one of the migrations produces an error, which can happen if we forget to take quoting or normalization into account
    sqlmesh_context.migrate()


def test_python_model_column_order(ctx_df: TestContext, tmp_path: pathlib.Path):
    ctx = ctx_df

    model_name = ctx.table("TEST")

    (tmp_path / "models").mkdir()

    # note: this model deliberately defines the columns in the @model definition to be in a different order than what
    # is returned by the DataFrame within the model
    model_path = tmp_path / "models" / "python_model.py"

    model_definitions = {
        # python model that emits a Pandas dataframe
        "pandas": """
import pandas as pd  # noqa: TID253
import typing as t
from sqlmesh import ExecutionContext, model

@model(
    'MODEL_NAME',
    columns={
        "id": "int",
        "name": "text"
    },
    dialect='DIALECT',
    TABLE_FORMAT
)
def execute(
    context: ExecutionContext,
    **kwargs: t.Any,
) -> pd.DataFrame:
    record = { "name": "foo", "id": 1 } if context.engine_adapter.dialect != 'snowflake' else { "NAME": "foo", "ID": 1 }
    return pd.DataFrame([
        record
    ])
        """,
        # python model that emits a PySpark dataframe
        "pyspark": """
from pyspark.sql import DataFrame, Row
import typing as t
from sqlmesh import ExecutionContext, model

@model(
    'MODEL_NAME',
    columns={
        "id": "int",
        "name": "varchar"
    },
    dialect='DIALECT'
)
def execute(
    context: ExecutionContext,
    **kwargs: t.Any,
) -> DataFrame:
    return context.spark.createDataFrame([
        Row(name="foo", id=1)
    ])
        """,
        # python model that emits a BigFrame dataframe
        "bigframe": """
from bigframes.pandas import DataFrame
import typing as t
from sqlmesh import ExecutionContext, model

@model(
    'MODEL_NAME',
    columns={
        "id": "int",
        "name": "varchar"
    },
    dialect="DIALECT"
)
def execute(
    context: ExecutionContext,
    **kwargs: t.Any,
) -> DataFrame:
    return DataFrame({'name': ['foo'], 'id': [1]}, session=context.bigframe)
        """,
        # python model that emits a Snowpark dataframe
        "snowpark": """
from snowflake.snowpark.dataframe import DataFrame
import typing as t
from sqlmesh import ExecutionContext, model

@model(
    'MODEL_NAME',
    columns={
        "id": "int",
        "name": "varchar"
    },
    dialect="DIALECT"
)
def execute(
    context: ExecutionContext,
    **kwargs: t.Any,
) -> DataFrame:
    return context.snowpark.create_dataframe([["foo", 1]], schema=["NAME", "ID"])
        """,
    }

    model_path.write_text(
        (
            model_definitions[ctx.df_type]
            .replace("MODEL_NAME", model_name.sql(dialect=ctx.dialect))
            .replace("DIALECT", ctx.dialect)
            .replace(
                "TABLE_FORMAT",
                f"table_format='{ctx.default_table_format}'" if ctx.default_table_format else "",
            )
        )
    )

    sqlmesh_ctx = ctx.create_context(path=tmp_path)

    assert len(sqlmesh_ctx.models) == 1

    plan = sqlmesh_ctx.plan(auto_apply=True)
    assert len(plan.new_snapshots) == 1

    engine_adapter = sqlmesh_ctx.engine_adapter

    query = exp.select("*").from_(plan.environment.snapshots[0].fully_qualified_table)
    df = engine_adapter.fetchdf(query, quote_identifiers=True)
    assert len(df) == 1

    # This test uses the dialect=<engine under test> on the model.
    # For dialect=snowflake, this means that the identifiers are all normalized to uppercase by default
    expected_result = (
        {"id": 1, "name": "foo"} if ctx.dialect != "snowflake" else {"ID": 1, "NAME": "foo"}
    )
    assert df.iloc[0].to_dict() == expected_result


def test_identifier_length_limit(ctx: TestContext):
    adapter = ctx.engine_adapter
    if adapter.MAX_IDENTIFIER_LENGTH is None:
        pytest.skip(f"Engine {adapter.dialect} does not have identifier length limits set.")

    long_table_name = "a" * (adapter.MAX_IDENTIFIER_LENGTH + 1)

    match = f"Identifier name '{long_table_name}' (length {len(long_table_name)}) exceeds {adapter.dialect.capitalize()}'s max identifier limit of {adapter.MAX_IDENTIFIER_LENGTH} characters"
    with pytest.raises(
        SQLMeshError,
        match=re.escape(match),
    ):
        adapter.create_table(long_table_name, {"col": exp.DataType.build("int")})


@pytest.mark.parametrize(
    "environment_suffix_target",
    [
        EnvironmentSuffixTarget.TABLE,
        EnvironmentSuffixTarget.SCHEMA,
        EnvironmentSuffixTarget.CATALOG,
    ],
)
@pytest.mark.xdist_group("serial")
def test_janitor(
    ctx: TestContext, tmp_path: pathlib.Path, environment_suffix_target: EnvironmentSuffixTarget
):
    if (
        environment_suffix_target == EnvironmentSuffixTarget.CATALOG
        and not ctx.engine_adapter.SUPPORTS_CREATE_DROP_CATALOG
    ):
        pytest.skip("Engine does not support catalog-based virtual environments")

    schema = ctx.schema()  # catalog.schema
    parsed_schema = d.to_schema(schema)

    init_example_project(tmp_path, ctx.engine_type, schema_name=parsed_schema.db)

    def _set_config(gateway: str, config: Config) -> None:
        config.environment_suffix_target = environment_suffix_target
        config.model_defaults.dialect = ctx.dialect
        config.gateways[gateway].connection.concurrent_tasks = 1

    sqlmesh = ctx.create_context(path=tmp_path, config_mutator=_set_config)

    sqlmesh.plan(auto_apply=True)

    # create a new model in dev
    (tmp_path / "models" / "new_model.sql").write_text(f"""
        MODEL (
            name {schema}.new_model,
            kind FULL
        );

        select * from {schema}.full_model
    """)
    sqlmesh.load()

    result = sqlmesh.plan(environment="dev", auto_apply=True)
    assert result.context_diff.is_new_environment
    assert len(result.context_diff.new_snapshots) == 1
    new_model = list(result.context_diff.new_snapshots.values())[0]
    assert "new_model" in new_model.name.lower()

    # check physical objects
    snapshot_table_name = exp.to_table(new_model.table_name(), dialect=ctx.dialect)
    snapshot_schema = parsed_schema.copy()
    snapshot_schema.set(
        "db", exp.to_identifier(snapshot_table_name.db)
    )  # we need this to be catalog.schema and not just schema for environment_suffix_target: catalog

    prod_schema = normalize_identifiers(d.to_schema(schema), dialect=ctx.dialect)
    dev_env_schema = prod_schema.copy()
    if environment_suffix_target == EnvironmentSuffixTarget.CATALOG:
        dev_env_schema.set("catalog", exp.to_identifier(f"{prod_schema.catalog}__dev"))
    else:
        dev_env_schema.set("db", exp.to_identifier(f"{prod_schema.db}__dev"))
    normalize_identifiers(dev_env_schema, dialect=ctx.dialect)

    md = ctx.get_metadata_results(prod_schema)
    if environment_suffix_target == EnvironmentSuffixTarget.TABLE:
        assert sorted([v.lower() for v in md.views]) == [
            "full_model",
            "incremental_model",
            "new_model__dev",
            "seed_model",
        ]
    else:
        assert sorted([v.lower() for v in md.views]) == [
            "full_model",
            "incremental_model",
            "seed_model",
        ]
    assert not md.tables
    assert not md.managed_tables

    if environment_suffix_target != EnvironmentSuffixTarget.TABLE:
        # note: this is "catalog__dev.schema" for EnvironmentSuffixTarget.CATALOG and "catalog.schema__dev" for EnvironmentSuffixTarget.SCHEMA
        md = ctx.get_metadata_results(dev_env_schema)
        assert [v.lower() for v in md.views] == ["new_model"]
        assert not md.tables
        assert not md.managed_tables

    md = ctx.get_metadata_results(snapshot_schema)
    assert not md.views
    assert not md.managed_tables
    assert sorted(t.split("__")[1].lower() for t in md.tables) == [
        "full_model",
        "incremental_model",
        "new_model",
        "seed_model",
    ]

    # invalidate dev and run the janitor to clean it up
    sqlmesh.invalidate_environment("dev")
    assert sqlmesh.run_janitor(
        ignore_ttl=True
    )  # ignore_ttl to delete the new_model snapshot even though it hasnt expired yet

    # there should be no dev environment or dev tables / schemas
    md = ctx.get_metadata_results(prod_schema)
    assert sorted([v.lower() for v in md.views]) == [
        "full_model",
        "incremental_model",
        "seed_model",
    ]
    assert not md.tables
    assert not md.managed_tables

    if environment_suffix_target != EnvironmentSuffixTarget.TABLE:
        if environment_suffix_target == EnvironmentSuffixTarget.SCHEMA:
            md = ctx.get_metadata_results(dev_env_schema)
        else:
            try:
                md = ctx.get_metadata_results(dev_env_schema)
            except Exception as e:
                # Most engines will raise an error when @set_catalog tries to set a catalog that doesnt exist
                # in this case, we just swallow the error. We know this call already worked before in the earlier checks
                md = MetadataResults()

        assert not md.views
        assert not md.tables
        assert not md.managed_tables

    if ctx.dialect == "fabric":
        # TestContext is using a different EngineAdapter instance / connection pool instance to the SQLMesh context
        # When the SQLMesh context drops :snapshot_schema using its EngineAdapter, connections in TestContext are unaware
        # and still have their threadlocal "target_catalog" attribute pointing to a catalog that no longer exists
        # Trying to establish a connection to a nonexistant catalog produces an error, so we close all connections here
        # to clear the threadlocal attributes
        ctx.engine_adapter.close()

    md = ctx.get_metadata_results(snapshot_schema)
    assert not md.views
    assert not md.managed_tables
    assert sorted(t.split("__")[1].lower() for t in md.tables) == [
        "full_model",
        "incremental_model",
        "seed_model",
    ]


def test_materialized_view_evaluation(ctx: TestContext):
    adapter = ctx.engine_adapter
    dialect = ctx.dialect

    if not adapter.SUPPORTS_MATERIALIZED_VIEWS:
        pytest.skip(f"Skipping engine {dialect} as it does not support materialized views")
    elif dialect in ("snowflake", "databricks"):
        pytest.skip(f"Skipping {dialect} as they're not enabled on standard accounts")

    model_name = ctx.table("test_tbl")
    mview_name = ctx.table("test_mview")

    sqlmesh = ctx.create_context()

    sqlmesh.upsert_model(
        load_sql_based_model(
            d.parse(
                f"""
                MODEL (name {model_name}, kind FULL);

                SELECT 1 AS col
                """
            )
        )
    )

    sqlmesh.upsert_model(
        load_sql_based_model(
            d.parse(
                f"""
                MODEL (name {mview_name}, kind VIEW (materialized true));

                SELECT * FROM {model_name}
                """
            )
        )
    )

    def _assert_mview_value(value: int):
        df = adapter.fetchdf(f"SELECT * FROM {mview_name.sql(dialect=dialect)}")
        assert df["col"][0] == value

    # Case 1: Ensure that plan is successful and we can query the materialized view
    sqlmesh.plan(auto_apply=True, no_prompts=True)

    _assert_mview_value(value=1)

    # Case 2: Ensure that we can change the underlying table and the materialized view is recreated
    sqlmesh.upsert_model(
        load_sql_based_model(d.parse(f"""MODEL (name {model_name}, kind FULL); SELECT 2 AS col"""))
    )

    logger = logging.getLogger("sqlmesh.core.snapshot.evaluator")

    with mock.patch.object(logger, "info") as mock_logger:
        sqlmesh.plan(auto_apply=True, no_prompts=True)

        assert any("Replacing view" in call[0][0] for call in mock_logger.call_args_list)

    _assert_mview_value(value=2)


def test_unicode_characters(ctx: TestContext, tmp_path: Path):
    # Engines that don't quote identifiers in views are incompatible with unicode characters in model names
    # at the time of writing this is Spark/Trino and they do this for compatibility reasons.
    # I also think Spark may not support unicode in general but that would need to be verified.
    if not ctx.engine_adapter.QUOTE_IDENTIFIERS_IN_VIEWS:
        pytest.skip("Skipping as these engines have issues with unicode characters in model names")

    model_name = "客户数据"
    table = ctx.table(model_name).sql(dialect=ctx.dialect)
    (tmp_path / "models").mkdir(exist_ok=True)

    model_def = f"""
    MODEL (
        name {table},
        kind FULL,
        dialect '{ctx.dialect}'
    );
    SELECT 1 as id
    """

    (tmp_path / "models" / "客户数据.sql").write_text(model_def)

    context = ctx.create_context(path=tmp_path)
    context.plan(auto_apply=True, no_prompts=True)

    results = ctx.get_metadata_results()
    assert len(results.views) == 1
    assert results.views[0].lower() == model_name

    schema = d.to_schema(ctx.schema(), dialect=ctx.dialect)
    schema_name = schema.args["db"].this
    schema.args["db"].set("this", "sqlmesh__" + schema_name)
    table_results = ctx.get_metadata_results(schema)
    assert len(table_results.tables) == 1
    assert table_results.tables[0].lower().startswith(schema_name.lower() + "________")


def test_sync_grants_config(ctx: TestContext) -> None:
    if not ctx.engine_adapter.SUPPORTS_GRANTS:
        pytest.skip(
            f"Skipping Test since engine adapter {ctx.engine_adapter.dialect} doesn't support grants"
        )

    table = ctx.table("sync_grants_integration")
    select_privilege = ctx.get_select_privilege()
    insert_privilege = ctx.get_insert_privilege()
    update_privilege = ctx.get_update_privilege()
    with ctx.create_users_or_roles("reader", "writer", "admin") as roles:
        ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})

        initial_grants = {
            select_privilege: [roles["reader"]],
            insert_privilege: [roles["writer"]],
        }
        ctx.engine_adapter.sync_grants_config(table, initial_grants)

        current_grants = ctx.engine_adapter._get_current_grants_config(table)
        assert set(current_grants.get(select_privilege, [])) == {roles["reader"]}
        assert set(current_grants.get(insert_privilege, [])) == {roles["writer"]}

        target_grants = {
            select_privilege: [roles["writer"], roles["admin"]],
            update_privilege: [roles["admin"]],
        }
        ctx.engine_adapter.sync_grants_config(table, target_grants)

        synced_grants = ctx.engine_adapter._get_current_grants_config(table)
        assert set(synced_grants.get(select_privilege, [])) == {
            roles["writer"],
            roles["admin"],
        }
        assert set(synced_grants.get(update_privilege, [])) == {roles["admin"]}
        assert synced_grants.get(insert_privilege, []) == []


def test_grants_sync_empty_config(ctx: TestContext):
    if not ctx.engine_adapter.SUPPORTS_GRANTS:
        pytest.skip(
            f"Skipping Test since engine adapter {ctx.engine_adapter.dialect} doesn't support grants"
        )

    table = ctx.table("grants_empty_test")
    select_privilege = ctx.get_select_privilege()
    insert_privilege = ctx.get_insert_privilege()
    with ctx.create_users_or_roles("user") as roles:
        ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})

        initial_grants = {
            select_privilege: [roles["user"]],
            insert_privilege: [roles["user"]],
        }
        ctx.engine_adapter.sync_grants_config(table, initial_grants)

        initial_current_grants = ctx.engine_adapter._get_current_grants_config(table)
        assert roles["user"] in initial_current_grants.get(select_privilege, [])
        assert roles["user"] in initial_current_grants.get(insert_privilege, [])

        ctx.engine_adapter.sync_grants_config(table, {})

        final_grants = ctx.engine_adapter._get_current_grants_config(table)
        assert final_grants == {}


def test_grants_case_insensitive_grantees(ctx: TestContext):
    if not ctx.engine_adapter.SUPPORTS_GRANTS:
        pytest.skip(
            f"Skipping Test since engine adapter {ctx.engine_adapter.dialect} doesn't support grants"
        )

    with ctx.create_users_or_roles("reader", "writer") as roles:
        table = ctx.table("grants_quoted_test")
        ctx.engine_adapter.create_table(table, {"id": exp.DataType.build("INT")})

        reader = roles["reader"]
        writer = roles["writer"]
        select_privilege = ctx.get_select_privilege()

        if ctx.dialect == "bigquery":
            # BigQuery labels are case sensitive, e.g. serviceAccount
            lablel, grantee = writer.split(":", 1)
            upper_case_writer = f"{lablel}:{grantee.upper()}"
        else:
            upper_case_writer = writer.upper()

        grants_config = {select_privilege: [reader, upper_case_writer]}
        ctx.engine_adapter.sync_grants_config(table, grants_config)

        # Grantees are still in lowercase
        current_grants = ctx.engine_adapter._get_current_grants_config(table)
        assert reader in current_grants.get(select_privilege, [])
        assert writer in current_grants.get(select_privilege, [])

        # Revoke writer
        grants_config = {select_privilege: [reader.upper()]}
        ctx.engine_adapter.sync_grants_config(table, grants_config)

        current_grants = ctx.engine_adapter._get_current_grants_config(table)
        assert reader in current_grants.get(select_privilege, [])
        assert writer not in current_grants.get(select_privilege, [])


def test_grants_plan(ctx: TestContext, tmp_path: Path):
    if not ctx.engine_adapter.SUPPORTS_GRANTS:
        pytest.skip(
            f"Skipping Test since engine adapter {ctx.engine_adapter.dialect} doesn't support grants"
        )

    table = ctx.table("grant_model").sql(dialect="duckdb")
    select_privilege = ctx.get_select_privilege()
    insert_privilege = ctx.get_insert_privilege()
    with ctx.create_users_or_roles("analyst", "etl_user") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        model_def = f"""
        MODEL (
            name {table},
            kind FULL,
            grants (
                '{select_privilege}' = ['{roles["analyst"]}']
            ),
            grants_target_layer 'all'
        );
        SELECT 1 as id, CURRENT_DATE as created_date
        """

        (tmp_path / "models" / "grant_model.sql").write_text(model_def)

        context = ctx.create_context(path=tmp_path)
        plan_result = context.plan(auto_apply=True, no_prompts=True)

        assert len(plan_result.new_snapshots) == 1
        snapshot = plan_result.new_snapshots[0]

        # Physical layer w/ grants
        table_name = snapshot.table_name()
        view_name = snapshot.qualified_view_name.for_environment(
            plan_result.environment_naming_info, dialect=ctx.dialect
        )
        current_grants = ctx.engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=ctx.dialect)
        )
        assert current_grants == {select_privilege: [roles["analyst"]]}

        # Virtual layer (view) w/ grants
        virtual_grants = ctx.engine_adapter._get_current_grants_config(
            exp.to_table(view_name, dialect=ctx.dialect)
        )
        assert virtual_grants == {select_privilege: [roles["analyst"]]}

        # Update model with query change and new grants
        updated_model = load_sql_based_model(
            d.parse(
                f"""
                MODEL (
                    name {table},
                    kind FULL,
                    grants (
                        '{select_privilege}' = ['{roles["analyst"]}', '{roles["etl_user"]}'],
                        '{insert_privilege}' = ['{roles["etl_user"]}']
                    ),
                    grants_target_layer 'all'
                );
                SELECT 1 as id, CURRENT_DATE as created_date, 'v2' as version
                """,
                default_dialect=context.default_dialect,
            ),
            dialect=context.default_dialect,
        )
        context.upsert_model(updated_model)

        plan = context.plan(auto_apply=True, no_prompts=True)
        plan_result = PlanResults.create(plan, ctx, ctx.add_test_suffix(TEST_SCHEMA))
        assert len(plan_result.plan.directly_modified) == 1

        new_snapshot = plan_result.snapshot_for(updated_model)
        assert new_snapshot is not None

        new_table_name = new_snapshot.table_name()
        final_grants = ctx.engine_adapter._get_current_grants_config(
            exp.to_table(new_table_name, dialect=ctx.dialect)
        )
        expected_final_grants = {
            select_privilege: [roles["analyst"], roles["etl_user"]],
            insert_privilege: [roles["etl_user"]],
        }
        assert set(final_grants.get(select_privilege, [])) == set(
            expected_final_grants[select_privilege]
        )
        assert final_grants.get(insert_privilege, []) == expected_final_grants[insert_privilege]

        # Virtual layer should also have the updated grants
        updated_virtual_grants = ctx.engine_adapter._get_current_grants_config(
            exp.to_table(view_name, dialect=ctx.dialect)
        )
        assert set(updated_virtual_grants.get(select_privilege, [])) == set(
            expected_final_grants[select_privilege]
        )
        assert (
            updated_virtual_grants.get(insert_privilege, [])
            == expected_final_grants[insert_privilege]
        )
